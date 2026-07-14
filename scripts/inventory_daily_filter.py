#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inventory Daily Filter for HH Control Tower.

Reads daily inventory by warehouse/lot from Excel exports and loads into:
  inventory.import_runs
  inventory.stock_lot_snapshots_stage
  inventory.stock_lot_snapshots via inventory.commit_stock_snapshot(load_batch_id)

Production design:
- Does NOT write directly to final snapshot table.
- Uses staging + commit function for transaction-safe swap by snapshot_date + warehouse.
- Auto-upserts dim.product_master / dim.product_identifier_map for INVENTORY keys.
- Supports both current HN and HCM file layouts.

Example:
  python scripts/inventory_daily_filter.py --snapshot-date 2026-05-20 --warehouse HN --file "data/inventory/HN.xlsx" --rerun
  python scripts/inventory_daily_filter.py --snapshot-date 2026-05-20 --file "data/inventory/HCM.xlsx" --rerun
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from psycopg2.extras import execute_values

sys.path.append(str(Path(__file__).resolve().parents[1]))
from etl_common import (  # noqa: E402
    clean_code,
    connect,
    file_sha256,
    json_safe,
    parse_date,
    safe_num,
    safe_text,
)

SOURCE_SYSTEM = "INVENTORY"
IMPORT_TYPE = "STOCK_SNAPSHOT"
TZ = "Asia/Ho_Chi_Minh"


@dataclass
class StockRecord:
    source_row_no: int
    warehouse: str
    code_raw: str
    ean_raw: Optional[str]
    sku_code_raw: Optional[str]
    product_name_raw: str
    lot_no: Optional[str]
    expiry_date: Optional[date]
    uom: Optional[str]
    stock_qty: float
    raw_payload: Dict[str, Any]
    product_id: Optional[str] = None


def strip_accents(text: str) -> str:
    s = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def norm_for_match(text: Any) -> str:
    return re.sub(r"\s+", " ", strip_accents(str(text or "")).lower()).strip()


def is_probable_ean(code: str) -> bool:
    s = clean_code(code)
    return bool(re.fullmatch(r"\d{8,14}", s))


def split_code(code: Any) -> Tuple[Optional[str], Optional[str]]:
    c = clean_code(code)
    if not c:
        return None, None
    if is_probable_ean(c):
        return c, None
    return None, c


def normalize_warehouse(text: Any, fallback: Optional[str] = None) -> str:
    raw = safe_text(text)
    if not raw:
        raw = safe_text(fallback) or "UNKNOWN"
    n = norm_for_match(raw)
    if "nhan tem" in n or "nhan tem" in n.replace("ă", "a"):
        return "HCM - NHAN TEM"
    if "hcm" in n or "thuong ha" in n or "thuong ha" in n:
        return "HCM"
    if "ha noi" in n or "hn" == n:
        return "HN"
    if raw.upper() in {"HN", "HCM"}:
        return raw.upper()
    return raw.strip()


def extract_warehouse_from_section(value: Any) -> Optional[str]:
    s = safe_text(value)
    if not s:
        return None
    n = norm_for_match(s)
    if "ma kho" not in n and "kho:" not in n:
        return None
    if "nhan tem" in n:
        return "HCM - NHAN TEM"
    if "hcm" in n:
        return "HCM"
    if "ha noi" in n:
        return "HN"
    # Fallback: keep text after ':' and before '(' if present.
    m = re.search(r":\s*([^\(]+)", s)
    return normalize_warehouse(m.group(1).strip() if m else s)


def extract_default_warehouse(df: pd.DataFrame, cli_warehouse: Optional[str]) -> str:
    if cli_warehouse:
        return normalize_warehouse(cli_warehouse)
    for i in range(min(len(df), 5)):
        line = " ".join(safe_text(x) for x in df.iloc[i].tolist() if safe_text(x))
        wh = extract_warehouse_from_section(line)
        if wh:
            return wh
    return "UNKNOWN"


