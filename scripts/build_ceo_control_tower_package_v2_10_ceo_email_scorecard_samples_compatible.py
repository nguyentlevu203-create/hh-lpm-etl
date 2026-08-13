from __future__ import annotations

import argparse
import importlib.util
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import psycopg2
from psycopg2.extras import RealDictCursor, Json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_TYPE = "CEO_DAILY_CONTROL_TOWER"

BASE_SCORECARD_BUILDER = PROJECT_ROOT / "scripts" / "build_ceo_control_tower_package_v2_scorecard.py"
SAMPLE_BUILDER = PROJECT_ROOT / "scripts" / "build_ceo_control_tower_package_v2_8_samples.py"
CEO_EMAIL_SCRIPT = PROJECT_ROOT / "scripts" / "send_staff_scorecard_emails.py"

CEO_EMAIL_SCORECARD_COLUMNS = [
    "stt",
    "kenh",
    "ty_le_thuc_hien",
    "chi_tieu_thang",
    "tong_thuc_hien_toi_ngay",
    "doanh_so_ngay",
    "chi_phi_quang_cao",
    "ty_le_quang_cao",
    "ty_le_quang_cao_luy_ke",
    "gia_von",
    "van_chuyen_phi_hoan",
    "backoffice",
    "live_in_house",
    "booking",
    "quang_cao",
    "bao_bi_dong_goi",
    "tien_thu",
    "ln_con_lai",
]

CEO_EMAIL_REQUIRED_COLUMNS = [
    "ty_le_quang_cao_luy_ke",
    "booking",
    "quang_cao",
    "ln_con_lai",
]

EMPLOYEE_SALES_COLUMNS = [
    "channel_code",
    "rank_in_channel",
    "employee_code",
    "employee_name",
    "is_temporary_code",
    "target_revenue",
    "mtd_revenue",
    "daily_revenue",
    "achievement_pct",
    "daily_orders",
    "mtd_orders",
    "mtd_quantity",
    "channel_contribution_pct",
    "mapping_status",
    "status_note",
]


def ensure_db_env() -> None:
    """Make scripts that read ETL_DB_PASSWORD/DB_PASSWORD work with PGPASSWORD."""
    if os.getenv("PGPASSWORD") and not os.getenv("ETL_DB_PASSWORD") and not os.getenv("DB_PASSWORD"):
        os.environ["ETL_DB_PASSWORD"] = os.getenv("PGPASSWORD") or ""
    if os.getenv("PGHOST") and not os.getenv("ETL_DB_HOST"):
        os.environ["ETL_DB_HOST"] = os.getenv("PGHOST") or ""
    if os.getenv("PGPORT") and not os.getenv("ETL_DB_PORT"):
        os.environ["ETL_DB_PORT"] = os.getenv("PGPORT") or ""
    if os.getenv("PGDATABASE") and not os.getenv("ETL_DB_NAME"):
        os.environ["ETL_DB_NAME"] = os.getenv("PGDATABASE") or ""
    if os.getenv("PGUSER") and not os.getenv("ETL_DB_USER"):
        os.environ["ETL_DB_USER"] = os.getenv("PGUSER") or ""


