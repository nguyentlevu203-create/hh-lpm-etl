#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build CEO Control Tower package v2 with standardized scorecard_by_channel.

This script keeps current sales_summary/channel_breakdown for continuity, but adds:
- metric_definitions
- scorecard_by_channel from control_tower.v_scorecard_by_channel_daily_for_package
- scorecard_source_notes
- exact-numbers policy: numeric missing/not-applicable fields = 0, negative values kept as-is
- inventory alerts from inventory.v_inventory_alerts_ceo_for_package if present

Run:
  python scripts/build_ceo_control_tower_package_v2_scorecard.py --date 2026-05-20 --out-dir outbox/packages --store-db
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from psycopg2.extras import RealDictCursor, Json

import sys
from pathlib import Path as _Path
sys.path.append(str(_Path(__file__).resolve().parents[1]))

from etl_common import connect, json_safe

try:
    from api.routers.ceo import CEO_MASTER_SQL
except Exception as exc:
    CEO_MASTER_SQL = None
    CEO_IMPORT_ERROR = exc
else:
    CEO_IMPORT_ERROR = None

REPORT_TYPE = "CEO_DAILY_CONTROL_TOWER"
PHASE = "sales_inventory_scorecard_standard"

EXCLUDED_METRICS = [
    "cong_no",
    "dos_chinh_xac",
    "sku_ban_cham_chinh_xac_theo_velocity",
    "so_luong_dat_hang_toi_uu",
    "chuong_trinh_xa_hang_toi_uu",
    "du_bao_het_hang_theo_ngay",
    "aov",
    "don_dang_giao",
]


METRIC_DEFINITIONS = {
    "gross_sales": "Gross Sales = Đơn giá × Số lượng. Trong package này lấy theo source_map từng kênh.",
    "net_sales": "Net Sales = Gross Sales - Discount - Voucher - Return/Refund. Với kênh chưa có tách discount/voucher thì dùng doanh thu/net revenue hiện ETL đang chuẩn hóa.",
    "cogs": "COGS = Số lượng × Landed Cost/Giá vốn chuẩn.",
    "gross_margin": "Gross Margin = Net Sales - COGS.",
    "gm_pct": "GM% = Gross Margin / Net Sales.",
    "aov": "AOV = Net Sales / Đơn thành công. Chưa đưa vào Scorecard theo kênh giai đoạn này.",
    "cancellation_rate": "Cancellation Rate = Đơn hủy / Tổng đơn. Không hiển thị trong bảng 4 Scorecard theo kênh theo yêu cầu hiện tại.",
    "dos": "DOS = Tồn khả dụng / Tốc độ bán trung bình ngày. Chưa tính chính xác giai đoạn này.",
}


# Exact numbers policy:
# - Numeric missing/not-applicable fields must be 0.
# - Negative values must be kept as filtered by the DB views.
# - Identifier/text fields such as EAN/owner may be empty strings, not numeric 0.
SCORECARD_NUMERIC_FIELDS = {
    "gross_sales", "net_sales", "discount_voucher_return", "cogs", "gross_margin", "gm_pct",
    "platform_fees", "shipping_return_fee", "ads_cost", "live_cost", "booking_fee",
    "backoffice_fee", "packaging_cost", "payout", "profit", "profit_margin_pct",
    "ads_to_sales_pct", "orders", "success_orders", "cancelled_orders", "cancellation_rate_pct",
    "revenue_share_pct",
}

INVENTORY_ALERT_NUMERIC_FIELDS = {
    "total_stock", "stock_hn", "stock_hcm", "reorder_point_qty",
    "affected_qty", "affected_pct",
}

TOP_SKU_NUMERIC_FIELDS = {"qty_sold", "net_sales", "cogs", "gross_margin"}
SAMPLE_ORDER_NUMERIC_FIELDS = {"sample_order_count", "sample_sku_qty", "sample_cogs_amount"}

