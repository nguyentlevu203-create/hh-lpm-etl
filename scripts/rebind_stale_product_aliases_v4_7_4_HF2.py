#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.7.4 HF2 - safely rebind stale marketplace SKU aliases to approved EAN products.

Problem addressed
-----------------
The approved SKU master says, for example:
    ST00526 <-> 3574661700526
The EAN resolves to the canonical inventory product_id, but an older SHOPEE/TIKTOK
SKU alias may already point to a different product_id that was auto-created from the
short SKU. The v4.7.4 importer correctly blocks this as a conflict.

HF2 does NOT merge or delete product_master rows. It only re-points the exact active
(source_system, SKU, short_sku) identifier rows to the product_id resolved by the
approved EAN, and only when there is no contradictory EAN evidence on the stale
product identity.

Safety
------
- Default is dry-run.
- Reads only status=APPROVED rows from product_sku_alias_master_v4_7_4.csv.
- Canonical target must resolve unambiguously by exact EAN.
- Existing conflicting alias must be an exact normalized short-SKU match.
- If a stale product has a different primary_ean or active EAN identifier, HF2 blocks it.
- No fuzzy matching, substring matching, code truncation, product-name matching,
  product creation, product deletion, or product-master merge.
- --apply executes all safe rebinds in one transaction; any SQL error rolls back all.

After HF2 succeeds, rerun import_product_sku_aliases_v4_7_4.py in dry-run mode.
Only when can_apply=true should you run that importer with --apply.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

try:
    import psycopg2
except Exception:
    psycopg2 = None

DEFAULT_SOURCES = ("INVENTORY", "NHANH", "SHOPEE", "TIKTOK")


def clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").replace("\ufeff", " ").replace("\xa0", " ")).strip()


def norm_local(v: Any) -> str:
    return clean(v).casefold()


def is_ean(v: Any) -> bool:
    return bool(re.fullmatch(r"\d{8,14}", clean(v)))


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
    rows: List[Dict[str, str]] = []
    seen_short: Dict[str, str] = {}
    seen_ean: Dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        for raw in rd:
            if clean(raw.get("status")).upper() != "APPROVED":
                continue
            ean = clean(raw.get("canonical_ean"))
            short = clean(raw.get("short_sku"))
            if not is_ean(ean):
                raise ValueError(f"Invalid approved EAN: {ean}")
            if not short:
                raise ValueError(f"Blank short SKU for {ean}")
            sk = norm_local(short)
            ek = norm_local(ean)
            if sk in seen_short and seen_short[sk] != ean:
                raise ValueError(f"Conflict in approved master for short SKU {short}: {seen_short[sk]} vs {ean}")
            if ek in seen_ean and norm_local(seen_ean[ek]) != sk:
                raise ValueError(f"Conflict in approved master for EAN {ean}: {seen_ean[ek]} vs {short}")
            seen_short[sk] = ean
            seen_ean[ek] = short
            rows.append({"ean": ean, "short_sku": short})
    return rows


def resolve_product_id(cur, ean: str) -> Tuple[str | None, List[str]]:
    cur.execute(
        """
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
        """,
        (ean, ean, ean),
    )
    hits = [str(r[0]) for r in cur.fetchall()]
    return (hits[0] if len(hits) == 1 else None), hits


def existing_alias(cur, source: str, short_sku: str) -> List[str]:
    cur.execute(
        """
        SELECT DISTINCT product_id::text
        FROM dim.product_identifier_map
        WHERE source_system=%s
          AND identifier_type='SKU'
          AND is_active IS TRUE
          AND dim.norm_identifier(identifier_value)=dim.norm_identifier(%s)
        ORDER BY product_id::text
        """,
        (source, short_sku),
    )
    return [str(r[0]) for r in cur.fetchall()]


