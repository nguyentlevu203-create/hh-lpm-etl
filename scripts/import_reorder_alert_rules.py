#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import reorder alert rules for HH Control Tower.

Reads the file "Cảnh báo đặt hàng" and loads into inventory.reorder_alert_rules.
Column "số lượng" is treated as reorder_point_qty.

Example:
  python scripts/import_reorder_alert_rules.py --file "data/inventory/Cảnh báo đặt hàng.xlsx" --replace-active
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from psycopg2.extras import execute_values

sys.path.append(str(Path(__file__).resolve().parents[1]))
from etl_common import clean_code, connect, file_sha256, json_safe, safe_num, safe_text  # noqa: E402

SOURCE_SYSTEM = "REORDER"
IMPORT_TYPE = "REORDER_RULES"
TZ = "Asia/Ho_Chi_Minh"


def strip_accents(text: str) -> str:
    s = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def norm_header(text: Any) -> str:
    return re.sub(r"\s+", " ", strip_accents(str(text or "")).lower()).strip()


def is_probable_ean(code: str) -> bool:
    return bool(re.fullmatch(r"\d{8,14}", clean_code(code)))


def split_code(ean_value: Any, sku_value: Any) -> Tuple[Optional[str], Optional[str]]:
    ean = clean_code(ean_value)
    sku = clean_code(sku_value)
    if ean and not is_probable_ean(ean):
        # Some files may put an internal code in EAN column; keep it as SKU if SKU is blank.
        if not sku:
            sku = ean
        ean = ""
    return ean or None, sku or None


def find_col(columns: List[Any], *candidates: str) -> Any:
    normalized = {norm_header(c): c for c in columns}
    for cand in candidates:
        nc = norm_header(cand)
        if nc in normalized:
            return normalized[nc]
    for c in columns:
        nc = norm_header(c)
        if any(norm_header(cand) in nc for cand in candidates):
            return c
    raise ValueError(f"Missing column. Need one of {candidates}. Available: {list(columns)}")


def read_rules(path: Path) -> List[Dict[str, Any]]:
    df = pd.read_excel(path, sheet_name=0, dtype=object)
    df.columns = [safe_text(c) for c in df.columns]
    c_ean = find_col(list(df.columns), "Ean", "EAN")
    c_sku = find_col(list(df.columns), "Mã", "Ma", "Mã nội bộ", "SKU")
    c_name = find_col(list(df.columns), "Tên sản phẩm", "Ten san pham", "Tên hàng")
    c_type = find_col(list(df.columns), "Loại", "Loai")
    c_uom = find_col(list(df.columns), "Đơn vị", "Don vi")
    c_qty = find_col(list(df.columns), "số lượng", "so luong", "reorder_point_qty")

    records: List[Dict[str, Any]] = []
    for idx, row in df.iterrows():
        ean, sku = split_code(row.get(c_ean), row.get(c_sku))
        name = safe_text(row.get(c_name), 1000)
        qty = safe_num(row.get(c_qty))
        if not name or qty <= 0:
            continue
        records.append({
            "source_row_no": int(idx) + 2,
            "ean_raw": ean,
            "sku_code_raw": sku,
            "product_name_raw": name,
            "product_type": safe_text(row.get(c_type), 100),
            "uom": safe_text(row.get(c_uom), 50),
            "reorder_point_qty": float(qty),
            "raw_payload": {safe_text(k): json_safe(v) for k, v in row.to_dict().items()},
        })
    if not records:
        raise RuntimeError(f"No reorder rules parsed from {path}")
    return records


def ensure_product(cur, *, ean: Optional[str], sku: Optional[str], product_name: str) -> Optional[str]:
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
    cur.execute("SELECT product_id FROM dim.product_master WHERE canonical_sku_norm = dim.norm_identifier(%s) LIMIT 1", (canonical,))
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
                (product_id, SOURCE_SYSTEM, ean),
            )
        if sku:
            cur.execute(
                """
                INSERT INTO dim.product_identifier_map(product_id, source_system, identifier_type, identifier_value, priority, is_active)
                VALUES (%s, %s, 'SKU', %s, 20, TRUE)
                ON CONFLICT DO NOTHING
                """,
                (product_id, SOURCE_SYSTEM, sku),
            )
    return str(product_id) if product_id else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Import reorder alert rules into Control Tower DB")
    ap.add_argument("--file", required=True, help="Cảnh báo đặt hàng Excel file")
    ap.add_argument("--replace-active", action="store_true", help="Deactivate all current active rules before inserting this file")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise FileNotFoundError(path)
    records = read_rules(path)
    fhash = file_sha256(path)

    conn = connect(); cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO inventory.import_runs(
                snapshot_date, source_timezone, warehouse, source_file, file_hash, import_type,
                rows_read, rows_staged, rows_loaded, status, created_by
            )
            VALUES (control_tower.local_business_date(), %s, NULL, %s, %s, %s, %s, %s, 0, 'staged', %s)
            RETURNING id
            """,
            (TZ, str(path), fhash, IMPORT_TYPE, len(records), len(records), "import_reorder_alert_rules"),
        )
        import_run_id = int(cur.fetchone()[0])

        if args.replace_active:
            cur.execute("UPDATE inventory.reorder_alert_rules SET is_active = FALSE, updated_at = now() WHERE is_active = TRUE")

        loaded = 0
        for r in records:
            product_id = ensure_product(cur, ean=r["ean_raw"], sku=r["sku_code_raw"], product_name=r["product_name_raw"])
            cur.execute(
                """
                INSERT INTO inventory.reorder_alert_rules(
                    product_id, ean_raw, sku_code_raw, product_name_raw,
                    product_type, uom, reorder_point_qty, is_focus_sku, priority,
                    owner, note, is_active, source_file, import_run_id, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, 'P2', NULL, NULL, TRUE, %s, %s, now())
                ON CONFLICT (product_id) WHERE is_active = TRUE AND product_id IS NOT NULL
                DO UPDATE SET
                    ean_raw = EXCLUDED.ean_raw,
                    sku_code_raw = EXCLUDED.sku_code_raw,
                    product_name_raw = EXCLUDED.product_name_raw,
                    product_type = EXCLUDED.product_type,
                    uom = EXCLUDED.uom,
                    reorder_point_qty = EXCLUDED.reorder_point_qty,
                    source_file = EXCLUDED.source_file,
                    import_run_id = EXCLUDED.import_run_id,
                    updated_at = now()
                """,
                (
                    product_id, r["ean_raw"], r["sku_code_raw"], r["product_name_raw"],
                    r["product_type"], r["uom"], r["reorder_point_qty"], str(path), import_run_id,
                ),
            )
            loaded += 1

        cur.execute(
            """
            UPDATE inventory.import_runs
            SET rows_loaded = %s, status = 'success', committed_at = now()
            WHERE id = %s
            """,
            (loaded, import_run_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()

    print(json.dumps({"status": "success", "file": str(path), "rows_loaded": loaded, "import_run_id": import_run_id}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