INVENTORY_SUMMARY_NUMERIC_HINTS = (
    "count", "qty", "stock", "alert", "p1", "p2", "p3", "under", "pct", "value", "amount"
)


def zero_none_numeric_fields(row: Dict[str, Any], numeric_fields: set[str]) -> Dict[str, Any]:
    for key in numeric_fields:
        if key in row and row[key] is None:
            row[key] = 0
    return row


def zero_none_inventory_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in list(row.items()):
        if value is None and any(hint in key.lower() for hint in INVENTORY_SUMMARY_NUMERIC_HINTS):
            row[key] = 0
    return row


def normalize_identifier_fields(row: Dict[str, Any], fields: set[str]) -> Dict[str, Any]:
    for key in fields:
        if key in row and row[key] is None:
            row[key] = ""
    return row


def fetch_all(cur, sql: str, params=()) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def fetch_one(cur, sql: str, params=()) -> Dict[str, Any]:
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else {}


def sales_params(report_date: str):
    return (
        report_date, report_date, report_date, report_date,
        report_date, report_date, report_date, report_date,
        report_date, report_date, report_date, report_date, report_date, report_date,
        report_date, report_date,
    )


def fetch_sales(cur, report_date: str) -> Dict[str, Any]:
    if CEO_MASTER_SQL is None:
        raise RuntimeError(f"Cannot import CEO_MASTER_SQL from api.routers.ceo: {CEO_IMPORT_ERROR}")
    cur.execute(CEO_MASTER_SQL, sales_params(report_date))
    channels = [dict(r) for r in cur.fetchall()]
    total = {
        "orders": sum(int(r.get("orders") or 0) for r in channels),
        "success_orders": sum(int(r.get("success_orders") or 0) for r in channels),
        "cancelled_orders": sum(int(r.get("cancelled_orders") or 0) for r in channels),
        "revenue": sum(float(r.get("revenue") or 0) for r in channels),
        "cogs": sum(float(r.get("cogs") or 0) for r in channels),
        "platform_fees": sum(float(r.get("platform_fees") or 0) for r in channels),
        "shipping_fee": sum(float(r.get("shipping_fee") or 0) for r in channels),
        "backoffice_fee": sum(float(r.get("backoffice_fee") or 0) for r in channels),
        "booking_fee": sum(float(r.get("booking_fee") or 0) for r in channels),
        "packaging_cost": sum(float(r.get("packaging_cost") or 0) for r in channels),
        "ads_cost": sum(float(r.get("ads_cost") or 0) for r in channels),
        "live_cost": sum(float(r.get("live_cost") or 0) for r in channels),
        "payout_estimated": sum(float(r.get("payout_estimated") or 0) for r in channels),
        "profit": sum(float(r.get("profit") or 0) for r in channels),
    }
    total["profit_margin_pct"] = round(total["profit"] / total["revenue"] * 100, 2) if total["revenue"] else 0
    total["cancel_rate_pct"] = round(total["cancelled_orders"] / total["orders"] * 100, 2) if total["orders"] else 0
    for r in channels:
        revenue = float(r.get("revenue") or 0)
        ads = float(r.get("ads_cost") or 0)
        orders = int(r.get("orders") or 0)
        cancelled = int(r.get("cancelled_orders") or 0)
        r["ads_to_sales_pct"] = round(ads / revenue * 100, 2) if revenue else 0
        r["cancel_rate_pct"] = round(cancelled / orders * 100, 2) if orders else 0
        r["revenue_share_pct"] = round(revenue / total["revenue"] * 100, 2) if total["revenue"] else 0
    return {"summary": total, "channels": channels}