def product_evidence(cur, product_id: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT product_id::text, canonical_sku_code, primary_ean, product_name
        FROM dim.product_master
        WHERE product_id::text=%s
        """,
        (product_id,),
    )
    row = cur.fetchone()
    master = {
        "product_id": product_id,
        "canonical_sku_code": row[1] if row else None,
        "primary_ean": row[2] if row else None,
        "product_name": row[3] if row else None,
    }
    cur.execute(
        """
        SELECT source_system, identifier_type, identifier_value, priority, is_active
        FROM dim.product_identifier_map
        WHERE product_id::text=%s
        ORDER BY is_active DESC, source_system, identifier_type, identifier_value
        """,
        (product_id,),
    )
    master["identifiers"] = [
        {
            "source_system": r[0],
            "identifier_type": r[1],
            "identifier_value": r[2],
            "priority": r[3],
            "is_active": bool(r[4]),
        }
        for r in cur.fetchall()
    ]
    return master


def contradictory_ean_evidence(evidence: Dict[str, Any], approved_ean: str) -> List[Dict[str, Any]]:
    contradictions: List[Dict[str, Any]] = []
    primary = clean(evidence.get("primary_ean"))
    if primary and norm_local(primary) != norm_local(approved_ean):
        contradictions.append({"kind": "PRIMARY_EAN", "value": primary})

    canonical = clean(evidence.get("canonical_sku_code"))
    if is_ean(canonical) and norm_local(canonical) != norm_local(approved_ean):
        contradictions.append({"kind": "CANONICAL_EAN_LIKE", "value": canonical})

    for ident in evidence.get("identifiers") or []:
        if not ident.get("is_active"):
            continue
        if clean(ident.get("identifier_type")).upper() != "EAN":
            continue
        value = clean(ident.get("identifier_value"))
        if value and norm_local(value) != norm_local(approved_ean):
            contradictions.append(
                {
                    "kind": "ACTIVE_EAN_IDENTIFIER",
                    "source_system": ident.get("source_system"),
                    "value": value,
                }
            )
    return contradictions


def run(path: Path, sources: Sequence[str], apply: bool) -> Dict[str, Any]:
    mappings = load_map(path)
    report: Dict[str, Any] = {
        "mode": "APPLY" if apply else "DRY_RUN",
        "mapping_rows": len(mappings),
        "source_systems": list(sources),
        "safe_rebinds": [],
        "already_correct": [],
        "no_existing_alias": [],
        "blocked_conflicts": [],
        "unresolved_eans": [],
    }

    conn = connect()
    cur = conn.cursor()
    try:
        for m in mappings:
            target_pid, ean_hits = resolve_product_id(cur, m["ean"])
            if not target_pid:
                report["unresolved_eans"].append(
                    {
                        "ean": m["ean"],
                        "short_sku": m["short_sku"],
                        "candidate_product_ids": ean_hits,
                    }
                )
                continue

            for source in sources:
                pids = existing_alias(cur, source, m["short_sku"])
                if not pids:
                    report["no_existing_alias"].append(
                        {
                            "source_system": source,
                            "short_sku": m["short_sku"],
                            "ean": m["ean"],
                            "target_product_id": target_pid,
                        }
                    )
                    continue
                if pids == [target_pid]:
                    report["already_correct"].append(
                        {
                            "source_system": source,
                            "short_sku": m["short_sku"],
                            "ean": m["ean"],
                            "product_id": target_pid,
                        }
                    )
                    continue

                for old_pid in pids:
                    if old_pid == target_pid:
                        continue
                    evidence = product_evidence(cur, old_pid)
                    contradictions = contradictory_ean_evidence(evidence, m["ean"])
                    item = {
                        "source_system": source,
                        "short_sku": m["short_sku"],
                        "ean": m["ean"],
                        "from_product_id": old_pid,
                        "to_product_id": target_pid,
                        "stale_product_master": {
                            "canonical_sku_code": evidence.get("canonical_sku_code"),
                            "primary_ean": evidence.get("primary_ean"),
                            "product_name": evidence.get("product_name"),
                        },
                        "contradictory_ean_evidence": contradictions,
                    }
                    if contradictions:
                        report["blocked_conflicts"].append(item)
                    else:
                        report["safe_rebinds"].append(item)

        report["safe_rebind_count"] = len(report["safe_rebinds"])
        report["blocked_conflict_count"] = len(report["blocked_conflicts"])
        report["unresolved_ean_count"] = len(report["unresolved_eans"])
        report["can_apply"] = not report["blocked_conflicts"] and not report["unresolved_eans"]

        if not apply:
            conn.rollback()
            return report

        if not report["can_apply"]:
            conn.rollback()
            return report

        for item in report["safe_rebinds"]:
            cur.execute(
                """
                UPDATE dim.product_identifier_map
                SET product_id=%s
                WHERE source_system=%s
                  AND identifier_type='SKU'
                  AND is_active IS TRUE
                  AND dim.norm_identifier(identifier_value)=dim.norm_identifier(%s)
                  AND product_id::text=%s
                """,
                (
                    item["to_product_id"],
                    item["source_system"],
                    item["short_sku"],
                    item["from_product_id"],
                ),
            )
            if cur.rowcount < 1:
                raise RuntimeError(
                    "Expected stale alias row was not updated: "
                    f"{item['source_system']} {item['short_sku']} {item['from_product_id']}"
                )
        conn.commit()
        report["applied_rebind_count"] = len(report["safe_rebinds"])
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="config/product_sku_alias_master_v4_7_4.csv")
    ap.add_argument("--source-systems", default=",".join(DEFAULT_SOURCES))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    sources = [clean(x).upper() for x in args.source_systems.split(",") if clean(x)]
    report = run(path, sources, args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("can_apply") else 1


if __name__ == "__main__":
    raise SystemExit(main())
