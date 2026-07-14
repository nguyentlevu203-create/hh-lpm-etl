#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import bảng giá vốn chuẩn vào dim.product_costs.

Rule production:
  Excel column "Mã ean" -> dim.product_costs.ma
  Excel column "Mã"     -> dim.product_costs.ma
  Excel column "Giá nhập VND-cont" -> dim.product_costs.gia_von

Một dòng Excel có thể tạo 2 mã lookup: EAN và mã nội bộ/SKU.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from psycopg2.extras import execute_values, Json

from etl_common import (
    json_safe,
    assert_production_schema,
    begin_batch,
    clean_code,
    connect,
    file_sha256,
    find_col,
    finish_batch,
    log_import_error,
    money_series,
    read_file,
    row_to_json,
    safe_text,
)

DEFAULT_FILE = Path("Giá nhập 25.06.2026.xlsx")


def import_product_costs(file_path: Path) -> int:
    assert_production_schema()
    if not file_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file giá vốn: {file_path}")

    df = read_file(file_path, dtype=object, header=0)
    df.columns = [safe_text(c) for c in df.columns]

    col_ean = find_col(df.columns, ["mã ean", "ma ean", "ean", "barcode"])
    col_ma = find_col(df.columns, ["mã", "ma", "mã sản phẩm", "ma san pham", "sku"], exact="Mã") or find_col(df.columns, ["mã", "ma", "mã sản phẩm", "sku"])
    col_cost = find_col(df.columns, ["giá nhập vnd-cont", "gia nhap vnd-cont", "giá nhập", "gia nhap", "giá vốn", "gia von", "cost"])

    if not col_cost:
        raise ValueError("File giá vốn thiếu cột giá vốn. Cần cột 'Giá nhập VND-cont' hoặc cột tương đương.")
    if not col_ean and not col_ma:
        raise ValueError("File giá vốn thiếu cả cột 'Mã ean' và 'Mã'.")

    rows_read = len(df)
    rows_stage = []
    rows_error = 0

    conn = connect()
    cur = conn.cursor()
    batch_id = None
    try:
        batch_id = begin_batch(cur, "MASTER_COST", "import_product_costs", str(file_path), file_sha256(file_path))

        for i, row in df.iterrows():
            raw = row_to_json(row)
            cost = float(money_series(row.get(col_cost)).iloc[0]) if col_cost else None
            ma_ean = clean_code(row.get(col_ean, "")) if col_ean else ""
            ma = clean_code(row.get(col_ma, "")) if col_ma else ""

            if cost is not None and cost < 0:
                rows_error += 1
                log_import_error(
                    cur,
                    batch_id,
                    "MASTER_COST",
                    "dim.product_costs",
                    file_path.name,
                    int(i) + 2,
                    "NEGATIVE_COST",
                    "Giá vốn âm, không nạp vào staging do CHECK constraint.",
                    raw,
                )
                continue

            rows_stage.append((batch_id, file_path.name, int(i) + 2, ma_ean or None, ma or None, cost if cost is not None else None, Json(json_safe(raw))))

        if rows_stage:
            execute_values(
                cur,
                """
                INSERT INTO dim.product_costs_stage(
                    batch_id, source_file, row_number, ma_ean, ma, gia_von, raw_row
                ) VALUES %s
                """,
                rows_stage,
                page_size=1000,
            )

        cur.execute("SELECT rows_loaded, rows_error FROM dim.load_product_costs_from_stage(%s)", (batch_id,))
        loaded_from_function, error_from_function = cur.fetchone()
        rows_loaded = int(loaded_from_function or 0)
        rows_error += int(error_from_function or 0)

        finish_batch(cur, batch_id, "success", rows_read=rows_read, rows_loaded=rows_loaded, rows_error=rows_error)
        conn.commit()
        print(f"✅ Đã nạp giá vốn: {rows_loaded} mã lookup, lỗi: {rows_error}, batch_id={batch_id}")
        return rows_loaded
    except Exception as e:
        conn.rollback()
        if batch_id:
            try:
                cur = conn.cursor()
                finish_batch(cur, batch_id, "failed", rows_read=rows_read, rows_loaded=0, rows_error=rows_error, error_message=str(e)[:2000])
                conn.commit()
            except Exception:
                conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Import bảng giá vốn vào dim.product_costs")
    parser.add_argument("file", nargs="?", default=str(DEFAULT_FILE), help="Đường dẫn file Excel/CSV bảng giá vốn")
    args = parser.parse_args()
    import_product_costs(Path(args.file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