def load_module(path: Path, module_name: str):
    if not path.exists():
        raise FileNotFoundError(f"Missing required script: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import script: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def json_safe(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def as_float_safe(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return 0.0


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in dict(row).items():
        out[k] = json_safe(v)
    return out


def validate_ceo_email_scorecard_rows(rows: List[Dict[str, Any]]) -> None:
    """Fail package build if the daily email scorecard is not on the new schema."""
    if not rows:
        raise RuntimeError("CEO email scorecard returned no rows.")

    missing_by_col = {
        col
        for col in CEO_EMAIL_REQUIRED_COLUMNS
        if any(col not in row for row in rows)
    }
    if missing_by_col:
        missing = ", ".join(sorted(missing_by_col))
        raise RuntimeError(
            "CEO email scorecard is missing required v5 columns: "
            f"{missing}. Update scripts/send_staff_scorecard_emails.py before building the CEO package."
        )


def connect(cursor_factory=None):
    return psycopg2.connect(
        host=os.getenv("PGHOST") or os.getenv("ETL_DB_HOST") or os.getenv("DB_HOST") or "localhost",
        port=int(os.getenv("PGPORT") or os.getenv("ETL_DB_PORT") or os.getenv("DB_PORT") or "5433"),
        dbname=os.getenv("PGDATABASE") or os.getenv("ETL_DB_NAME") or os.getenv("DB_NAME") or "DuLieu",
        user=os.getenv("PGUSER") or os.getenv("ETL_DB_USER") or os.getenv("DB_USER") or "postgres",
        password=os.getenv("PGPASSWORD") or os.getenv("ETL_DB_PASSWORD") or os.getenv("DB_PASSWORD") or "",
        cursor_factory=cursor_factory,
    )




def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def table_exists(cur, schema: str, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.{table}",))
    row = cur.fetchone()
    return bool(row and row["ok"])


def get_table_columns(cur, schema: str, table: str) -> Set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        """,
        (schema, table),
    )
    return {str(r["column_name"]) for r in cur.fetchall()}


def pick_column(columns: Set[str], candidates: List[str]) -> Optional[str]:
    lower_map = {c.lower(): c for c in columns}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def text_expr(alias: str, col: Optional[str], default: str = "NULL") -> str:
    if col:
        return f"NULLIF(CAST({alias}.{quote_ident(col)} AS text), '')"
    return default


def numeric_expr(alias: str, col: Optional[str], default: str = "0") -> str:
    if col:
        return f"COALESCE({alias}.{quote_ident(col)}, {default})"
    return default


def parse_report_date(report_date: Any) -> date:
    if isinstance(report_date, date):
        return report_date
    return date.fromisoformat(str(report_date))


def fetch_tiktok_samples_compatible_with_old_sample_orders(cur, start_date: date, end_date: date, report_date: date) -> Dict[str, Any]:
    """
    Fetch TikTok sample orders from tiktok.orders_pnl and return a structure compatible
    with the legacy `sample_orders` block.

    Owner-approved rule:
    - TikTok sample order = not cancelled AND fee_base_revenue = 0.
    - CEO-facing sample_net_impact = ABS(platform_paid_or_unpaid - cogs_amount - packaging_cost).
    - ma_hang is seller_sku from tiktok.order_items when available.
    - If one order has multiple SKUs, allocate sample_net_impact by line COGS; if no line COGS, allocate by quantity.
    """
    if not table_exists(cur, "tiktok", "orders_pnl"):
        return {
            "total": {},
            "by_channel": [],
            "status_breakdown": [],
            "detail_by_item_date": [],
            "notes": ["Missing table tiktok.orders_pnl; cannot fetch TikTok sample orders."],
        }

    pnl_cols = get_table_columns(cur, "tiktok", "orders_pnl")
    required = {"order_date", "is_cancelled", "fee_base_revenue"}
    missing = sorted(required - pnl_cols)
    if missing:
        return {
            "total": {},
            "by_channel": [],
            "status_breakdown": [],
            "detail_by_item_date": [],
            "notes": [f"Missing required columns in tiktok.orders_pnl: {missing}"],
        }

    order_id_col = pick_column(pnl_cols, ["external_order_id", "order_id", "order_code", "ma_don_hang"])
    shop_col = pick_column(pnl_cols, ["shop_label", "shop", "shop_name", "store"])
    status_col = pick_column(pnl_cols, ["order_status", "status"])
    brand_col = pick_column(pnl_cols, ["brand_group", "brand"])
    qty_col = pick_column(pnl_cols, ["total_sku_qty", "qty_num", "quantity", "qty"])
    cogs_col = pick_column(pnl_cols, ["cogs_amount", "cogs", "line_cogs", "gia_von"])
    packaging_col = pick_column(pnl_cols, ["packaging_cost", "pack_cost", "bao_bi_dong_goi"])
    paid_col = pick_column(pnl_cols, ["settlement_paid", "set_paid", "paid_amount"])
    unpaid_col = pick_column(pnl_cols, ["settlement_unpaid", "set_unpaid", "unpaid_amount"])

    order_id_expr = f"COALESCE({text_expr('p', order_id_col)}, 'UNKNOWN')" if order_id_col else "'UNKNOWN'"
    shop_expr = f"COALESCE({text_expr('p', shop_col)}, 'TIKTOK')" if shop_col else "'TIKTOK'"
    status_expr = f"COALESCE({text_expr('p', status_col)}, '')" if status_col else "''"
    brand_expr = f"COALESCE({text_expr('p', brand_col)}, '')" if brand_col else "''"
    qty_expr = numeric_expr("p", qty_col, "0")
    cogs_expr = numeric_expr("p", cogs_col, "0")
    packaging_expr = numeric_expr("p", packaging_col, "0")
    paid_expr = numeric_expr("p", paid_col, "0")
    unpaid_expr = numeric_expr("p", unpaid_col, "0")

    has_items = table_exists(cur, "tiktok", "order_items")
    if has_items:
        item_cols = get_table_columns(cur, "tiktok", "order_items")
        item_order_col = pick_column(item_cols, ["external_order_id", "order_id", "order_code"])
        item_shop_col = pick_column(item_cols, ["shop_label", "shop", "shop_name"])
        item_sku_col = pick_column(item_cols, ["seller_sku", "ma_hang", "sku_code", "product_sku", "sku", "item_sku", "product_code"])
        item_qty_col = pick_column(item_cols, ["quantity", "qty", "qty_num"])
        item_unit_cogs_col = pick_column(item_cols, ["unit_cogs", "unit_cost", "gia_von", "cogs"])
        item_line_cogs_col = pick_column(item_cols, ["line_cogs", "cogs_amount"])
        if not item_order_col:
            has_items = False

    if has_items:
        item_join = f"""
            LEFT JOIN tiktok.order_items i
              ON {text_expr('i', item_order_col)} = s.external_order_id
             AND COALESCE({text_expr('i', item_shop_col)}, s.shop_label) = s.shop_label
        """
        item_sku_expr = f"COALESCE({text_expr('i', item_sku_col)}, s.external_order_id)" if item_sku_col else "s.external_order_id"
        item_qty_expr = numeric_expr("i", item_qty_col, "0")
        if item_line_cogs_col:
            item_line_cogs_expr = numeric_expr("i", item_line_cogs_col, "0")
        else:
            item_line_cogs_expr = f"({item_qty_expr}) * ({numeric_expr('i', item_unit_cogs_col, '0')})"
    else:
        item_join = ""
        item_sku_expr = "s.external_order_id"
        item_qty_expr = "s.order_qty"
        item_line_cogs_expr = "s.order_cogs"

    base_cte = f"""
        WITH sample_orders AS (
            SELECT
                {order_id_expr} AS external_order_id,
                {shop_expr} AS shop_label,
                p.order_date::date AS order_date,
                {status_expr} AS order_status,
                {brand_expr} AS brand_group,
                ({qty_expr})::numeric AS order_qty,
                ({cogs_expr})::numeric AS order_cogs,
                ({packaging_expr})::numeric AS packaging_cost,
                COALESCE(NULLIF(({paid_expr})::numeric, 0), NULLIF(({unpaid_expr})::numeric, 0), 0) AS platform_paid_or_unpaid,
                ABS(
                    COALESCE(NULLIF(({paid_expr})::numeric, 0), NULLIF(({unpaid_expr})::numeric, 0), 0)
                    - COALESCE(({cogs_expr})::numeric, 0)
                    - COALESCE(({packaging_expr})::numeric, 0)
                ) AS sample_net_impact
            FROM tiktok.orders_pnl p
            WHERE p.order_date BETWEEN %s AND %s
              AND COALESCE(p.is_cancelled, false) = false
              AND COALESCE(p.fee_base_revenue, 0) = 0
        ),
        item_lines AS (
            SELECT
                s.external_order_id,
                s.shop_label,
                s.order_date,
                s.order_status,
                s.brand_group,
                COALESCE(NULLIF(CAST(({item_sku_expr}) AS text), ''), s.external_order_id) AS ma_hang,
                COALESCE(({item_qty_expr})::numeric, 0) AS quantity,
                COALESCE(({item_line_cogs_expr})::numeric, 0) AS line_cogs,
                s.sample_net_impact
            FROM sample_orders s
            {item_join}
        ),
        weighted AS (
            SELECT
                *,
                SUM(line_cogs) OVER (PARTITION BY external_order_id, shop_label) AS order_line_cogs,
                SUM(quantity) OVER (PARTITION BY external_order_id, shop_label) AS order_qty
            FROM item_lines
        ),
        allocated AS (
            SELECT
                ma_hang,
                order_date AS ngay,
                order_status,
                brand_group,
                external_order_id,
                shop_label,
                quantity,
                line_cogs,
                CASE
                    WHEN order_line_cogs > 0 THEN sample_net_impact * line_cogs / NULLIF(order_line_cogs, 0)
                    WHEN order_qty > 0 THEN sample_net_impact * quantity / NULLIF(order_qty, 0)
                    ELSE sample_net_impact
                END AS sample_net_impact_allocated
            FROM weighted
        )
    """

    detail_sql = base_cte + """
        SELECT
            ma_hang,
            ngay,
            COUNT(DISTINCT external_order_id) AS sample_order_count,
            SUM(quantity) AS sample_qty,
            ROUND(SUM(line_cogs), 0) AS sample_cogs_raw,
            ROUND(SUM(sample_net_impact_allocated), 0) AS sample_net_impact
        FROM allocated
        GROUP BY ma_hang, ngay
        ORDER BY ngay, ma_hang
    """
    cur.execute(detail_sql, (start_date, end_date))
    detail_rows = [normalize_row(r) for r in cur.fetchall()]

    by_channel_sql = base_cte + """
        SELECT
            %s::date AS report_date,
            'TIKTOK'::text AS channel,
            'TIKTOK'::text AS source_channel,
            COALESCE(shop_label, 'TIKTOK') AS shop_label,
            COUNT(DISTINCT external_order_id) AS sample_order_count,
            SUM(quantity) AS sample_qty,
            COUNT(DISTINCT ma_hang) AS sample_sku_count,
            ROUND(SUM(sample_net_impact_allocated), 0) AS sample_cogs_amount,
            0::numeric AS cancelled_sample_order_count,
            NULL::numeric AS channel_net_sales,
            NULL::numeric AS sample_cogs_pct_of_channel_net_sales
        FROM allocated
        GROUP BY shop_label
        ORDER BY sample_cogs_amount DESC
    """
    cur.execute(by_channel_sql, (start_date, end_date, report_date))
    by_channel = [normalize_row(r) for r in cur.fetchall()]

    status_sql = base_cte + """
        SELECT
            %s::date AS report_date,
            'TIKTOK'::text AS channel,
            COALESCE(order_status, '') AS order_status,
            COUNT(DISTINCT external_order_id) AS sample_order_count,
            SUM(quantity) AS sample_qty,
            ROUND(SUM(sample_net_impact_allocated), 0) AS sample_cogs_amount
        FROM allocated
        GROUP BY order_status
        ORDER BY sample_cogs_amount DESC
    """
    cur.execute(status_sql, (start_date, end_date, report_date))
    status_breakdown = [normalize_row(r) for r in cur.fetchall()]

    total = {
        "report_date": report_date.isoformat(),
        "sample_order_count": sum(float(r.get("sample_order_count") or 0) for r in by_channel),
        "sample_qty": sum(float(r.get("sample_qty") or 0) for r in by_channel),
        "sample_sku_count": len({r.get("ma_hang") for r in detail_rows if r.get("ma_hang")}),
        "sample_cogs_amount": sum(float(r.get("sample_net_impact") or 0) for r in detail_rows),
        "total_net_sales": None,
        "sample_cogs_pct_of_total_net_sales": None,
    }

    return {
        "total": total,
        "by_channel": by_channel,
        "status_breakdown": status_breakdown,
        "detail_by_item_date": detail_rows,
        "ceo_columns": ["ma_hang", "ngay", "sample_net_impact"],
        "rule": "TikTok sample orders from tiktok.orders_pnl where is_cancelled=false and fee_base_revenue=0.",
        "sample_net_impact_policy": "CEO-facing positive sample cost impact = ABS(platform paid/unpaid - COGS - packaging).",
        "allocation_policy": "If multiple SKUs in one order, allocate sample_net_impact by line COGS; if unavailable, by quantity.",
        "ma_hang_source": "tiktok.order_items.seller_sku if available; otherwise external_order_id fallback.",
    }


def merge_tiktok_orders_pnl_samples_into_sample_orders(report_date: str, sample_orders_block: Any) -> Dict[str, Any]:
    """Keep legacy block name `sample_orders` and replace/merge TikTok sample logic inside it."""
    report_dt = parse_report_date(report_date)
    current_month_start = report_dt.replace(day=1)

    if not isinstance(sample_orders_block, dict):
        sample_orders_block = {
            "report_date": report_dt.isoformat(),
            "source": "sample_orders",
            "scope": "daily_and_mtd",
            "total": {},
            "by_channel": [],
            "status_breakdown": [],
            "mtd": {
                "from_date": current_month_start.isoformat(),
                "to_date": report_dt.isoformat(),
                "by_channel": [],
                "total": {},
            },
            "notes": [],
        }

    conn = connect(cursor_factory=RealDictCursor)
    cur = conn.cursor()
    try:
        daily_tiktok = fetch_tiktok_samples_compatible_with_old_sample_orders(cur, report_dt, report_dt, report_dt)
        mtd_tiktok = fetch_tiktok_samples_compatible_with_old_sample_orders(cur, current_month_start, report_dt, report_dt)
    finally:
        cur.close()
        conn.close()

    # Remove old TikTok rows to avoid double count, keep all other channels from legacy sample_orders.
    old_by_channel = [
        r for r in sample_orders_block.get("by_channel", [])
        if str(r.get("channel", "")).upper() != "TIKTOK"
    ]
    old_status = [
        r for r in sample_orders_block.get("status_breakdown", [])
        if str(r.get("channel", "")).upper() != "TIKTOK"
    ]

    sample_orders_block["by_channel"] = old_by_channel + daily_tiktok.get("by_channel", [])
    sample_orders_block["status_breakdown"] = old_status + daily_tiktok.get("status_breakdown", [])
    sample_orders_block["total"] = {
        "report_date": report_dt.isoformat(),
        "sample_order_count": sum(float(r.get("sample_order_count") or 0) for r in sample_orders_block["by_channel"]),
        "sample_qty": sum(float(r.get("sample_qty") or 0) for r in sample_orders_block["by_channel"]),
        "sample_sku_count": None,
        "sample_cogs_amount": sum(float(r.get("sample_cogs_amount") or 0) for r in sample_orders_block["by_channel"]),
        "total_net_sales": None,
        "sample_cogs_pct_of_total_net_sales": None,
    }

    sample_orders_block.setdefault("mtd", {})
    sample_orders_block["mtd"]["from_date"] = current_month_start.isoformat()
    sample_orders_block["mtd"]["to_date"] = report_dt.isoformat()
    old_mtd_by_channel = [
        r for r in sample_orders_block["mtd"].get("by_channel", [])
        if str(r.get("channel", "")).upper() != "TIKTOK"
    ]
    sample_orders_block["mtd"]["by_channel"] = old_mtd_by_channel + mtd_tiktok.get("by_channel", [])
    sample_orders_block["mtd"]["total"] = {
        "from_date": current_month_start.isoformat(),
        "to_date": report_dt.isoformat(),
        "sample_order_count": sum(float(r.get("sample_order_count") or 0) for r in sample_orders_block["mtd"]["by_channel"]),
        "sample_qty": sum(float(r.get("sample_qty") or 0) for r in sample_orders_block["mtd"]["by_channel"]),
        "sample_sku_count": None,
        "sample_cogs_amount": sum(float(r.get("sample_cogs_amount") or 0) for r in sample_orders_block["mtd"]["by_channel"]),
        "total_net_sales": None,
        "sample_cogs_pct_of_total_net_sales": None,
    }
    sample_orders_block["mtd"]["detail_by_item_date"] = mtd_tiktok.get("detail_by_item_date", [])
    sample_orders_block["mtd"]["ceo_columns"] = ["ma_hang", "ngay", "sample_net_impact"]
    sample_orders_block["mtd"]["tiktok_sample_policy"] = {
        "rule": mtd_tiktok.get("rule"),
        "sample_net_impact_policy": mtd_tiktok.get("sample_net_impact_policy"),
        "allocation_policy": mtd_tiktok.get("allocation_policy"),
        "ma_hang_source": mtd_tiktok.get("ma_hang_source"),
    }

    sample_orders_block.setdefault("notes", [])
    sample_orders_block["notes"].append(
        "TikTok sample orders are merged into legacy sample_orders block from tiktok.orders_pnl where is_cancelled=false and fee_base_revenue=0."
    )
    sample_orders_block["notes"].append(
        "TikTok sample_cogs_amount / sample_net_impact are CEO-facing positive values = ABS(platform paid/unpaid - COGS - packaging)."
    )
    sample_orders_block["notes"].append(
        "TikTok item-level MTD detail is available at sample_orders.mtd.detail_by_item_date with columns ma_hang, ngay, sample_net_impact."
    )
    return sample_orders_block


def build_base_package(report_date: str) -> Dict[str, Any]:
    score_mod = load_module(BASE_SCORECARD_BUILDER, "base_scorecard_builder")
    pkg = score_mod.build_package(report_date)

    if SAMPLE_BUILDER.exists():
        sample_mod = load_module(SAMPLE_BUILDER, "sample_builder")
        try:
            pkg["sample_orders"] = sample_mod.fetch_sample_orders_block(report_date, pkg)
            pkg.setdefault("metadata", {})["sample_orders_policy"] = "multi_channel_v2_8_plus_tiktok_orders_pnl"
        except Exception as exc:
            pkg["sample_orders"] = {
                "error": str(exc),
                "notes": ["Could not enrich legacy sample_orders. Check sample order migration/import."],
            }

    try:
        pkg["sample_orders"] = merge_tiktok_orders_pnl_samples_into_sample_orders(
            report_date,
            pkg.get("sample_orders"),
        )
        pkg.setdefault("metadata", {})["sample_orders_policy"] = (
            "legacy_sample_orders_block_with_tiktok_orders_pnl_rule_not_cancelled_fee_base_zero_abs_net_impact"
        )
    except Exception as exc:
        pkg.setdefault("sample_orders", {})
        if isinstance(pkg["sample_orders"], dict):
            pkg["sample_orders"].setdefault("notes", [])
            pkg["sample_orders"]["notes"].append(
                f"Could not merge TikTok sample orders from tiktok.orders_pnl: {exc}"
            )
    return pkg

def fetch_ceo_email_scorecard(report_date: str) -> Dict[str, Any]:
    """Use exactly the same DB logic as the daily CEO email script."""
    ensure_db_env()
    ceo_mod = load_module(CEO_EMAIL_SCRIPT, "send_staff_scorecard_emails_runtime")

    raw_rows = ceo_mod.get_scorecard_rows(report_date)
    full_rows = ceo_mod.filter_rows_by_scope(raw_rows, "FULL")

    rows = [normalize_row(r) for r in full_rows]
    validate_ceo_email_scorecard_rows(rows)
    return {
        "report_date": report_date,
        "source": "scripts/send_staff_scorecard_emails.py::get_scorecard_rows + filter_rows_by_scope(FULL)",
        "purpose": "Render the CEO daily email scorecard form with exact MTD/target/cost logic.",
        "rows": rows,
        "columns": CEO_EMAIL_SCORECARD_COLUMNS,
        "cost_policy": {
            "source": "scripts/send_staff_scorecard_emails.py",
            "booking_policy": "booking_v6_actual_tiktok_only",
            "fixed_booking_pool_vnd": 0,
            "fixed_booking_channels": [],
            "fixed_booking_per_channel_vnd": 0,
            "tiktok_extra_fixed_booking_vnd": 0,
            "channel_booking_rule": {
                "MT": 0,
                "GT": 0,
                "DIGITAL": 0,
                "SHOPEE": 0,
                "TIKTOK": "Use only tiktok.orders_pnl.booking_fee via send_staff_scorecard_emails.py",
            },
            "ads_ratio_daily_column": "ty_le_quang_cao",
            "ads_ratio_cumulative_column": "ty_le_quang_cao_luy_ke",
            "do_not_recalculate_in_report": True,
        },
        "rendering_notes": [
            "This block is the only source for CEO Scorecard Form.",
            "Do not recreate CEO Scorecard Form from scorecard_by_channel.",
            "This block includes MTD, monthly targets, ECOM grouping, child rows Shopee/TikTok, and total row.",
            "This block includes cumulative ads ratio and booking policy v6 from send_staff_scorecard_emails.py: MT/GT/DIGITAL/SHOPEE booking = 0; TikTok uses only actual tiktok.orders_pnl.booking_fee.",
        ],
    }


def fetch_employee_sales_scorecard(report_date: str) -> Dict[str, Any]:
    """Fetch MT/GT employee target and sales detail for CEO analysis.

    The target table is the complete employee list. Actual sales from mtgt.sales_lines
    are joined into it so employees with no sales still appear in the CEO report.
    """
    report_dt = parse_report_date(report_date)
    month_start = report_dt.replace(day=1)
    warnings: List[str] = []

    conn = connect(cursor_factory=RealDictCursor)
    cur = conn.cursor()
    try:
        has_employees = table_exists(cur, "ops", "sales_employees")
        has_targets = table_exists(cur, "ops", "monthly_employee_sales_targets")
        if not has_employees or not has_targets:
            missing = []
            if not has_employees:
                missing.append("ops.sales_employees")
            if not has_targets:
                missing.append("ops.monthly_employee_sales_targets")
            return {
                "report_date": report_date,
                "from_date": month_start.isoformat(),
                "to_date": report_dt.isoformat(),
                "source": "mtgt.sales_lines + ops.monthly_employee_sales_targets",
                "scope": "MT/GT employee-level sales MTD and daily",
                "rows": [],
                "columns": EMPLOYEE_SALES_COLUMNS,
                "data_quality": {
                    "status": "WARN",
                    "warnings": [f"Missing required target table(s): {', '.join(missing)}."],
                },
                "rendering_notes": [
                    "Do not render an employee table when rows is empty.",
                    "Create/import ops.sales_employees and ops.monthly_employee_sales_targets first.",
                ],
            }

        sql = """
        WITH params AS (
            SELECT
                %s::date AS report_date,
                date_trunc('month', %s::date)::date AS month_start
        ),

        actual AS (
            SELECT
                s.channel_group AS channel_code,
                COALESCE(NULLIF(s.employee_code, ''), 'UNASSIGNED') AS employee_code,
                COALESCE(NULLIF(s.employee_name, ''), 'Chưa gán nhân viên') AS employee_name,
                SUM(
                    CASE
                        WHEN s.line_type <> 'CVC' AND s.sale_date = p.report_date
                        THEN COALESCE(s.net_revenue, 0)
                        ELSE 0
                    END
                ) AS daily_revenue,
                SUM(
                    CASE
                        WHEN s.line_type <> 'CVC'
                        THEN COALESCE(s.net_revenue, 0)
                        ELSE 0
                    END
                ) AS mtd_revenue,
                COUNT(DISTINCT s.document_no) FILTER (
                    WHERE s.line_type <> 'CVC'
                      AND s.sale_date = p.report_date
                ) AS daily_orders,
                COUNT(DISTINCT s.document_no) FILTER (
                    WHERE s.line_type <> 'CVC'
                ) AS mtd_orders,
                SUM(
                    CASE
                        WHEN s.line_type <> 'CVC'
                        THEN COALESCE(s.quantity, 0)
                        ELSE 0
                    END
                ) AS mtd_quantity
            FROM mtgt.sales_lines s
            CROSS JOIN params p
            WHERE s.sale_date BETWEEN p.month_start AND p.report_date
              AND s.channel_group IN ('MT', 'GT')
            GROUP BY
                s.channel_group,
                COALESCE(NULLIF(s.employee_code, ''), 'UNASSIGNED'),
                COALESCE(NULLIF(s.employee_name, ''), 'Chưa gán nhân viên')
        ),

        target_base AS (
            SELECT
                e.employee_id,
                e.channel_code,
                e.employee_code,
                e.employee_name,
                COALESCE(e.is_temporary_code, false) AS is_temporary_code,
                COALESCE(t.target_revenue, 0) AS target_revenue
            FROM ops.monthly_employee_sales_targets t
            JOIN ops.sales_employees e ON e.employee_id = t.employee_id
            CROSS JOIN params p
            WHERE t.target_month = p.month_start
              AND t.is_enabled = true
              AND e.is_enabled = true
              AND e.channel_code IN ('MT', 'GT')
        ),

        joined AS (
            SELECT
                COALESCE(tb.channel_code, a.channel_code) AS channel_code,
                COALESCE(tb.employee_code, a.employee_code) AS employee_code,
                COALESCE(tb.employee_name, a.employee_name) AS employee_name,
                COALESCE(tb.is_temporary_code, false) AS is_temporary_code,
                COALESCE(tb.target_revenue, 0) AS target_revenue,
                COALESCE(a.daily_revenue, 0) AS daily_revenue,
                COALESCE(a.mtd_revenue, 0) AS mtd_revenue,
                COALESCE(a.daily_orders, 0) AS daily_orders,
                COALESCE(a.mtd_orders, 0) AS mtd_orders,
                COALESCE(a.mtd_quantity, 0) AS mtd_quantity,
                CASE
                    WHEN a.employee_code IS NULL THEN 'TARGET_NO_SALES'
                    WHEN tb.employee_code IS NULL THEN 'SALES_NO_TARGET'
                    WHEN a.employee_code = 'UNASSIGNED' THEN 'UNASSIGNED_SALES'
                    ELSE 'OK'
                END AS mapping_status
            FROM target_base tb
            FULL OUTER JOIN actual a
              ON a.channel_code = tb.channel_code
             AND a.employee_code = tb.employee_code
        ),

        ranked AS (
            SELECT
                j.*,
                SUM(j.mtd_revenue) OVER (PARTITION BY j.channel_code) AS channel_mtd_revenue,
                RANK() OVER (
                    PARTITION BY j.channel_code
                    ORDER BY j.mtd_revenue DESC, j.employee_name
                ) AS rank_in_channel
            FROM joined j
        )

        SELECT
            channel_code,
            rank_in_channel,
            employee_code,
            employee_name,
            is_temporary_code,
            target_revenue,
            mtd_revenue,
            daily_revenue,
            CASE
                WHEN target_revenue = 0 THEN NULL
                ELSE ROUND(mtd_revenue / NULLIF(target_revenue, 0) * 100, 1)
            END AS achievement_pct,
            daily_orders,
            mtd_orders,
            mtd_quantity,
            CASE
                WHEN channel_mtd_revenue = 0 THEN NULL
                ELSE ROUND(mtd_revenue / NULLIF(channel_mtd_revenue, 0) * 100, 1)
            END AS channel_contribution_pct,
            mapping_status,
            CASE
                WHEN mapping_status = 'TARGET_NO_SALES' THEN 'Có chỉ tiêu nhưng chưa có doanh số'
                WHEN mapping_status = 'SALES_NO_TARGET' THEN 'Có doanh số nhưng chưa có chỉ tiêu'
                WHEN mapping_status = 'UNASSIGNED_SALES' THEN 'Doanh số thiếu mã nhân viên'
                WHEN is_temporary_code THEN 'Đang dùng mã nhân viên tạm'
                WHEN target_revenue = 0 THEN 'Chưa giao chỉ tiêu'
                WHEN mtd_revenue >= target_revenue THEN 'Đạt/vượt chỉ tiêu'
                WHEN daily_revenue = 0 THEN 'Không phát sinh doanh số ngày'
                ELSE 'Đang thực hiện'
            END AS status_note
        FROM ranked
        ORDER BY channel_code, rank_in_channel, employee_name;
        """
        cur.execute(sql, (report_date, report_date))
        rows = [normalize_row(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    if not rows:
        warnings.append("employee_sales_scorecard returned no rows.")
    if any(r.get("mapping_status") == "SALES_NO_TARGET" for r in rows):
        warnings.append("Some employees have sales but no monthly target.")
    if any(r.get("mapping_status") == "TARGET_NO_SALES" for r in rows):
        warnings.append("Some employees have monthly target but no sales.")
    if any(r.get("mapping_status") == "UNASSIGNED_SALES" for r in rows):
        warnings.append("Some sales rows are missing employee_code.")
    if any(bool(r.get("is_temporary_code")) for r in rows):
        warnings.append("Some employees are using temporary employee codes.")
    if any(as_float_safe(r.get("mtd_quantity")) > 1_000_000 for r in rows):
        warnings.append("Some employee mtd_quantity values look unusually high; check MT/GT quantity ETL.")

    return {
        "report_date": report_date,
        "from_date": month_start.isoformat(),
        "to_date": report_dt.isoformat(),
        "source": "mtgt.sales_lines + ops.sales_employees + ops.monthly_employee_sales_targets",
        "scope": "MT/GT employee-level sales MTD and daily",
        "rows": rows,
        "columns": EMPLOYEE_SALES_COLUMNS,
        "data_quality": {
            "status": "WARN" if warnings else "PASS",
            "warnings": warnings,
        },
        "rendering_notes": [
            "Render this block immediately after Scorecard theo kênh.",
            "Use this block for employee-level MT/GT sales detail.",
            "Do not mix this block into ceo_email_scorecard.rows.",
            "Do not infer employee performance for Shopee/TikTok/Nhanh from this block.",
            "If target_revenue is 0 or null, do not say the employee failed KPI.",
        ],
    }


def apply_rendering_policy(pkg: Dict[str, Any]) -> None:
    pkg.setdefault("metadata", {})["package_variant"] = "v2_10_ceo_email_scorecard_sample_orders_compatible"
    pkg.setdefault("instructions_for_agent", {})
    pkg["instructions_for_agent"].update({
        "ceo_email_scorecard_source": "ceo_email_scorecard.rows",
        "scorecard_analysis_source": "scorecard_by_channel",
        "employee_sales_scorecard_source": "employee_sales_scorecard.rows",
        "employee_sales_scorecard_position": "after_scorecard_by_channel",
        "do_not_use_channel_breakdown_legacy": True,
        "do_not_recalculate_ceo_email_scorecard": True,
        "do_not_recalculate_employee_sales_scorecard": True,
        "hide_scorecard_columns": ["discount_voucher_return"],
        "removed_scorecard_column": "Discount/Voucher/Return",
        "ceo_email_scorecard_columns": CEO_EMAIL_SCORECARD_COLUMNS,
        "ceo_email_scorecard_cost_policy": "Use booking/quang_cao/ty_le_quang_cao_luy_ke directly from ceo_email_scorecard.rows. Booking policy v6: no fixed booking pool; TikTok uses only actual booking_fee.",
        "employee_sales_scorecard_columns": EMPLOYEE_SALES_COLUMNS,
    })
    pkg["report_rendering_policy"] = {
        "scorecard_by_channel": {
            "source": "scorecard_by_channel",
            "hide_columns": ["discount_voucher_return"],
            "display_columns": [
                "channel",
                "gross_sales",
                "net_sales",
                "cogs",
                "gross_margin",
                "gm_pct",
                "platform_fees",
                "shipping_return_fee",
                "ads_cost",
                "ads_to_sales_pct",
                "live_cost",
                "booking_fee",
                "backoffice_fee",
                "packaging_cost",
                "payout",
                "profit",
                "profit_margin_pct",
                "alert",
                "action",
            ],
            "note": "Discount/Voucher/Return is kept in raw package if present for audit, but must not be rendered in the report table.",
        },
        "ceo_email_scorecard": {
            "source": "ceo_email_scorecard.rows",
            "must_use_exact_rows": True,
            "display_columns": CEO_EMAIL_SCORECARD_COLUMNS,
            "note": "Use this block to render the CEO Email Scorecard Form; do not derive it from scorecard_by_channel or recalculate booking/ads ratios. Booking already follows v6 in these rows.",
        },
        "employee_sales_scorecard": {
            "source": "employee_sales_scorecard.rows",
            "must_use_exact_rows": True,
            "position": "after_scorecard_by_channel",
            "title": "Chi tiết doanh số nhân viên theo kênh",
            "display_columns": EMPLOYEE_SALES_COLUMNS,
            "note": (
                "Render this table immediately after Scorecard theo kênh. "
                "This block currently covers MT/GT employee sales only. "
                "Do not recalculate from scorecard_by_channel or ceo_email_scorecard."
            ),
        },
    }


def fmt_money(v: Any) -> str:
    try:
        return f"{float(v or 0):,.0f}"
    except Exception:
        return "0"


def fmt_pct(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.1f}%"
    except Exception:
        return "-"


def markdown_ceo_email_scorecard(block: Dict[str, Any]) -> str:
    lines = []
    lines.append("## CEO Email Scorecard Form")
    lines.append("")
    lines.append("| STT | Kênh | % thực hiện | Chỉ tiêu tháng | Tổng thực hiện tới ngày | Doanh số ngày | Chi phí QC | % QC/DS ngày | % QC lũy kế / DS | Giá vốn | VC/Phí hoàn | Backoffice | Live | Booking | Quảng cáo | Bao bì | Tiền thu | LN còn lại |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in block.get("rows", []):
        lines.append(
            f"| {r.get('stt','')} | {r.get('kenh','')} | {fmt_pct(r.get('ty_le_thuc_hien'))} | "
            f"{fmt_money(r.get('chi_tieu_thang'))} | {fmt_money(r.get('tong_thuc_hien_toi_ngay'))} | "
            f"{fmt_money(r.get('doanh_so_ngay'))} | {fmt_money(r.get('chi_phi_quang_cao'))} | "
            f"{fmt_pct(r.get('ty_le_quang_cao'))} | {fmt_pct(r.get('ty_le_quang_cao_luy_ke'))} | "
            f"{fmt_money(r.get('gia_von'))} | {fmt_money(r.get('van_chuyen_phi_hoan'))} | {fmt_money(r.get('backoffice'))} | "
            f"{fmt_money(r.get('live_in_house'))} | {fmt_money(r.get('booking'))} | {fmt_money(r.get('quang_cao'))} | "
            f"{fmt_money(r.get('bao_bi_dong_goi'))} | {fmt_money(r.get('tien_thu'))} | {fmt_money(r.get('ln_con_lai'))} |"
        )
    return "\n".join(lines) + "\n"


def markdown_scorecard_without_discount(pkg: Dict[str, Any]) -> str:
    lines = []
    lines.append("## Scorecard Theo Kênh - Không Hiển Thị Discount/Voucher/Return")
    lines.append("")
    lines.append("| Channel | Gross Sales | Net Sales | COGS | Gross Margin | GM% | Platform | Ship/Return | Ads | Ads/DS | Live | Booking | Backoffice | Packaging | Payout | Profit | Profit Margin |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in pkg.get("scorecard_by_channel", []):
        lines.append(
            f"| {r.get('channel')} | {fmt_money(r.get('gross_sales'))} | {fmt_money(r.get('net_sales'))} | "
            f"{fmt_money(r.get('cogs'))} | {fmt_money(r.get('gross_margin'))} | {fmt_pct(r.get('gm_pct'))} | "
            f"{fmt_money(r.get('platform_fees'))} | {fmt_money(r.get('shipping_return_fee'))} | {fmt_money(r.get('ads_cost'))} | "
            f"{fmt_pct(r.get('ads_to_sales_pct'))} | {fmt_money(r.get('live_cost'))} | {fmt_money(r.get('booking_fee'))} | "
            f"{fmt_money(r.get('backoffice_fee'))} | {fmt_money(r.get('packaging_cost'))} | {fmt_money(r.get('payout'))} | "
            f"{fmt_money(r.get('profit'))} | {fmt_pct(r.get('profit_margin_pct'))} |"
        )
    return "\n".join(lines) + "\n"


def markdown_employee_sales_scorecard(block: Dict[str, Any]) -> str:
    lines = []
    lines.append("## Chi tiết doanh số nhân viên theo kênh")
    lines.append("")
    dq = block.get("data_quality", {})
    if dq.get("warnings"):
        lines.append(f"- Data quality: **{dq.get('status')}**")
        for warning in dq.get("warnings", []):
            lines.append(f"- Warning: {warning}")
        lines.append("")
    lines.append("| Kênh | Rank | Mã NV | Nhân viên | Mã tạm | Chỉ tiêu | MTD | Ngày | % TH | Đơn ngày | Đơn MTD | Tỷ trọng kênh | Trạng thái |")
    lines.append("|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in block.get("rows", []):
        lines.append(
            f"| {r.get('channel_code','')} | {r.get('rank_in_channel','')} | {r.get('employee_code','')} | "
            f"{r.get('employee_name','')} | {'Y' if r.get('is_temporary_code') else ''} | "
            f"{fmt_money(r.get('target_revenue'))} | {fmt_money(r.get('mtd_revenue'))} | "
            f"{fmt_money(r.get('daily_revenue'))} | {fmt_pct(r.get('achievement_pct'))} | "
            f"{fmt_money(r.get('daily_orders'))} | {fmt_money(r.get('mtd_orders'))} | "
            f"{fmt_pct(r.get('channel_contribution_pct'))} | {r.get('status_note','')} |"
        )
    return "\n".join(lines) + "\n"


def store_package(pkg: Dict[str, Any], md_path: Path) -> Dict[str, Any]:
    conn = connect(cursor_factory=RealDictCursor)
    cur = conn.cursor()
    try:
        report_date = pkg["metadata"]["report_date"]
        cur.execute("SELECT control_tower.reserve_report_package_version(%s, %s) AS version", (report_date, REPORT_TYPE))
        version = int(cur.fetchone()["version"])
        pkg["metadata"]["package_version"] = version
        cur.execute(
            """
            INSERT INTO control_tower.report_packages(
                report_date, report_type, package_version, package, data_quality_status, can_send,
                exported_markdown_path, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, package_checksum
            """,
            (
                report_date,
                REPORT_TYPE,
                version,
                Json(pkg),
                pkg.get("data_quality", {}).get("status"),
                bool(pkg.get("data_quality", {}).get("can_send")),
                str(md_path),
                "build_ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible",
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return {"package_id": row["id"], "package_version": version, "package_checksum": row["package_checksum"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--out-dir", default="outbox/packages")
    ap.add_argument("--store-db", action="store_true")
    args = ap.parse_args()

    pkg = build_base_package(args.date)
    pkg["ceo_email_scorecard"] = fetch_ceo_email_scorecard(args.date)
    pkg["employee_sales_scorecard"] = fetch_employee_sales_scorecard(args.date)
    apply_rendering_policy(pkg)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible_{args.date}.json"
    md_path = out_dir / f"ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible_{args.date}.md"

    json_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2, default=json_safe), encoding="utf-8")

    md_parts = [
        f"# HH CEO Control Tower Package v2.10 Samples Compatible - {args.date}",
        "",
        f"- Data quality: **{pkg.get('data_quality',{}).get('status')}** | can_send: **{pkg.get('data_quality',{}).get('can_send')}**",
        "- CEO Email Scorecard source: **send_staff_scorecard_emails.py**",
        "- Analytical Scorecard: **Discount/Voucher/Return hidden from report rendering**",
        "- Employee Sales Scorecard: **employee_sales_scorecard.rows rendered after scorecard_by_channel**",
        "",
        markdown_scorecard_without_discount(pkg),
        markdown_employee_sales_scorecard(pkg["employee_sales_scorecard"]),
        markdown_ceo_email_scorecard(pkg["ceo_email_scorecard"]),
    ]
    md_path.write_text("\n".join(md_parts), encoding="utf-8")

    result = {
        "status": "success",
        "json_path": str(json_path),
        "markdown_path": str(md_path),
        "ceo_email_scorecard_rows": len(pkg["ceo_email_scorecard"]["rows"]),
        "employee_sales_scorecard_rows": len(pkg["employee_sales_scorecard"]["rows"]),
        "hidden_scorecard_columns": ["discount_voucher_return"],
    }
    if args.store_db:
        result.update(store_package(pkg, md_path))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_safe))


if __name__ == "__main__":
    main()