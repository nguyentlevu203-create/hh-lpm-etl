#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nhanh.vn ETL for DuLieu production schema.

Writes only to:
  nhanh.orders
  nhanh.order_items
  nhanh.ads_costs

Reads shared COGS only from:
  dim.product_costs(ma, gia_von)
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from psycopg2.extras import execute_values, Json

import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))
from etl_common import (
    assert_production_schema,
    archive_file,
    begin_batch,
    clean_code,
    connect,
    file_sha256,
    find_col,
    finish_batch,
    get_input_files,
    load_cost_map,
    json_safe,
    log_import_error,
    lookup_unit_cost,
    money_series,
    parse_date,
    parse_datetime,
    read_smart_table,
    row_to_json,
    safe_num,
    safe_text,
)

BASE_IN = Path(os.getenv("NHANH_BASE_IN", "data/nhanh"))
BASE_OUT = Path(os.getenv("NHANH_BASE_OUT", "processed/nhanh"))
ORDER_DIR = BASE_IN / "orders"
ADS_DIR = BASE_IN / "ads"
ORDER_OUT = BASE_OUT / "orders"
ADS_OUT = BASE_OUT / "ads"

ALLOWED_LABELS = {
    "FB - LPM Vietnam",
    "FB - LPM Vietnam 168",
    "FB -Blédina Brasses VN",
    "landing page",
    "Web - lepetitmarseillais.vn",
    "OA - LPM Vietnam",
    "INSTAGRAM",
}

# v2.12 exact Nhanh business rules.
# IMPORTANT: status matching is exact after trimming outer spaces only.
NHANH_NO_BO_STATUSES = {"Đã hủy", "Hệ Thống hủy", "Đã hoàn", "Khách hủy"}
NHANH_RETURN_STATUSES = {"Đã hoàn"}
NHANH_RETURN_FEE_VND = 25000.0

HEADER_KEYWORDS = [
    "id", "mã đơn", "ma don", "trạng thái", "trang thai", "khách hàng", "khach hang", "nhãn", "nhan",
    "mã sản phẩm", "ma san pham", "mã vạch", "barcode",
]


def find_exact_header_trimmed(columns, required_name: str):
    """Return the column whose header matches required_name exactly after trimming outer spaces.

    This deliberately does NOT use fuzzy matching or fallback names.
    It preserves Vietnamese accents and all characters in the source header.
    """
    for c in columns:
        if str(c).strip() == required_name:
            return c
    available = ", ".join(str(c).strip() for c in columns)
    raise ValueError(
        f"File Nhanh thiếu cột bắt buộc chính xác '{required_name}'. "
        f"Các cột hiện có: {available}"
    )


def is_blank_cell(value) -> bool:
    """True only when the ORIGINAL Excel cell is blank/null-like.

    Used before any ffill. This prevents an order header with blank Nhãn or
    blank Giá trị đơn hàng from inheriting values from the previous order.
    """
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    s = str(value).strip()
    return s == "" or s.lower() in {"nan", "none", "nat", "null"}


def exact_cell_text(value, max_len: int = 500) -> str:
    """Trim outer spaces only; preserve Vietnamese accents and all inner chars."""
    if is_blank_cell(value):
        return ""
    return str(value).strip()[:max_len]