def enrich_scorecard(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for r in rows:
        zero_none_numeric_fields(r, SCORECARD_NUMERIC_FIELDS)
        source_map = r.get("source_map") or {}
        source_map["ceo_package_policy"] = (
            "exact_numbers_v2_5_2: numeric missing/not-applicable fields = 0; "
            "negative values are kept unchanged; use scorecard_by_channel for table 4"
        )
        r["source_map"] = source_map

    total_net = sum(float(r.get("net_sales") or 0) for r in rows)
    out = []
    for r in rows:
        net = float(r.get("net_sales") or 0)
        r["revenue_share_pct"] = round(net / total_net * 100, 2) if total_net else 0
        zero_none_numeric_fields(r, SCORECARD_NUMERIC_FIELDS)
        source_map = r.get("source_map") or {}
        cost_status = {}
        for k in ["platform_fees", "shipping_return_fee", "ads_cost", "live_cost", "booking_fee", "backoffice_fee", "packaging_cost", "payout", "discount_voucher_return"]:
            source = str(source_map.get(k, ""))
            if "not_applicable" in source:
                cost_status[k] = "not_applicable"
            elif "not_available" in source:
                cost_status[k] = "not_available"
            else:
                cost_status[k] = "available"
        r["cost_status"] = cost_status

        margin = float(r.get("profit_margin_pct") or 0)
        ads_pct = float(r.get("ads_to_sales_pct") or 0)
        ch = str(r.get("channel") or "")
        if margin < 1:
            r["alert"] = "P1: Profit margin rất thấp"
            r["action"] = "Rà ngay chi phí ads/fee/live/booking và SKU đang kéo lỗ."
        elif ch in {"NHANH"} and ads_pct > 40:
            r["alert"] = "P1: Ads/DS vượt ngưỡng Digital"
            r["action"] = "Giảm/tắt campaign ads hiệu quả thấp, ưu tiên SKU có GM tốt."
        elif ch in {"SHOPEE", "TIKTOK"} and ads_pct > 20:
            r["alert"] = "P2: Ads/DS vượt ngưỡng Ecom"
            r["action"] = "Rà voucher, ads, phí sàn và mix SKU."
        elif margin < 5:
            r["alert"] = "P2: Margin thấp"
            r["action"] = "Kiểm tra chi phí biến đổi và giá bán."
        else:
            r["alert"] = "Theo dõi"
            r["action"] = "Duy trì, tối ưu SKU và ngân sách."
        out.append(r)
    return out


def quality_status(dq_rows: List[Dict[str, Any]], manual_checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    statuses = [r.get("check_status") for r in dq_rows] + [c.get("status") for c in manual_checks]
    if any(s == "FAIL" for s in statuses):
        return {"status": "FAIL", "can_send": False}
    if any(s in {"WARNING", "PASS_WITH_WARNINGS"} for s in statuses):
        return {"status": "PASS_WITH_WARNINGS", "can_send": True}
    return {"status": "PASS", "can_send": True}


def build_package(report_date: str) -> Dict[str, Any]:
    conn = connect(cursor_factory=RealDictCursor)
    cur = conn.cursor()
    try:
        sales = fetch_sales(cur, report_date)
        scorecard = fetch_all(
            cur,
            """
            SELECT *
            FROM control_tower.v_scorecard_by_channel_daily_for_package
            WHERE report_date = %s
            ORDER BY channel_sort, channel
            """,
            (report_date,),
        )
        scorecard = enrich_scorecard(scorecard)

        dq_rows = fetch_all(
            cur,
            "SELECT * FROM control_tower.v_data_quality_gate_daily WHERE report_date = %s ORDER BY domain",
            (report_date,),
        )
        stock_snapshot_count = fetch_one(
            cur,
            "SELECT COUNT(*) AS cnt FROM inventory.stock_lot_snapshots WHERE snapshot_date = %s",
            (report_date,),
        ).get("cnt", 0)
        reorder_rules_count = fetch_one(
            cur,
            "SELECT COUNT(*) AS cnt FROM inventory.reorder_alert_rules WHERE is_active = TRUE",
        ).get("cnt", 0)
        manual_checks = [
            {
                "check_key": "inventory_snapshot_exists",
                "status": "PASS" if int(stock_snapshot_count or 0) > 0 else "FAIL",
                "message": f"inventory.stock_lot_snapshots rows = {stock_snapshot_count}",
            },
            {
                "check_key": "reorder_rules_exists",
                "status": "PASS" if int(reorder_rules_count or 0) > 0 else "FAIL",
                "message": f"active reorder rules = {reorder_rules_count}",
            },
            {
                "check_key": "scorecard_by_channel_exists",
                "status": "PASS" if len(scorecard) > 0 else "FAIL",
                "message": f"scorecard_by_channel rows = {len(scorecard)}",
            },
        ]
        dq = quality_status(dq_rows, manual_checks)

        top_overall_qty = fetch_all(
            cur,
            """
            SELECT
                report_date,
                rank_no,
                sku_code,
                COALESCE(ean, '') AS ean,
                product_name,
                COALESCE(qty_sold, 0) AS qty_sold,
                COALESCE(net_sales, 0) AS net_sales,
                COALESCE(channels, '') AS channels
            FROM mart.v_top_sku_overall_daily
            WHERE report_date = %s
            ORDER BY rank_no
            """,
            (report_date,),
        )
        top_by_channel = fetch_all(
            cur,
            """
            SELECT
                report_date,
                channel,
                rank_no,
                sku_code,
                COALESCE(ean, '') AS ean,
                product_name,
                COALESCE(qty_sold, 0) AS qty_sold,
                COALESCE(net_sales, 0) AS net_sales
            FROM mart.v_top_sku_daily
            WHERE report_date = %s
            ORDER BY channel, rank_no
            """,
            (report_date,),
        )
        inventory_summary = fetch_one(
            cur,
            "SELECT * FROM inventory.v_inventory_daily_summary WHERE snapshot_date = %s",
            (report_date,),
        )
        zero_none_inventory_summary(inventory_summary)

        # Prefer clean CEO/package view if present.
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.views
                WHERE table_schema='inventory' AND table_name='v_inventory_alerts_ceo_for_package'
            ) AS exists_view
        """)
        alert_view = "inventory.v_inventory_alerts_ceo_for_package" if bool(cur.fetchone()["exists_view"]) else "inventory.v_inventory_alerts_ceo"

        inventory_alerts = fetch_all(
            cur,
            f"""
            SELECT
                snapshot_date,
                priority,
                alert_type,
                sku_code,
                COALESCE(ean, '') AS ean,
                product_name,
                COALESCE(total_stock, 0) AS total_stock,
                COALESCE(stock_hn, 0) AS stock_hn,
                COALESCE(stock_hcm, 0) AS stock_hcm,
                COALESCE(reorder_point_qty, 0) AS reorder_point_qty,
                COALESCE(affected_qty, 0) AS affected_qty,
                COALESCE(affected_pct, 0) AS affected_pct,
                earliest_expiry_date,
                COALESCE(owner, '') AS owner,
                reason,
                suggested_action
            FROM {alert_view}
            WHERE snapshot_date = %s
            ORDER BY priority, alert_type, affected_qty DESC NULLS LAST, total_stock ASC
            """,
            (report_date,),
        )
        for row in inventory_alerts:
            zero_none_numeric_fields(row, INVENTORY_ALERT_NUMERIC_FIELDS)
            normalize_identifier_fields(row, {"ean", "owner"})


        sample_orders = {}
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.views
                WHERE table_schema='control_tower' AND table_name='v_sample_order_ceo'
            ) AS exists_view
        """)
        if bool(cur.fetchone()["exists_view"]):
            sample_orders = fetch_one(
                cur,
                """
                SELECT
                    report_date,
                    COALESCE(sample_order_count, 0) AS sample_order_count,
                    COALESCE(sample_sku_qty, 0) AS sample_sku_qty,
                    COALESCE(sample_cogs_amount, 0) AS sample_cogs_amount
                FROM control_tower.v_sample_order_ceo
                WHERE report_date = %s
                """,
                (report_date,),
            )
            zero_none_numeric_fields(sample_orders, SAMPLE_ORDER_NUMERIC_FIELDS)

        return json_safe({
            "metadata": {
                "report_date": report_date,
                "report_type": REPORT_TYPE,
                "phase": PHASE,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "timezone": "Asia/Ho_Chi_Minh",
                "excluded_metrics": EXCLUDED_METRICS,
                "scorecard_policy": "exact_numbers_v2_5_2",
            },
            "metric_definitions": METRIC_DEFINITIONS,
            "data_quality": {
                "status": dq["status"],
                "can_send": dq["can_send"],
                "mapping_checks": dq_rows,
                "manual_checks": manual_checks,
            },
            "sales_summary": sales["summary"],
            "channel_breakdown_legacy": sales["channels"],
            "scorecard_by_channel": scorecard,
            "scorecard_notes": [
                "Bảng 4 Scorecard theo kênh phải dùng scorecard_by_channel, không dùng channel_breakdown_legacy.",
                "Scorecard không hiển thị Order Health theo yêu cầu hiện tại.",
                "Policy v2.5.2: mọi field số không có/không áp dụng = 0.",
                "Số âm được giữ nguyên theo đúng số bộ lọc trả ra; agent không được tự sửa.",
                "MT và GT được tách riêng trong scorecard_by_channel.",
            ],
            "top_sku_overall_by_qty": top_overall_qty,
            "top_sku_by_channel": top_by_channel,
            "inventory_summary": inventory_summary,
            "inventory_alerts": inventory_alerts,
            "sample_orders": sample_orders,
            "instructions_for_agent": {
                "language": "vi",
                "must_not_invent_numbers": True,
                "scorecard_source": "scorecard_by_channel",
                "do_not_use_order_health_in_scorecard": True,
                "numeric_null_policy": "all numeric missing/not-applicable fields are 0",
                "negative_number_policy": "keep negative values unchanged",
                "output_required": [
                    "executive_summary",
                    "kpi_cards",
                    "metric_definitions",
                    "scorecard_by_channel",
                    "top_sku_analysis",
                    "inventory_alerts",
                    "p1_p2_p3_actions",
                    "email_html"
                ],
            },
        })
    finally:
        cur.close()
        conn.close()