def parse_inventory_workbook(path: Path, snapshot_date: date, cli_warehouse: Optional[str]) -> List[StockRecord]:
    raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    default_warehouse = extract_default_warehouse(raw, cli_warehouse)
    current_warehouse = default_warehouse
    records: List[StockRecord] = []

    # Expected current layouts:
    # HN: data from row 4, B:G = code/name/lot/expiry/uom/qty.
    # HCM: section rows in col A, data B:G; row 3/4 are headers.
    for idx, row in raw.iterrows():
        excel_row_no = int(idx) + 1
        values = list(row.values)
        col_a = values[0] if len(values) > 0 else None
        wh = extract_warehouse_from_section(col_a)
        if wh:
            current_warehouse = wh
            continue

        code = clean_code(values[1] if len(values) > 1 else None)
        product_name = safe_text(values[2] if len(values) > 2 else None, 1000)
        lot_no = safe_text(values[3] if len(values) > 3 else None, 255)
        expiry = parse_date(values[4] if len(values) > 4 else None)
        uom = safe_text(values[5] if len(values) > 5 else None, 50)
        qty = safe_num(values[6] if len(values) > 6 else None)

        if not code or not product_name:
            continue
        code_match = norm_for_match(code)
        name_match = norm_for_match(product_name)
        if code_match in {"ma hang", "ma", "ean"} or "ten hang" in name_match:
            continue
        if qty <= 0:
            # Zero rows are not useful in stock-lot snapshot. OUT_OF_STOCK alerts are generated from reorder rules.
            continue

        ean_raw, sku_code_raw = split_code(code)
        rec = StockRecord(
            source_row_no=excel_row_no,
            warehouse=current_warehouse,
            code_raw=code,
            ean_raw=ean_raw,
            sku_code_raw=sku_code_raw,
            product_name_raw=product_name,
            lot_no=lot_no or None,
            expiry_date=expiry,
            uom=uom or None,
            stock_qty=float(qty),
            raw_payload={
                "source_row_no": excel_row_no,
                "warehouse": current_warehouse,
                "code": code,
                "product_name": product_name,
                "lot_no": lot_no,
                "expiry_date": expiry.isoformat() if expiry else None,
                "uom": uom,
                "stock_qty": float(qty),
            },
        )
        records.append(rec)

    if not records:
        raise RuntimeError(f"No inventory rows parsed from {path}")
    return records


