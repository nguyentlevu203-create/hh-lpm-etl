#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build CEO Control Tower Package for ChatGPT/Agent analysis.

Outputs JSON + Markdown package from DB. Optionally stores the package in
control_tower.report_packages with concurrency-safe versioning.

Example:
  python scripts/build_ceo_control_tower_package.py --date 2026-05-20 --out-dir outbox/packages --store-db
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from psycopg2.extras import RealDictCursor, Json

sys.path.append(str(Path(__file__).resolve().parents[1]))
from etl_common import connect, json_safe  # noqa: E402

try:
    from api.routers.ceo import CEO_MASTER_SQL  # reuse the existing production CEO SQL
except Exception as exc:  # pragma: no cover
    CEO_MASTER_SQL = None
    CEO_IMPORT_ERROR = exc
else:
    CEO_IMPORT_ERROR = None

REPORT_TYPE = "CEO_DAILY_CONTROL_TOWER"
PHASE = "sales_inventory_basic"

EXCLUDED_METRICS = [
    "cong_no",
    "dos_chinh_xac",
    "sku_ban_cham_chinh_xac_theo_velocity",
    "so_luong_dat_hang_toi_uu",
    "chuong_trinh_xa_hang_toi_uu",
    "du_bao_het_hang_theo_ngay",
    "aov",
    "don_mau",
    "don_dang_giao",
]


def fetch_all(cur, sql: str, params=()) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def fetch_one(cur, sql: str, params=()) -> Dict[str, Any]:
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else {}