def format_money(v: Any) -> str:
    if v is None:
        return "0"
    try:
        return f"{float(v):,.0f}"
    except Exception:
        return str(v if v is not None else 0)


def markdown_package(pkg: Dict[str, Any]) -> str:
    md = []
    meta = pkg["metadata"]
    dq = pkg["data_quality"]
    md.append(f"# HH CEO Control Tower Package v2.5.2 - {meta['report_date']}")
    md.append("")
    md.append(f"- Data quality: **{dq['status']}** | can_send: **{dq['can_send']}**")
    md.append(f"- Phase: {meta['phase']}")
    md.append("- Policy: **numeric missing/not-applicable fields = 0; negative values kept unchanged**")
    md.append("")
    md.append("## Metric Definitions")
    for k, v in pkg.get("metric_definitions", {}).items():
        md.append(f"- **{k}**: {v}")
    md.append("")
    md.append("## Scorecard by Channel")
    md.append("| Channel | Gross Sales | Net Sales | Disc/Voucher/Return | COGS | Gross Margin | GM% | Platform | Ship/Return | Ads | Live | Booking | Backoffice | Packaging | Payout | Profit | Profit Margin | Alert |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in pkg.get("scorecard_by_channel", []):
        md.append(
            f"| {r.get('channel')} | {format_money(r.get('gross_sales'))} | {format_money(r.get('net_sales'))} | "
            f"{format_money(r.get('discount_voucher_return'))} | {format_money(r.get('cogs'))} | {format_money(r.get('gross_margin'))} | "
            f"{r.get('gm_pct')} | {format_money(r.get('platform_fees'))} | {format_money(r.get('shipping_return_fee'))} | "
            f"{format_money(r.get('ads_cost'))} | {format_money(r.get('live_cost'))} | {format_money(r.get('booking_fee'))} | "
            f"{format_money(r.get('backoffice_fee'))} | {format_money(r.get('packaging_cost'))} | {format_money(r.get('payout'))} | "
            f"{format_money(r.get('profit'))} | {r.get('profit_margin_pct')} | {r.get('alert')} |"
        )
    md.append("")
    md.append("## Top SKU Overall")
    md.append("| Rank | SKU | Product | Qty | Net Sales | Channels |")
    md.append("|---:|---|---|---:|---:|---|")
    for r in pkg.get("top_sku_overall_by_qty", []):
        md.append(f"| {r.get('rank_no')} | {r.get('sku_code')} | {r.get('product_name')} | {r.get('qty_sold')} | {format_money(r.get('net_sales'))} | {r.get('channels')} |")
    md.append("")
    md.append("## Inventory Alerts")
    md.append("| Priority | Type | SKU | Product | Total | HN | HCM | Reorder Point | Affected Qty | Affected % | Reason | Action |")
    md.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    for r in pkg.get("inventory_alerts", []):
        md.append(
            f"| {r.get('priority')} | {r.get('alert_type')} | {r.get('sku_code')} | {r.get('product_name')} | "
            f"{format_money(r.get('total_stock'))} | {format_money(r.get('stock_hn'))} | {format_money(r.get('stock_hcm'))} | "
            f"{format_money(r.get('reorder_point_qty'))} | {format_money(r.get('affected_qty'))} | {r.get('affected_pct') or 0} | "
            f"{r.get('reason')} | {r.get('suggested_action')} |"
        )
    md.append("")
    md.append("## Agent Task")
    md.append("Hãy tạo báo cáo CEO HTML đẹp. Bảng 4 Scorecard theo kênh bắt buộc dùng `scorecard_by_channel`; không đưa Order Health vào bảng 4; không tự bịa số.")
    return "\n".join(md)


def store_package(pkg: Dict[str, Any], json_path: Path, md_path: Path) -> Dict[str, Any]:
    conn = connect(cursor_factory=RealDictCursor)
    cur = conn.cursor()
    try:
        report_date = pkg["metadata"]["report_date"]
        cur.execute("SELECT control_tower.reserve_report_package_version(%s, %s) AS version", (report_date, REPORT_TYPE))
        version = int(cur.fetchone()["version"])
        pkg["metadata"]["package_version"] = version
        dq_status = pkg["data_quality"]["status"]
        can_send = bool(pkg["data_quality"]["can_send"])
        cur.execute(
            """
            INSERT INTO control_tower.report_packages(
                report_date, report_type, package_version, package, data_quality_status, can_send,
                exported_markdown_path, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, package_checksum
            """,
            (report_date, REPORT_TYPE, version, Json(pkg), dq_status, can_send, str(md_path), "build_ceo_control_tower_package_v2_scorecard"),
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
    ap = argparse.ArgumentParser(description="Build CEO Control Tower Package v2 Scorecard")
    ap.add_argument("--date", required=True, help="Report date YYYY-MM-DD")
    ap.add_argument("--out-dir", default="outbox/packages", help="Output directory")
    ap.add_argument("--store-db", action="store_true")
    args = ap.parse_args()

    datetime.strptime(args.date, "%Y-%m-%d")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pkg = build_package(args.date)
    json_path = out_dir / f"ceo_control_tower_package_v2_scorecard_{args.date}.json"
    md_path = out_dir / f"ceo_control_tower_package_v2_scorecard_{args.date}.md"
    json_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2, default=json_safe), encoding="utf-8")
    md_path.write_text(markdown_package(pkg), encoding="utf-8")

    result = {"status": "success", "json_path": str(json_path), "markdown_path": str(md_path), "data_quality": pkg["data_quality"]}
    if args.store_db:
        result.update(store_package(pkg, json_path, md_path))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_safe))


if __name__ == "__main__":
    main()