def row_hash(snapshot_date: date, r: StockRecord) -> str:
    parts = [
        snapshot_date.isoformat(), r.warehouse, r.ean_raw or "", r.sku_code_raw or "",
        r.product_name_raw, r.lot_no or "", r.expiry_date.isoformat() if r.expiry_date else "", str(r.stock_qty),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def ensure_product(cur, *, source_system: str, ean: Optional[str], sku: Optional[str], product_name: str) -> Optional[str]:
    canonical = ean or sku
    if not canonical:
        return None
    cur.execute(
        """
        INSERT INTO dim.product_master(canonical_sku_code, primary_ean, product_name, is_active, created_at, updated_at)
        VALUES (%s, %s, %s, TRUE, now(), now())
        ON CONFLICT ON CONSTRAINT uq_product_master_canonical_sku
        DO UPDATE SET
            primary_ean = COALESCE(dim.product_master.primary_ean, EXCLUDED.primary_ean),
            product_name = CASE
                WHEN dim.product_master.product_name IS NULL OR dim.product_master.product_name = ''
                     OR dim.product_master.product_name = dim.product_master.canonical_sku_code
                     OR dim.product_master.product_name = dim.product_master.primary_ean
                THEN EXCLUDED.product_name
                ELSE dim.product_master.product_name
            END,
            updated_at = now()
        """,
        (canonical, ean, product_name or canonical),
    )
    cur.execute(
        "SELECT product_id FROM dim.product_master WHERE canonical_sku_norm = dim.norm_identifier(%s) LIMIT 1",
        (canonical,),
    )
    row = cur.fetchone()
    product_id = row[0] if row else None
    if product_id:
        if ean:
            cur.execute(
                """
                INSERT INTO dim.product_identifier_map(product_id, source_system, identifier_type, identifier_value, priority, is_active)
                VALUES (%s, %s, 'EAN', %s, 10, TRUE)
                ON CONFLICT DO NOTHING
                """,
                (product_id, source_system, ean),
            )
        if sku:
            cur.execute(
                """
                INSERT INTO dim.product_identifier_map(product_id, source_system, identifier_type, identifier_value, priority, is_active)
                VALUES (%s, %s, 'SKU', %s, 20, TRUE)
                ON CONFLICT DO NOTHING
                """,
                (product_id, source_system, sku),
            )
    return str(product_id) if product_id else None


def create_import_run(cur, *, snapshot_date: date, warehouse: str, path: Path, rows_read: int, rows_staged: int, total_stock: float, rerun: bool) -> Tuple[int, str]:
    fhash = file_sha256(path)
    cur.execute(
        """
        SELECT id, load_batch_id, status
        FROM inventory.import_runs
        WHERE snapshot_date = %s AND warehouse = %s AND import_type = %s AND file_hash = %s
        LIMIT 1
        """,
        (snapshot_date, warehouse, IMPORT_TYPE, fhash),
    )
    existing = cur.fetchone()
    hash_to_insert = fhash
    if existing and not rerun:
        raise RuntimeError(
            f"Inventory file already imported for {snapshot_date} / {warehouse} with same hash. "
            f"Existing import_run_id={existing[0]}, status={existing[2]}. Use --rerun to load again."
        )
    if existing and rerun:
        hash_to_insert = None

    cur.execute(
        """
        INSERT INTO inventory.import_runs(
            snapshot_date, source_timezone, warehouse, source_file, file_hash, import_type,
            rows_read, rows_staged, total_stock_from_rows, status, created_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'staged', %s)
        RETURNING id, load_batch_id
        """,
        (snapshot_date, TZ, warehouse, str(path), hash_to_insert, IMPORT_TYPE, rows_read, rows_staged, total_stock, "inventory_daily_filter"),
    )
    import_run_id, load_batch_id = cur.fetchone()
    return int(import_run_id), str(load_batch_id)


def load_group(cur, *, path: Path, snapshot_date: date, warehouse: str, records: List[StockRecord], rerun: bool) -> Dict[str, Any]:
    total_stock = sum(float(r.stock_qty or 0) for r in records)
    import_run_id, load_batch_id = create_import_run(
        cur,
        snapshot_date=snapshot_date,
        warehouse=warehouse,
        path=path,
        rows_read=len(records),
        rows_staged=len(records),
        total_stock=total_stock,
        rerun=rerun,
    )

    for r in records:
        r.product_id = ensure_product(cur, source_system=SOURCE_SYSTEM, ean=r.ean_raw, sku=r.sku_code_raw, product_name=r.product_name_raw)

    rows = []
    for r in records:
        rows.append((
            load_batch_id,
            import_run_id,
            snapshot_date,
            TZ,
            warehouse,
            r.source_row_no,
            r.product_id,
            r.ean_raw,
            r.sku_code_raw,
            r.product_name_raw,
            r.lot_no,
            r.expiry_date,
            r.uom,
            r.stock_qty,
            str(path),
            row_hash(snapshot_date, r),
            'mapped' if r.product_id else 'unmapped',
        ))

    execute_values(
        cur,
        """
        INSERT INTO inventory.stock_lot_snapshots_stage(
            load_batch_id, import_run_id, snapshot_date, source_timezone, warehouse,
            source_row_no, product_id, ean_raw, sku_code_raw, product_name_raw,
            lot_no, expiry_date, uom, stock_qty, source_file, source_row_hash, row_status
        ) VALUES %s
        """,
        rows,
        page_size=500,
    )

    cur.execute("SELECT * FROM inventory.commit_stock_snapshot(%s, %s)", (load_batch_id, "inventory_daily_filter"))
    committed = cur.fetchone()
    return {
        "warehouse": warehouse,
        "import_run_id": import_run_id,
        "load_batch_id": load_batch_id,
        "rows_staged": len(records),
        "total_stock": total_stock,
        "commit_result": {
            "import_run_id": committed[0],
            "snapshot_date": committed[1].isoformat() if hasattr(committed[1], "isoformat") else str(committed[1]),
            "warehouse": committed[2],
            "rows_loaded": committed[3],
            "unmapped_rows": committed[4],
            "unmapped_pct": float(committed[5] or 0),
        } if committed else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Import daily inventory snapshot into Control Tower DB")
    ap.add_argument("--file", required=True, help="Inventory Excel file")
    ap.add_argument("--snapshot-date", required=True, help="Business snapshot date YYYY-MM-DD")
    ap.add_argument("--warehouse", help="Default warehouse when file has no section warehouse, e.g. HN/HCM")
    ap.add_argument("--rerun", action="store_true", help="Allow reloading same file hash as a new import run")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise FileNotFoundError(path)
    snap = datetime.strptime(args.snapshot_date, "%Y-%m-%d").date()

    records = parse_inventory_workbook(path, snap, args.warehouse)
    by_wh: Dict[str, List[StockRecord]] = defaultdict(list)
    for r in records:
        by_wh[r.warehouse].append(r)

    results = []
    conn = connect()
    cur = conn.cursor()
    try:
        for wh, group in sorted(by_wh.items()):
            results.append(load_group(cur, path=path, snapshot_date=snap, warehouse=wh, records=group, rerun=args.rerun))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()

    print(json.dumps({"status": "success", "file": str(path), "snapshot_date": snap.isoformat(), "groups": results}, ensure_ascii=False, indent=2, default=json_safe))


if __name__ == "__main__":
    main()