def sales_params(report_date: str):
    # Matches api.routers.ceo.CEO_MASTER_SQL parameter order.
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
        manual_checks = []
        manual_checks.append({
            "check_key": "inventory_snapshot_exists",
            "status": "PASS" if int(stock_snapshot_count or 0) > 0 else "FAIL",
            "message": f"inventory.stock_lot_snapshots rows = {stock_snapshot_count}",
        })
        manual_checks.append({
            "check_key": "reorder_rules_exists",
            "status": "PASS" if int(reorder_rules_count or 0) > 0 else "FAIL",
            "message": f"active reorder rules = {reorder_rules_count}",
        })
        dq = quality_status(dq_rows, manual_checks)

        top_overall_qty = fetch_all(
            cur,
            """
            SELECT report_date, rank_no, sku_code, ean, product_name, qty_sold, net_sales, channels
            FROM mart.v_top_sku_overall_daily
            WHERE report_date = %s
            ORDER BY rank_no
            """,
            (report_date,),
        )
        top_overall_revenue = fetch_all(
            cur,
            """
            SELECT report_date, rank_no, sku_code, ean, product_name, qty_sold, net_sales, channels
            FROM mart.v_top_sku_revenue_overall_daily
            WHERE report_date = %s
            ORDER BY rank_no
            """,
            (report_date,),
        )
        top_by_channel = fetch_all(
            cur,
            """
            SELECT report_date, channel, rank_no, sku_code, ean, product_name, qty_sold, net_sales
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
        inventory_alerts = fetch_all(
            cur,
            """
            SELECT snapshot_date, priority, alert_type, sku_code, ean, product_name,
                   total_stock, stock_hn, stock_hcm, reorder_point_qty,
                   affected_qty, affected_pct, earliest_expiry_date, owner, reason, suggested_action
            FROM inventory.v_inventory_alerts_ceo_for_package
            WHERE snapshot_date = %s
            ORDER BY priority, alert_type, affected_qty DESC NULLS LAST, total_stock ASC
            """,
            (report_date,),
        )
        return json_safe({
            "metadata": {
                "report_date": report_date,
                "report_type": REPORT_TYPE,
                "phase": PHASE,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "timezone": "Asia/Ho_Chi_Minh",
                "excluded_metrics": EXCLUDED_METRICS,
            },
            "data_quality": {
                "status": dq["status"],
                "can_send": dq["can_send"],
                "mapping_checks": dq_rows,
                "manual_checks": manual_checks,
            },
            "sales_summary": sales["summary"],
            "channel_breakdown": sales["channels"],
            "top_sku_overall_by_qty": top_overall_qty,
            "top_sku_overall_by_revenue": top_overall_revenue,
            "top_sku_by_channel": top_by_channel,
            "inventory_summary": inventory_summary,
            "inventory_alerts": inventory_alerts,
            "instructions_for_agent": {
                "language": "vi",
                "must_not_invent_numbers": True,
                "output_required": ["executive_summary", "kpi_cards", "channel_analysis", "top_sku_analysis", "inventory_alerts", "p1_p2_p3_actions", "email_html"],
            },
        })
    finally:
        cur.close(); conn.close()


def format_money(v: Any) -> str:
    try:
        return f"{float(v):,.0f}"
    except Exception:
        return str(v or "")


def markdown_package(pkg: Dict[str, Any]) -> str:
    md = []
    meta = pkg["metadata"]
    dq = pkg["data_quality"]
    sales = pkg["sales_summary"]
    md.append(f"# HH CEO Control Tower Package - {meta['report_date']}")
    md.append("")
    md.append(f"- Data quality: **{dq['status']}** | can_send: **{dq['can_send']}**")
    md.append(f"- Phase: {meta['phase']}")
    md.append("- Excluded: " + ", ".join(meta["excluded_metrics"]))
    md.append("")
    md.append("## Sales Summary")
    for k in ["revenue", "profit", "profit_margin_pct", "orders", "success_orders", "cancelled_orders", "cancel_rate_pct", "ads_cost"]:
        md.append(f"- {k}: {format_money(sales.get(k))}")
    md.append("")
    md.append("## Channel Breakdown")
    md.append("| Channel | Revenue | Profit | Margin % | Ads/Sales % | Orders | Cancelled |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in pkg.get("channel_breakdown", []):
        md.append(f"| {r.get('source_system')} | {format_money(r.get('revenue'))} | {format_money(r.get('profit'))} | {r.get('profit_margin_pct')} | {r.get('ads_to_sales_pct')} | {r.get('orders')} | {r.get('cancelled_orders')} |")
    md.append("")
    md.append("## Top SKU Overall by Quantity")
    md.append("| Rank | SKU | EAN | Product | Qty | Net Sales | Channels |")
    md.append("|---:|---|---|---|---:|---:|---|")
    for r in pkg.get("top_sku_overall_by_qty", []):
        md.append(f"| {r.get('rank_no')} | {r.get('sku_code')} | {r.get('ean') or ''} | {r.get('product_name')} | {r.get('qty_sold')} | {format_money(r.get('net_sales'))} | {r.get('channels')} |")
    md.append("")
    md.append("## Inventory Summary")
    inv = pkg.get("inventory_summary") or {}
    for k, v in inv.items():
        md.append(f"- {k}: {format_money(v)}")
    md.append("")
    md.append("## Inventory Alerts")
    md.append("| Priority | Type | SKU | Product | Total | HN | HCM | Affected | Reason | Action |")
    md.append("|---|---|---|---|---:|---:|---:|---:|---|---|")
    for r in pkg.get("inventory_alerts", []):
        md.append(f"| {r.get('priority')} | {r.get('alert_type')} | {r.get('sku_code')} | {r.get('product_name')} | {format_money(r.get('total_stock'))} | {format_money(r.get('stock_hn'))} | {format_money(r.get('stock_hcm'))} | {format_money(r.get('affected_qty'))} | {r.get('reason')} | {r.get('suggested_action')} |")
    md.append("")
    md.append("## Agent Task")
    md.append("Hãy phân tích package này và tạo báo cáo CEO bằng tiếng Việt, gồm Executive Summary, KPI Cards, phân tích kênh, Top SKU, tồn kho, cảnh báo P1/P2/P3, action list và EMAIL_HTML đẹp để gửi Gmail.")
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
            (report_date, REPORT_TYPE, version, Json(pkg), dq_status, can_send, str(md_path), "build_ceo_control_tower_package"),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return {"package_id": row["id"], "package_version": version, "package_checksum": row["package_checksum"]}
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build CEO Control Tower Package")
    ap.add_argument("--date", required=True, help="Report date YYYY-MM-DD")
    ap.add_argument("--out-dir", default="outbox/packages", help="Output directory")
    ap.add_argument("--store-db", action="store_true", help="Insert package into control_tower.report_packages")
    args = ap.parse_args()

    # Validate date format.
    datetime.strptime(args.date, "%Y-%m-%d")
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    pkg = build_package(args.date)
    json_path = out_dir / f"ceo_control_tower_package_{args.date}.json"
    md_path = out_dir / f"ceo_control_tower_package_{args.date}.md"
    json_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2, default=json_safe), encoding="utf-8")
    md_path.write_text(markdown_package(pkg), encoding="utf-8")
    result = {"status": "success", "json_path": str(json_path), "markdown_path": str(md_path), "data_quality": pkg["data_quality"]}
    if args.store_db:
        result.update(store_package(pkg, json_path, md_path))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_safe))


if __name__ == "__main__":
    main()
