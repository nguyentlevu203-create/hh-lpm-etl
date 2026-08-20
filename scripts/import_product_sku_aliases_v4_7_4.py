#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import approved short-SKU/EAN mappings into dim.product_identifier_map.

Default is dry-run. --apply writes aliases only when every approved EAN resolves
unambiguously to one existing product_id and no alias conflict exists.
No product is auto-created and no existing alias is silently re-pointed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None

DEFAULT_SOURCES = ("INVENTORY", "NHANH", "SHOPEE", "TIKTOK")


def clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").replace("\ufeff", " ").replace("\xa0", " ")).strip()


def connect():
    if psycopg2 is None:
        raise RuntimeError("Missing psycopg2-binary")
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return psycopg2.connect(dsn)
    return psycopg2.connect(
        host=os.getenv("PGHOST", os.getenv("ETL_DB_HOST", "localhost")),
        port=os.getenv("PGPORT", os.getenv("ETL_DB_PORT", "5433")),
        dbname=os.getenv("PGDATABASE", os.getenv("ETL_DB_NAME", "DuLieu")),
        user=os.getenv("PGUSER", os.getenv("ETL_DB_USER", "postgres")),
        password=os.getenv("PGPASSWORD", os.getenv("ETL_DB_PASSWORD", "")),
    )


def load_map(path: Path) -> List[Dict[str, str]]:
    rows=[]; seen_short={}; seen_ean={}
    with path.open("r",encoding="utf-8-sig",newline="") as f:
        rd=csv.DictReader(f)
        for raw in rd:
            if clean(raw.get("status")).upper() != "APPROVED":
                continue
            ean=clean(raw.get("canonical_ean")); short=clean(raw.get("short_sku"))
            if not re.fullmatch(r"\d{8,14}",ean):
                raise ValueError(f"Invalid approved EAN: {ean}")
            if not short:
                raise ValueError(f"Blank short SKU for {ean}")
            sk=short.casefold(); ek=ean.casefold()
            if sk in seen_short and seen_short[sk] != ean:
                raise ValueError(f"Conflict short SKU {short}: {seen_short[sk]} vs {ean}")
            if ek in seen_ean and seen_ean[ek].casefold() != sk:
                raise ValueError(f"Conflict EAN {ean}: {seen_ean[ek]} vs {short}")
            seen_short[sk]=ean; seen_ean[ek]=short
            rows.append({"ean":ean,"short_sku":short})
    return rows


def resolve_product_id(cur, ean: str) -> Tuple[str|None,List[str]]:
    cur.execute("""
        WITH hits AS (
            SELECT product_id::text AS product_id
            FROM dim.product_master
            WHERE dim.norm_identifier(COALESCE(primary_ean,'')) = dim.norm_identifier(%s)
               OR canonical_sku_norm = dim.norm_identifier(%s)
            UNION
            SELECT product_id::text
            FROM dim.product_identifier_map
            WHERE is_active IS TRUE
              AND identifier_type = 'EAN'
              AND dim.norm_identifier(identifier_value) = dim.norm_identifier(%s)
        )
        SELECT DISTINCT product_id FROM hits ORDER BY product_id
    """,(ean,ean,ean))
    hits=[str(r[0]) for r in cur.fetchall()]
    return (hits[0] if len(hits)==1 else None), hits


def existing_alias(cur, source: str, short_sku: str) -> List[str]:
    cur.execute("""
        SELECT DISTINCT product_id::text
        FROM dim.product_identifier_map
        WHERE source_system=%s AND identifier_type='SKU' AND is_active IS TRUE
          AND dim.norm_identifier(identifier_value)=dim.norm_identifier(%s)
        ORDER BY product_id::text
    """,(source,short_sku))
    return [str(r[0]) for r in cur.fetchall()]


def run(path: Path, sources: Sequence[str], apply: bool) -> Dict[str,Any]:
    mappings=load_map(path)
    report={"mode":"APPLY" if apply else "DRY_RUN","mapping_rows":len(mappings),"source_systems":list(sources),"new_aliases":0,"existing_aliases":0,"conflicts":[],"unresolved_eans":[],"details":[]}
    conn=connect(); cur=conn.cursor()
    try:
        actions=[]
        for m in mappings:
            pid,hits=resolve_product_id(cur,m["ean"])
            if not pid:
                report["unresolved_eans"].append({"ean":m["ean"],"short_sku":m["short_sku"],"candidate_product_ids":hits})
                continue
            for source in sources:
                existing=existing_alias(cur,source,m["short_sku"])
                if existing and any(x != pid for x in existing):
                    report["conflicts"].append({"source_system":source,"short_sku":m["short_sku"],"ean":m["ean"],"expected_product_id":pid,"existing_product_ids":existing})
                    continue
                if pid in existing:
                    report["existing_aliases"]+=1
                    report["details"].append({"action":"EXISTING","source_system":source,"short_sku":m["short_sku"],"ean":m["ean"],"product_id":pid})
                else:
                    report["new_aliases"]+=1
                    actions.append((pid,source,m["short_sku"]))
                    report["details"].append({"action":"NEW","source_system":source,"short_sku":m["short_sku"],"ean":m["ean"],"product_id":pid})
        if report["conflicts"] or report["unresolved_eans"]:
            conn.rollback()
            report["can_apply"]=False
            return report
        report["can_apply"]=True
        if apply:
            for pid,source,short in actions:
                cur.execute("""
                    INSERT INTO dim.product_identifier_map(product_id,source_system,identifier_type,identifier_value,priority,is_active)
                    VALUES (%s,%s,'SKU',%s,20,TRUE)
                    ON CONFLICT DO NOTHING
                """,(pid,source,short))
            conn.commit()
        else:
            conn.rollback()
        return report
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--file",default="config/product_sku_alias_master_v4_7_4.csv")
    ap.add_argument("--source-systems",default=",".join(DEFAULT_SOURCES))
    ap.add_argument("--apply",action="store_true")
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:
        rows=load_map(Path(args.file))
        if len(rows)!=102:
            print(f"SELF-TEST FAILED: expected 102 approved mappings, got {len(rows)}",file=sys.stderr); return 1
        probe=next((r for r in rows if r['ean']=='3574661700526'),None)
        if not probe or probe['short_sku']!='ST00526':
            print("SELF-TEST FAILED: 3574661700526 mapping",file=sys.stderr); return 1
        print("SELF-TEST PASSED")
        print("Approved mappings: 102")
        print("3574661700526 <-> ST00526: PASS")
        return 0
    path=Path(args.file)
    if not path.is_absolute(): path=Path(__file__).resolve().parents[1]/path
    sources=[clean(x).upper() for x in args.source_systems.split(',') if clean(x)]
    report=run(path,sources,args.apply)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report.get('can_apply') else 1

if __name__=='__main__':
    raise SystemExit(main())