def build_valid_nhanh_order_scope(df: pd.DataFrame, c_id, c_label, c_value, path: Path) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Validate Nhanh orders from ORIGINAL order-header rows before any ffill.

    Required production rules:
    - The order id must appear on an order header row.
    - Column name must be exactly "Nhãn" and the original header-row cell must
      be one of ALLOWED_LABELS.
    - Column name must be exactly "Giá trị đơn hàng" and the original header-row
      cell must be non-blank.
    - Only after this validation do item rows inherit order-level fields within
      the same order.
    """
    scoped = df.copy()
    scoped["__order_id_header"] = scoped[c_id].map(clean_code)
    scoped["__is_order_header"] = scoped["__order_id_header"] != ""

    header_rows = scoped.loc[scoped["__is_order_header"]].copy()
    if header_rows.empty:
        print(f"⚠️ Nhanh {path.name}: không có dòng header đơn hợp lệ.")
        return scoped.iloc[0:0].copy(), {}

    header_rows["__label_header_raw"] = header_rows[c_label].map(lambda x: exact_cell_text(x, 200))
    header_rows["__order_value_header_blank"] = header_rows[c_value].map(is_blank_cell)

    invalid_label = ~header_rows["__label_header_raw"].isin(ALLOWED_LABELS)
    invalid_value = header_rows["__order_value_header_blank"]
    valid_headers = header_rows.loc[~invalid_label & ~invalid_value].copy()

    valid_ids = set(valid_headers["__order_id_header"].astype(str))
    invalid_orders = len(header_rows) - len(valid_headers)

    # Assign each physical row to the nearest previous order header, then keep only
    # rows whose original header passed the strict validation above.
    scoped["__external_order_id"] = scoped["__order_id_header"].replace("", pd.NA).ffill()
    scoped = scoped.loc[scoped["__external_order_id"].isin(valid_ids)].copy()

    if invalid_orders:
        invalid_rows = len(df) - len(scoped)
        no_label = int(invalid_label.sum())
        no_value = int(invalid_value.sum())
        print(
            f"⚠️ Nhanh {path.name}: loại {invalid_orders} đơn / {invalid_rows} dòng "
            f"trước ffill vì header gốc không đạt điều kiện: "
            f"Nhãn không hợp lệ hoặc trống={no_label}, "
            f"Giá trị đơn hàng trống={no_value}."
        )

    return scoped, {oid: oid for oid in valid_ids}


def prepare_dirs() -> None:
    for p in [ORDER_DIR, ADS_DIR, ORDER_OUT, ADS_OUT]:
        p.mkdir(parents=True, exist_ok=True)


def process_order_file(path: Path) -> pd.DataFrame:
    df = read_smart_table(path, HEADER_KEYWORDS)
    if df.empty:
        return df

    c_id = find_col(df.columns, ["id", "mã đơn hàng", "ma don hang", "mã đơn", "ma don"], exact="ID") or find_col(df.columns, ["id"])
    c_time = find_col(df.columns, ["thời gian", "thoi gian", "ngày tạo", "ngay tao", "ngày đặt", "ngay dat", "ngày tạo đơn"])
    c_status = find_col(df.columns, ["trạng thái", "trang thai", "status"])
    c_source = find_col(df.columns, ["nguồn", "nguon", "source"])
    # IMPORTANT: label must come ONLY from the exact Excel header "Nhãn".
    # Orders are allowed into DB only when this label exactly matches one of ALLOWED_LABELS
    # after trimming outer spaces. No fuzzy matching, no fallback column.
    c_label = find_exact_header_trimmed(df.columns, "Nhãn")
    c_emp = find_col(df.columns, ["nhân viên", "nhan vien", "sales"])
    # IMPORTANT: order_value must come ONLY from the exact Excel header "Giá trị đơn hàng".
    # No fallback to "Giá trị đơn", "Tổng tiền", "Khách phải trả", etc.
    c_value = find_exact_header_trimmed(df.columns, "Giá trị đơn hàng")
    # v2.12: shipping cost must come from the two exact Excel headers.
    # Net company shipping cost = "Phí vận chuyển" - "Phí ship báo khách".
    # No fuzzy fallback is allowed, to prevent silently using the wrong column.
    c_ship_cost = find_exact_header_trimmed(df.columns, "Phí vận chuyển")
    c_ship_customer = find_exact_header_trimmed(df.columns, "Phí ship báo khách")
    c_prod = find_col(df.columns, ["sản phẩm", "san pham", "tên sản phẩm", "ten san pham", "product"])
    c_sku = find_col(df.columns, ["mã sản phẩm", "ma san pham", "sku", "product code"])
    c_barcode = find_col(df.columns, ["mã vạch", "ma vach", "barcode", "ean"])
    c_qty = find_col(df.columns, ["số lượng", "so luong", "quantity"])
    c_price = find_col(df.columns, ["giá bán", "gia ban", "đơn giá", "don gia", "price"])

    required = {"order_id": c_id, "order_date/time": c_time, "order_value": c_value, "quantity": c_qty}
    missing = [name for name, col in required.items() if not col]
    if missing:
        raise ValueError(f"File {path.name} thiếu cột bắt buộc: {', '.join(missing)}")

    # CRITICAL: validate "Nhãn" and "Giá trị đơn hàng" on the ORIGINAL
    # order header row before any ffill. This prevents rows like order 764468655
    # from inheriting label/order_value from the previous order.
    df, _valid_id_map = build_valid_nhanh_order_scope(df, c_id, c_label, c_value, path)
    if df.empty:
        return df

    header_cols = [c for c in [c_id, c_time, c_status, c_source, c_label, c_emp, c_value, c_ship_cost, c_ship_customer] if c]
    for col in header_cols:
        # ffill only inside each validated order id, never across orders.
        df[col] = df.groupby("__external_order_id", dropna=False)[col].ffill()

    out = pd.DataFrame()
    out["external_order_id"] = df["__external_order_id"].map(clean_code)
    out["order_time"] = df[c_time].map(parse_datetime) if c_time else None
    out["order_date"] = out["order_time"].map(lambda x: x.date() if x else None)
    out["status"] = df[c_status].map(lambda x: safe_text(x, 200)) if c_status else ""
    # v2.12: exact order status matching. Trim outer spaces only.
    # Only these statuses are treated as cancellation/return for Nhanh operational reporting:
    # "Đã hủy", "Hệ Thống hủy", "Đã hoàn", "Khách hủy".
    out["status_exact"] = df[c_status].map(lambda x: exact_cell_text(x, 200)) if c_status else ""
    out["is_cancelled"] = out["status_exact"].isin(NHANH_NO_BO_STATUSES)
    out["source"] = df[c_source].map(lambda x: safe_text(x, 200)) if c_source else ""
    out["label"] = df[c_label].map(lambda x: safe_text(x, 200).strip())
    out["sales_employee"] = df[c_emp].map(lambda x: safe_text(x, 200)) if c_emp else ""
    # Store the exact value from Excel column "Giá trị đơn hàng" into nhanh.orders.order_value.
    # Do not alter it based on order status; if the Excel value is 0, DB gets 0.
    # If the Excel value is positive/negative, DB keeps that amount.
    out["order_value"] = money_series(df[c_value])
    # v2.12: company shipping/return cost for Nhanh.
    # Base shipping cost = "Phí vận chuyển" - "Phí ship báo khách".
    # If exact status is "Đã hoàn", add 25,000 VND return fee.
    ship_cost = money_series(df[c_ship_cost])
    ship_charged_to_customer = money_series(df[c_ship_customer])
    return_fee = out["status_exact"].eq("Đã hoàn").astype(float) * NHANH_RETURN_FEE_VND
    out["shipping_fee"] = ship_cost - ship_charged_to_customer + return_fee
    out["product_name"] = df[c_prod].map(lambda x: safe_text(x, 500)) if c_prod else ""
    out["product_code"] = df[c_sku].map(clean_code) if c_sku else ""
    out["barcode"] = df[c_barcode].map(clean_code) if c_barcode else ""
    out["quantity"] = money_series(df[c_qty]).astype(int) if c_qty else 1
    out["price"] = money_series(df[c_price]) if c_price else 0.0
    out["is_gift"] = out["price"].fillna(0).eq(0)
    out["raw_row"] = [row_to_json(r) for _, r in df.iterrows()]
    out["row_number"] = [int(i) + 2 for i in range(len(out))]

    # At this point all rows already belong to an order whose ORIGINAL header row
    # had an allowed label and non-blank Giá trị đơn hàng. Keep this defensive
    # assertion/filter in case a future edit changes the pipeline.
    out = out[(out["external_order_id"] != "") & out["label"].isin(ALLOWED_LABELS)].copy()
    return out


def load_orders_file(path: Path) -> Tuple[int, int]:
    df = process_order_file(path)
    conn = connect()
    cur = conn.cursor()
    batch_id = None
    rows_error = 0
    rows_loaded = 0
    try:
        batch_id = begin_batch(cur, "NHANH", "nhanh_orders", str(path), file_sha256(path))
        costs = load_cost_map(cur)

        valid_rows = []
        for _, row in df.iterrows():
            if not row["external_order_id"]:
                rows_error += 1
                log_import_error(cur, batch_id, "NHANH", "nhanh.orders", path.name, row.get("row_number"), "MISSING_ORDER_ID", "Thiếu mã đơn hàng.", row.get("raw_row"))
                continue
            if row["order_date"] is None:
                rows_error += 1
                log_import_error(cur, batch_id, "NHANH", "nhanh.orders", path.name, row.get("row_number"), "MISSING_ORDER_DATE", "Thiếu hoặc sai ngày đơn hàng.", row.get("raw_row"))
                continue
            if int(row.get("quantity", 0)) < 0:
                rows_error += 1
                log_import_error(cur, batch_id, "NHANH", "nhanh.order_items", path.name, row.get("row_number"), "NEGATIVE_QUANTITY", "Số lượng âm.", row.get("raw_row"))
                continue
            valid_rows.append(row)

        if not valid_rows:
            finish_batch(cur, batch_id, "success", rows_read=len(df), rows_loaded=0, rows_error=rows_error)
            conn.commit()
            return 0, rows_error

        valid_df = pd.DataFrame(valid_rows)
        # One order can have multiple item rows. Use the first occurrence after ffill so
        # order_value is taken from the order header column "Giá trị đơn hàng" once.
        orders_df = valid_df.drop_duplicates(subset=["external_order_id"], keep="first")
        order_values = [
            (
                r.external_order_id,
                r.order_time,
                r.order_date,
                r.status,
                bool(r.is_cancelled),
                r.source,
                r.label or "DEFAULT",
                r.sales_employee,
                safe_num(r.order_value),
                safe_num(r.shipping_fee),
                batch_id,
                path.name,
                Json(json_safe(r.raw_row)),
            )
            for r in orders_df.itertuples(index=False)
        ]

        returned = execute_values(
            cur,
            """
            INSERT INTO nhanh.orders(
                external_order_id, order_time, order_date, status, is_cancelled,
                source, label, sales_employee, order_value, shipping_fee,
                batch_id, source_file, raw_payload
            ) VALUES %s
            ON CONFLICT (external_order_id) DO UPDATE SET
                order_time = EXCLUDED.order_time,
                order_date = EXCLUDED.order_date,
                status = EXCLUDED.status,
                is_cancelled = EXCLUDED.is_cancelled,
                source = EXCLUDED.source,
                label = EXCLUDED.label,
                sales_employee = EXCLUDED.sales_employee,
                order_value = EXCLUDED.order_value,
                shipping_fee = EXCLUDED.shipping_fee,
                batch_id = EXCLUDED.batch_id,
                source_file = EXCLUDED.source_file,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = now()
            RETURNING external_order_id, id
            """,
            order_values,
            fetch=True,
            page_size=1000,
        )
        mapping = {str(ext): int(internal_id) for ext, internal_id in returned}

        if mapping:
            cur.execute("DELETE FROM nhanh.order_items WHERE order_id = ANY(%s)", (list(mapping.values()),))

        item_values = []
        line_no_by_order: Dict[str, int] = {}
        for r in valid_df.itertuples(index=False):
            order_id = mapping.get(r.external_order_id)
            if not order_id:
                continue
            line_no_by_order[r.external_order_id] = line_no_by_order.get(r.external_order_id, 0) + 1
            code = r.barcode or r.product_code
            item_values.append(
                (
                    order_id,
                    line_no_by_order[r.external_order_id],
                    r.product_name,
                    r.product_code or None,
                    r.barcode or None,
                    int(safe_num(r.quantity)),
                    safe_num(r.price),
                    bool(r.is_gift),
                    lookup_unit_cost(code, costs),
                    batch_id,
                    path.name,
                    Json(json_safe(r.raw_row)),
                )
            )

        if item_values:
            execute_values(
                cur,
                """
                INSERT INTO nhanh.order_items(
                    order_id, line_no, product_name, product_code, barcode,
                    quantity, price, is_gift, unit_cogs,
                    batch_id, source_file, raw_payload
                ) VALUES %s
                """,
                item_values,
                page_size=1000,
            )

        rows_loaded = len(order_values)
        finish_batch(cur, batch_id, "success", rows_read=len(df), rows_loaded=rows_loaded, rows_error=rows_error)
        conn.commit()
        archive_file(path, ORDER_OUT)
        print(f"✅ Nhanh orders {path.name}: {rows_loaded} đơn, {len(item_values)} dòng hàng, lỗi {rows_error}, batch_id={batch_id}")
        return rows_loaded, rows_error
    except Exception as e:
        conn.rollback()
        if batch_id:
            try:
                cur = conn.cursor()
                finish_batch(cur, batch_id, "failed", rows_read=len(df) if df is not None else 0, rows_loaded=rows_loaded, rows_error=rows_error, error_message=str(e)[:2000])
                conn.commit()
            except Exception:
                conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def load_ads_file(path: Path) -> Tuple[int, int]:
    df = read_smart_table(path, ["ngày", "date", "chi phí", "cost", "spent", "tiền quảng cáo"])
    conn = connect()
    cur = conn.cursor()
    batch_id = None
    rows_error = 0
    try:
        batch_id = begin_batch(cur, "NHANH", "nhanh_ads", str(path), file_sha256(path))
        c_date = find_col(df.columns, ["ngày", "date", "thời gian", "time"])
        c_cost = find_col(df.columns, ["tiền quảng cáo", "chi phí", "chi phi", "cost", "spent"])
        c_label = find_col(df.columns, ["nhãn", "label", "kênh", "kenh", "shop"])
        if not c_cost:
            raise ValueError(f"File ads {path.name} thiếu cột chi phí.")

        ads: Dict[Tuple[object, str], float] = {}
        for i, row in df.iterrows():
            ads_date = parse_date(row.get(c_date)) if c_date else None
            if ads_date is None:
                m = re.search(r"\d{4}[-_]\d{1,2}[-_]\d{1,2}|\d{1,2}[-_]\d{1,2}[-_]\d{4}", path.name)
                if m:
                    ads_date = parse_date(m.group().replace("_", "-"))
            if ads_date is None:
                rows_error += 1
                log_import_error(cur, batch_id, "NHANH", "nhanh.ads_costs", path.name, int(i) + 2, "MISSING_ADS_DATE", "Thiếu ngày quảng cáo.", row_to_json(row))
                continue
            cost = safe_num(row.get(c_cost))
            if cost < 0:
                rows_error += 1
                log_import_error(cur, batch_id, "NHANH", "nhanh.ads_costs", path.name, int(i) + 2, "NEGATIVE_ADS_COST", "Chi phí quảng cáo âm.", row_to_json(row))
                continue
            label = safe_text(row.get(c_label), 200) if c_label else "DEFAULT"
            label = label or "DEFAULT"
            key = (ads_date, label)
            ads[key] = ads.get(key, 0.0) + cost

        values = [(d, label, cost, batch_id, path.name, Json(json_safe({"source_file": path.name}))) for (d, label), cost in ads.items()]
        if values:
            execute_values(
                cur,
                """
                INSERT INTO nhanh.ads_costs(ads_date, shop_label, cost_vnd, batch_id, source_file, raw_payload)
                VALUES %s
                ON CONFLICT (ads_date, shop_label) DO UPDATE SET
                    cost_vnd = EXCLUDED.cost_vnd,
                    batch_id = EXCLUDED.batch_id,
                    source_file = EXCLUDED.source_file,
                    raw_payload = EXCLUDED.raw_payload,
                    updated_at = now()
                """,
                values,
                page_size=1000,
            )
        finish_batch(cur, batch_id, "success", rows_read=len(df), rows_loaded=len(values), rows_error=rows_error)
        conn.commit()
        archive_file(path, ADS_OUT)
        print(f"✅ Nhanh ads {path.name}: {len(values)} ngày/nhãn, lỗi {rows_error}, batch_id={batch_id}")
        return len(values), rows_error
    except Exception as e:
        conn.rollback()
        if batch_id:
            try:
                cur = conn.cursor()
                finish_batch(cur, batch_id, "failed", rows_read=len(df) if df is not None else 0, rows_loaded=0, rows_error=rows_error, error_message=str(e)[:2000])
                conn.commit()
            except Exception:
                conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def main() -> int:
    assert_production_schema()
    prepare_dirs()
    order_files = get_input_files(ORDER_DIR)
    ads_files = get_input_files(ADS_DIR)
    total_orders = total_ads = total_errors = 0

    for path in order_files:
        loaded, errors = load_orders_file(path)
        total_orders += loaded
        total_errors += errors
    for path in ads_files:
        loaded, errors = load_ads_file(path)
        total_ads += loaded
        total_errors += errors

    print("\n===== NHANH ETL SUMMARY =====")
    print(f"Order files: {len(order_files)} | orders loaded: {total_orders}")
    print(f"Ads files:   {len(ads_files)} | ads rows loaded: {total_ads}")
    print(f"Errors logged: {total_errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
