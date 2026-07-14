#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MT/GT ETL for DuLieu production schema.

Writes only to mtgt.* tables and reads COGS from dim.product_costs.
"""
from __future__ import annotations

import os
import re
import sys
import traceback
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from psycopg2.extras import execute_values, Json

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
    read_file,
    read_smart_table,
    row_to_json,
    safe_num,
    safe_text,
)

BASE_IN = Path(os.getenv("MTGT_BASE_IN", "data/mt_gt"))
BASE_OUT = Path(os.getenv("MTGT_BASE_OUT", "processed/mt_gt"))
SALES_DIR = BASE_IN / "1_sales_ledger"
MASTER_DIR = BASE_IN / "2_master_data"
SALES_OUT = BASE_OUT / "1_sales_ledger"
MASTER_OUT = BASE_OUT / "2_master_data"

MTGT_ETL_VERSION = "v4.23_mtgt_gross_doanh_so_ban_net_tong_thanh_toan"


def parse_period_from_filename(path: Path) -> date:
    # Parse MT/GT sales snapshot period from a filename.
    # Accepted examples:
    #   01_05_2026-GTMT.xlsx
    #   01-05-2026-GTMT.xlsx
    #   01.05.2026-MTGT.xlsx
    # The day must be 01 and the name must contain GTMT or MTGT.
    name = path.stem.upper().strip()
    m = re.search(r"(\d{1,2})[_\-.](\d{1,2})[_\-.](\d{4}).*(GTMT|MTGT)", name)
    if not m:
        raise ValueError(
            f"Tên file {path.name} không đúng format. "
            "Cần dạng 01_05_2026-GTMT.xlsx hoặc 01_05_2026-MTGT.xlsx."
        )

    day = int(m.group(1))
    month = int(m.group(2))
    year = int(m.group(3))

    if day != 1:
        raise ValueError(
            f"Tên file {path.name} phải bắt đầu bằng ngày 01 để thể hiện kỳ tháng. "
            f"Ví dụ: 01_{month:02d}_{year}-GTMT.xlsx"
        )

    try:
        return date(year, month, 1)
    except ValueError as exc:
        raise ValueError(f"Tên file {path.name} có tháng/năm không hợp lệ: {exc}") from exc


def next_month_start(period_month: date) -> date:
    if period_month.month == 12:
        return date(period_month.year + 1, 1, 1)
    return date(period_month.year, period_month.month + 1, 1)


def build_sales_period_map(sales_files: List[Path]) -> Dict[Path, date]:
    # Validate MT/GT sales files and ensure one file per period.
    period_by_file: Dict[Path, date] = {}
    files_by_period: Dict[date, List[Path]] = {}

    for path in sales_files:
        period = parse_period_from_filename(path)
        period_by_file[path] = period
        files_by_period.setdefault(period, []).append(path)

    duplicated = {
        period: files
        for period, files in files_by_period.items()
        if len(files) > 1
    }
    if duplicated:
        details = []
        for period, files in sorted(duplicated.items()):
            details.append(
                f"{period}: " + ", ".join(path.name for path in files)
            )
        raise ValueError(
            "Có nhiều file MT/GT sales cùng kỳ tháng. "
            "Vui lòng chỉ để lại một file chính thức cho mỗi tháng. "
            + " | ".join(details)
        )

    return period_by_file


def ensure_dirs() -> None:
    for p in [SALES_DIR, MASTER_DIR, SALES_OUT, MASTER_OUT]:
        p.mkdir(parents=True, exist_ok=True)


def normalize_channel(value: object) -> str:
    s = clean_code(value)
    if s in {"MT", "MODERNTRADE"}:
        return "MT"
    if s in {"GT", "GENERALTRADE"}:
        return "GT"
    return "OTHER"


def read_master_data() -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str], Dict[Tuple[object, str], float], List[dict], List[dict]]:
    """Return channel map, customer name map, employee channel map, ads, customer rows, employee rows."""
    channel_map: Dict[str, str] = {}
    customer_name_map: Dict[str, str] = {}
    employee_channel_map: Dict[str, str] = {}
    ads_map: Dict[Tuple[object, str], float] = {}
    customer_rows: List[dict] = []
    employee_rows: List[dict] = []

    for path in get_input_files(MASTER_DIR):
        try:
            df = read_file(path, dtype=object, header=0)
            df.columns = [safe_text(c) for c in df.columns]
        except Exception as e:
            print(f"⚠️ Không đọc được master {path.name}: {e}")
            continue
        fname = path.name.lower()

        if ("phân loại" in fname or "phan loai" in fname or "kenh" in fname) and not any(x in fname for x in ["ads", "quảng cáo", "quang cao", "chi phí", "chi phi"]):
            if len(df.columns) >= 2:
                for _, row in df.iterrows():
                    key = clean_code(row.iloc[0])
                    ch = normalize_channel(row.iloc[1])
                    if key:
                        channel_map[key] = ch
                print(f"✅ MTGT master kênh {path.name}: {len(channel_map)} key")

        elif "khách" in fname or "khach" in fname:
            c_code = find_col(df.columns, ["mã khách hàng", "ma khach hang", "customer code"]) or df.columns[0]
            c_name = find_col(df.columns, ["tên khách hàng", "ten khach hang", "customer name"]) or (df.columns[1] if len(df.columns) > 1 else None)
            c_channel = find_col(df.columns, ["kênh", "kenh", "channel"])
            for _, row in df.iterrows():
                key = clean_code(row.get(c_code))
                if not key:
                    continue
                name = safe_text(row.get(c_name, ""), 300) if c_name else ""
                customer_name_map[key] = name
                if c_channel:
                    customer_rows.append({"customer_key": key, "channel_group": normalize_channel(row.get(c_channel)), "customer_name": name})
            print(f"✅ MTGT master khách hàng {path.name}: {len(customer_name_map)} khách")

        elif "nhân viên" in fname or "nhan vien" in fname or "employee" in fname:
            c_code = find_col(df.columns, ["mã", "ma", "mã nhân viên", "employee code"]) or df.columns[0]
            c_channel = find_col(df.columns, ["kênh", "kenh", "channel"]) or (df.columns[1] if len(df.columns) > 1 else None)
            c_name = find_col(df.columns, ["tên", "ten", "name"])
            for _, row in df.iterrows():
                key = clean_code(row.get(c_code))
                if not key:
                    continue
                ch = normalize_channel(row.get(c_channel)) if c_channel else "OTHER"
                employee_channel_map[key] = ch
                employee_rows.append({"employee_key": key, "channel_group": ch, "employee_name": safe_text(row.get(c_name, ""), 300) if c_name else ""})
            print(f"✅ MTGT master nhân viên {path.name}: {len(employee_channel_map)} nhân viên")

        elif "quảng cáo" in fname or "quang cao" in fname or "ads" in fname or "chi phí" in fname or "chi phi" in fname:
            c_channel = find_col(df.columns, ["kênh", "kenh", "channel"])
            c_cost = find_col(df.columns, ["chi phí", "chi phi", "cost", "số tiền", "so tien"])
            c_date = find_col(df.columns, ["ngày", "ngay", "date"])
            if not c_channel or not c_cost or not c_date:
                print(f"⚠️ Bỏ qua ads MTGT {path.name}: cần cột kênh, chi phí, ngày")
                continue
            for _, row in df.iterrows():
                d = parse_date(row.get(c_date))
                ch = normalize_channel(row.get(c_channel))
                cost = safe_num(row.get(c_cost))
                if d is None or cost < 0:
                    continue
                ads_map[(d, ch)] = ads_map.get((d, ch), 0.0) + cost
            print(f"✅ MTGT ads master {path.name}: {len(ads_map)} key ngày/kênh")

    return channel_map, customer_name_map, employee_channel_map, ads_map, customer_rows, employee_rows


def load_master_maps(cur, batch_id: int, customer_rows: List[dict], employee_rows: List[dict]) -> None:
    if customer_rows:
        values = [(r["customer_key"], r["channel_group"], r.get("customer_name", "")) for r in customer_rows]
        execute_values(
            cur,
            """
            INSERT INTO mtgt.customer_channel_map(customer_key, channel_group, customer_name)
            VALUES %s
            ON CONFLICT (customer_key) DO UPDATE SET
                channel_group = EXCLUDED.channel_group,
                customer_name = EXCLUDED.customer_name,
                updated_at = now()
            """,
            values,
        )
    if employee_rows:
        values = [(r["employee_key"], r["channel_group"], r.get("employee_name", "")) for r in employee_rows]
        execute_values(
            cur,
            """
            INSERT INTO mtgt.employee_channel_map(employee_key, channel_group, employee_name)
            VALUES %s
            ON CONFLICT (employee_key) DO UPDATE SET
                channel_group = EXCLUDED.channel_group,
                employee_name = EXCLUDED.employee_name,
                updated_at = now()
            """,
            values,
        )


def load_ads(cur, batch_id: int, ads_map: Dict[Tuple[object, str], float]) -> int:
    values = [(d, ch, cost, batch_id, Json(json_safe({"source": "master_ads"}))) for (d, ch), cost in ads_map.items() if d and ch]
    if values:
        execute_values(
            cur,
            """
            INSERT INTO mtgt.ads_costs(ads_date, channel_group, cost_vnd, batch_id, raw_payload)
            VALUES %s
            ON CONFLICT (ads_date, channel_group) DO UPDATE SET
                cost_vnd = EXCLUDED.cost_vnd,
                batch_id = EXCLUDED.batch_id,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = now()
            """,
            values,
        )
    return len(values)


def _is_blank_or_total_row(row: pd.Series, c_date: Optional[str], c_doc: Optional[str], c_product_code: Optional[str], c_product_name: Optional[str]) -> bool:
    """Skip non-transaction rows such as blank helper rows and grand-total rows.

    The MT/GT sales ledger often contains:
    - one blank/helper row right under the header
    - one "Tổng cộng" summary row at the bottom
    These rows are not data errors and must not be logged to etl.import_errors.
    """
    values = [safe_text(v).strip() for v in row.tolist()]
    non_empty = [v for v in values if v and v.lower() not in {"nan", "none", "nat"}]
    joined = " ".join(non_empty).lower()

    if not non_empty:
        return True
    if "tổng cộng" in joined or "tong cong" in joined or joined == "total":
        return True

    date_raw = safe_text(row.get(c_date)) if c_date else ""
    doc_raw = safe_text(row.get(c_doc)) if c_doc else ""
    code_raw = safe_text(row.get(c_product_code)) if c_product_code else ""
    name_raw = safe_text(row.get(c_product_name)) if c_product_name else ""

    if not date_raw and not doc_raw and not code_raw and not name_raw:
        return True
    if not code_raw and "trường mở rộng" in joined:
        return True
    return False


def _is_cvc_line(product_code: str, product_name: str) -> bool:
    code = clean_code(product_code)
    name = safe_text(product_name).strip().lower()
    return code == "CVC" or "chi phí vận chuyển" in name or "chi phi van chuyen" in name


def _signed_revenue_from_qty_unit_price(qty_sale: float, qty_return: float, unit_price: float, fallback_amount: float) -> float:
    """Revenue rule approved by owner: revenue = signed quantity * unit price.

    Gift quantity is excluded from revenue. Returns are negative.
    If unit price is missing but the file contains an amount, fall back to that amount.
    """
    qty_revenue = safe_num(qty_sale) - safe_num(qty_return)
    unit_price = safe_num(unit_price)
    if unit_price != 0:
        return round(qty_revenue * unit_price, 0)
    return safe_num(fallback_amount)


def process_sales_file(
    path: Path,
    period_month: date,
    next_month: date,
    channel_map: Dict[str, str],
    customer_map: Dict[str, str],
    employee_channel_map: Dict[str, str],
    costs: Dict[str, float],
    cur,
    batch_id: int,
) -> Tuple[int, int]:
    df = read_smart_table(path, ["ngày chứng từ", "ngay chung tu", "số chứng từ", "so chung tu", "mã hàng", "ma hang"])
    if df.empty:
        return 0, 0

    c_date = find_col(df.columns, ["ngày chứng từ", "ngay chung tu", "ngày", "date"])
    c_doc = find_col(df.columns, ["số chứng từ", "so chung tu", "document"])
    c_customer_code = find_col(df.columns, ["mã khách hàng", "ma khach hang", "customer code"])
    c_customer_name = find_col(df.columns, ["tên khách hàng", "ten khach hang", "customer name"])
    c_employee_code = find_col(df.columns, ["mã nhân viên bán hàng", "ma nhan vien ban hang", "mã nhân viên", "ma nhan vien"])
    c_employee_name = find_col(df.columns, ["tên nhân viên bán hàng", "ten nhan vien ban hang", "tên nhân viên"])
    c_product_code = find_col(df.columns, ["mã hàng", "ma hang", "mã sản phẩm", "sku"])
    c_product_name = find_col(df.columns, ["tên hàng", "ten hang", "tên sản phẩm", "product name"])
    c_barcode = find_col(df.columns, ["mã vạch", "ma vach", "barcode", "ean"])
    c_qty_sale = find_col(df.columns, ["số lượng bán", "so luong ban", "sl bán", "sl ban", "quantity"])
    c_qty_gift = find_col(df.columns, ["sl bán khuyến mại", "sl ban khuyen mai", "khuyến mại", "khuyen mai"])
    c_qty_return = find_col(df.columns, ["tổng số lượng trả lại", "tong so luong tra lai", "sl trả", "sl tra", "return"])
    c_unit_price = find_col(df.columns, ["đơn giá", "don gia", "unit price", "price"])
    c_sales_amount = find_col(df.columns, ["doanh số bán", "doanh so ban", "sales amount"])
    c_total_payment = find_col(df.columns, ["tổng thanh toán", "tong thanh toan", "amount"])
    c_group = find_col(df.columns, ["tên nhóm khách hàng", "ten nhom khach hang", "nhóm khách hàng", "customer group"])
    c_project = find_col(df.columns, ["mã công trình", "ma cong trinh", "project"])

    required = {"ngày chứng từ": c_date, "số chứng từ": c_doc, "mã hàng": c_product_code, "doanh số bán": c_sales_amount, "tổng thanh toán": c_total_payment}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(f"File {path.name} thiếu cột bắt buộc: {', '.join(missing)}")

    rows = []
    errors = 0
    skipped_non_data = 0
    out_of_period_errors = 0

    for idx, row in df.iterrows():
        if _is_blank_or_total_row(row, c_date, c_doc, c_product_code, c_product_name):
            skipped_non_data += 1
            continue

        sale_date = parse_date(row.get(c_date))
        doc_no = clean_code(row.get(c_doc))
        product_code = clean_code(row.get(c_product_code))
        product_name = safe_text(row.get(c_product_name, ""), 500) if c_product_name else ""

        if not sale_date or not doc_no or not product_code:
            errors += 1
            log_import_error(cur, batch_id, "MTGT", "mtgt.sales_lines", path.name, int(idx) + 2, "MISSING_REQUIRED_FIELD", "Thiếu ngày, số chứng từ hoặc mã hàng.", row_to_json(row))
            continue

        if not (period_month <= sale_date < next_month):
            errors += 1
            out_of_period_errors += 1
            log_import_error(
                cur,
                batch_id,
                "MTGT",
                "mtgt.sales_lines",
                path.name,
                int(idx) + 2,
                "OUT_OF_PERIOD",
                f"Ngày chứng từ {sale_date} không thuộc kỳ {period_month} đến trước {next_month}.",
                row_to_json(row),
            )
            continue

        customer_code = clean_code(row.get(c_customer_code)) if c_customer_code else ""
        employee_code = clean_code(row.get(c_employee_code)) if c_employee_code else ""
        group_key = clean_code(row.get(c_group)) if c_group else ""
        project_key = clean_code(row.get(c_project)) if c_project else ""
        channel = channel_map.get(group_key) or channel_map.get(project_key) or employee_channel_map.get(employee_code) or "OTHER"
        channel = normalize_channel(channel)

        qty_sale = safe_num(row.get(c_qty_sale)) if c_qty_sale else 0.0
        qty_gift = safe_num(row.get(c_qty_gift)) if c_qty_gift else 0.0
        qty_return = safe_num(row.get(c_qty_return)) if c_qty_return else 0.0
        unit_price = safe_num(row.get(c_unit_price)) if c_unit_price else 0.0

        # Owner-approved production rule v4.23:
        # MT/GT gross_revenue MUST come directly from the Excel column "Doanh số bán".
        # MT/GT net_revenue MUST come directly from the Excel column "Tổng thanh toán".
        # Do not recompute revenue from quantity * unit_price.
        sales_amount = safe_num(row.get(c_sales_amount)) if c_sales_amount else 0.0
        total_payment = safe_num(row.get(c_total_payment)) if c_total_payment else 0.0

        is_cvc = _is_cvc_line(product_code, product_name)
        if is_cvc:
            line_type = "CVC"
            qty_net = 0.0
            gross_revenue = 0.0
            net_revenue = 0.0
            cvc_amount = abs(total_payment)
            unit_cogs = 0.0
        else:
            qty_net = qty_sale + qty_gift - qty_return
            gross_revenue = sales_amount*1.08
            net_revenue = total_payment
            cvc_amount = 0.0
            line_type = "RETURN" if (qty_return > 0 or qty_net < 0 or net_revenue < 0) else "SALE"
            barcode = clean_code(row.get(c_barcode)) if c_barcode else ""
            unit_cogs = lookup_unit_cost(barcode or product_code, costs)

        barcode = clean_code(row.get(c_barcode)) if c_barcode else ""
        if line_type != "CVC" and qty_net == 0 and gross_revenue == 0 and net_revenue == 0:
            # A real zero line is harmless but should not pollute reporting.
            skipped_non_data += 1
            continue

        rows.append({
            "sale_date": sale_date,
            "source_system": "MTGT",
            "channel_group": channel,
            "document_no": doc_no,
            "customer_code": customer_code or None,
            "customer_name": customer_map.get(customer_code) or safe_text(row.get(c_customer_name, ""), 300),
            "employee_code": employee_code or None,
            "employee_name": safe_text(row.get(c_employee_name, ""), 300) if c_employee_name else "",
            "product_code": product_code,
            "barcode": barcode or None,
            "product_name": product_name,
            "line_type": line_type,
            "quantity_sale": qty_sale,
            "quantity_gift": qty_gift,
            "quantity_return": qty_return,
            "unit_price": unit_price,
            "quantity": qty_net,
            "gross_revenue": gross_revenue,
            "discount_amount": 0.0,
            "net_revenue": net_revenue,
            "unit_cogs": unit_cogs,
            "cvc_amount": cvc_amount,
            "raw": row_to_json(row),
        })

    if out_of_period_errors > 0:
        raise ValueError(
            f"File {path.name} có {out_of_period_errors} dòng ngoài kỳ {period_month}. "
            "Dừng ETL để rollback và giữ nguyên dữ liệu cũ."
        )

    if not rows:
        print(f"ℹ️ MTGT {path.name}: bỏ qua {skipped_non_data} dòng không phải dữ liệu")
        return 0, errors

    # Monthly snapshot reload:
    # The file name determines the period. After the whole file has been
    # parsed and validated successfully, delete the existing MT/GT rows for
    # this month, then insert the current snapshot.
    cur.execute(
        """
        DELETE FROM mtgt.sales_lines
        WHERE source_system = 'MTGT'
          AND sale_date >= %s
          AND sale_date < %s
        """,
        (period_month, next_month),
    )
    deleted_rows = cur.rowcount if cur.rowcount is not None else 0
    print(
        f"🧹 MTGT {path.name}: deleted old period rows={deleted_rows}, "
        f"period={period_month} -> {next_month}"
    )

    line_by_doc: Dict[Tuple[object, str], int] = {}
    values = []
    for r in rows:
        k = (r["sale_date"], r["document_no"])
        line_by_doc[k] = line_by_doc.get(k, 0) + 1
        values.append((
            r["sale_date"], r["source_system"], r["channel_group"], r["document_no"], line_by_doc[k],
            r["customer_code"], r["customer_name"], r["employee_code"], r["employee_name"],
            r["product_code"], r["barcode"], r["product_name"], r["quantity"], r["gross_revenue"],
            r["discount_amount"], r["net_revenue"], r["unit_cogs"], r["line_type"],
            r["quantity_sale"], r["quantity_gift"], r["quantity_return"], r["unit_price"], r["cvc_amount"],
            batch_id, path.name, Json(json_safe(r["raw"])),
        ))

    execute_values(
        cur,
        """
        INSERT INTO mtgt.sales_lines(
            sale_date, source_system, channel_group, document_no, line_no,
            customer_code, customer_name, employee_code, employee_name,
            product_code, barcode, product_name, quantity, gross_revenue,
            discount_amount, net_revenue, unit_cogs, line_type,
            quantity_sale, quantity_gift, quantity_return, unit_price, cvc_amount,
            batch_id, source_file, raw_payload
        ) VALUES %s
        """,
        values,
        page_size=1000,
    )
    sale_lines = sum(1 for r in rows if r["line_type"] == "SALE")
    return_lines = sum(1 for r in rows if r["line_type"] == "RETURN")
    cvc_lines = sum(1 for r in rows if r["line_type"] == "CVC")
    print(
        f"ℹ️ MTGT {path.name}: SALE={sale_lines}, RETURN={return_lines}, CVC={cvc_lines}, "
        f"bỏ qua non-data={skipped_non_data}"
    )
    return len(values), errors

def run_etl() -> int:
    assert_production_schema()
    ensure_dirs()
    print("=" * 75)
    print(f"🚀 MT/GT ETL {MTGT_ETL_VERSION} -> mtgt.* + dim.product_costs")
    print("=" * 75)

    channel_map, customer_map, employee_channel_map, ads_map, customer_rows, employee_rows = read_master_data()
    sales_files = get_input_files(SALES_DIR)
    sales_periods = build_sales_period_map(sales_files)

    conn = connect()
    cur = conn.cursor()
    batch_id = None
    total_loaded = 0
    total_errors = 0
    try:
        batch_id = begin_batch(cur, "MTGT", "mtgt_etl", "multiple_files", None)
        costs = load_cost_map(cur)
        load_master_maps(cur, batch_id, customer_rows, employee_rows)
        ads_loaded = load_ads(cur, batch_id, ads_map)

        for path in sales_files:
            period_month = sales_periods[path]
            next_month = next_month_start(period_month)
            loaded, errors = process_sales_file(
                path,
                period_month,
                next_month,
                channel_map,
                customer_map,
                employee_channel_map,
                costs,
                cur,
                batch_id,
            )
            total_loaded += loaded
            total_errors += errors
            print(
                f"✅ MTGT sales {path.name}: period={period_month}, "
                f"{loaded} dòng, lỗi {errors}"
            )

        finish_batch(cur, batch_id, "success", rows_read=total_loaded + total_errors, rows_loaded=total_loaded + ads_loaded, rows_error=total_errors)
        conn.commit()
        for path in sales_files:
            period_month = sales_periods[path]
            archive_file(path, SALES_OUT / period_month.strftime("%Y-%m"))
        print(f"✅ DONE MTGT: sales={total_loaded}, ads={ads_loaded}, errors={total_errors}, batch_id={batch_id}")
        return 0
    except Exception as e:
        conn.rollback()
        if batch_id:
            try:
                cur = conn.cursor()
                finish_batch(cur, batch_id, "failed", rows_read=total_loaded + total_errors, rows_loaded=total_loaded, rows_error=total_errors, error_message=str(e)[:2000])
                conn.commit()
            except Exception:
                conn.rollback()
        print("❌ MTGT ETL failed. Files were NOT moved.")
        traceback.print_exc()
        return 1
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(run_etl())
