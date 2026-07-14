#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build CEO Control Tower package v2.9 with CEO Email Scorecard block.

Purpose:
- Keep the existing package blocks from v2.8 samples.
- Add `ceo_email_scorecard` using the same logic as send_staff_scorecard_emails.py.
- Tell the agent to render CEO Scorecard Form from `ceo_email_scorecard.rows`, not from scorecard_by_channel.
- Tell the agent to hide Discount/Voucher/Return from the analytical scorecard table.

Usage:
  python scripts/build_ceo_control_tower_package_v2_9_ceo_email_scorecard.py --date 2026-06-04 --out-dir outbox/packages --store-db

Requirements:
- scripts/build_ceo_control_tower_package_v2_scorecard.py exists.
- scripts/build_ceo_control_tower_package_v2_8_samples.py exists if sample_orders block is needed.
- scripts/send_staff_scorecard_emails.py exists.
- DB password should be supplied through one of:
  PGPASSWORD, ETL_DB_PASSWORD, DB_PASSWORD.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import psycopg2
from psycopg2.extras import RealDictCursor, Json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_TYPE = "CEO_DAILY_CONTROL_TOWER"

BASE_SCORECARD_BUILDER = PROJECT_ROOT / "scripts" / "build_ceo_control_tower_package_v2_scorecard.py"
SAMPLE_BUILDER = PROJECT_ROOT / "scripts" / "build_ceo_control_tower_package_v2_8_samples.py"
CEO_EMAIL_SCRIPT = PROJECT_ROOT / "scripts" / "send_staff_scorecard_emails.py"


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


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in dict(row).items():
        out[k] = json_safe(v)
    return out


def connect(cursor_factory=None):
    return psycopg2.connect(
        host=os.getenv("PGHOST") or os.getenv("ETL_DB_HOST") or os.getenv("DB_HOST") or "localhost",
        port=int(os.getenv("PGPORT") or os.getenv("ETL_DB_PORT") or os.getenv("DB_PORT") or "5433"),
        dbname=os.getenv("PGDATABASE") or os.getenv("ETL_DB_NAME") or os.getenv("DB_NAME") or "DuLieu",
        user=os.getenv("PGUSER") or os.getenv("ETL_DB_USER") or os.getenv("DB_USER") or "postgres",
        password=os.getenv("PGPASSWORD") or os.getenv("ETL_DB_PASSWORD") or os.getenv("DB_PASSWORD") or "",
        cursor_factory=cursor_factory,
    )


def build_base_package(report_date: str) -> Dict[str, Any]:
    score_mod = load_module(BASE_SCORECARD_BUILDER, "base_scorecard_builder")
    pkg = score_mod.build_package(report_date)

    if SAMPLE_BUILDER.exists():
        sample_mod = load_module(SAMPLE_BUILDER, "sample_builder")
        try:
            pkg["sample_orders"] = sample_mod.fetch_sample_orders_block(report_date, pkg)
            pkg.setdefault("metadata", {})["sample_orders_policy"] = "multi_channel_v2_8"
        except Exception as exc:
            pkg["sample_orders"] = {
                "error": str(exc),
                "notes": ["Could not enrich sample_orders. Check sample order migration/import."]
            }
    return pkg


def fetch_ceo_email_scorecard(report_date: str) -> Dict[str, Any]:
    """Use exactly the same DB logic as the daily CEO email script."""
    ensure_db_env()
    ceo_mod = load_module(CEO_EMAIL_SCRIPT, "send_staff_scorecard_emails_runtime")

    raw_rows = ceo_mod.get_scorecard_rows(report_date)
    full_rows = ceo_mod.filter_rows_by_scope(raw_rows, "FULL")

    rows = [normalize_row(r) for r in full_rows]
    return {
        "report_date": report_date,
        "source": "scripts/send_staff_scorecard_emails.py::get_scorecard_rows + filter_rows_by_scope(FULL)",
        "purpose": "Render the CEO daily email scorecard form with exact MTD/target/cost logic.",
        "rows": rows,
        "columns": [
            "stt",
            "kenh",
            "ty_le_thuc_hien",
            "chi_tieu_thang",
            "tong_thuc_hien_toi_ngay",
            "doanh_so_ngay",
            "chi_phi_quang_cao",
            "ty_le_quang_cao",
            "gia_von",
            "van_chuyen_phi_hoan",
            "backoffice",
            "live_in_house",
            "booking",
            "quang_cao",
            "bao_bi_dong_goi",
            "tien_thu",
            "ln_con_lai",
        ],
        "rendering_notes": [
            "This block is the only source for CEO Scorecard Form.",
            "Do not recreate CEO Scorecard Form from scorecard_by_channel.",
            "This block includes MTD, monthly targets, ECOM grouping, child rows Shopee/TikTok, and total row.",
        ],
    }


def apply_rendering_policy(pkg: Dict[str, Any]) -> None:
    pkg.setdefault("metadata", {})["package_variant"] = "v2_9_ceo_email_scorecard"
    pkg.setdefault("instructions_for_agent", {})
    pkg["instructions_for_agent"].update({
        "ceo_email_scorecard_source": "ceo_email_scorecard.rows",
        "scorecard_analysis_source": "scorecard_by_channel",
        "do_not_use_channel_breakdown_legacy": True,
        "do_not_recalculate_ceo_email_scorecard": True,
        "hide_scorecard_columns": ["discount_voucher_return"],
        "removed_scorecard_column": "Discount/Voucher/Return",
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
            "note": "Use this block to render the CEO Email Scorecard Form; do not derive it from scorecard_by_channel.",
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
    lines.append("| STT | Kênh | % thực hiện | Chỉ tiêu tháng | Tổng thực hiện tới ngày | Doanh số ngày | Chi phí QC | % QC/DS ngày | Giá vốn | VC/Phí hoàn | Backoffice | Live | Booking | Quảng cáo | Bao bì | Tiền thu | LN còn lại |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in block.get("rows", []):
        lines.append(
            f"| {r.get('stt','')} | {r.get('kenh','')} | {fmt_pct(r.get('ty_le_thuc_hien'))} | "
            f"{fmt_money(r.get('chi_tieu_thang'))} | {fmt_money(r.get('tong_thuc_hien_toi_ngay'))} | "
            f"{fmt_money(r.get('doanh_so_ngay'))} | {fmt_money(r.get('chi_phi_quang_cao'))} | {fmt_pct(r.get('ty_le_quang_cao'))} | "
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
                "build_ceo_control_tower_package_v2_9_ceo_email_scorecard",
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
    apply_rendering_policy(pkg)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"ceo_control_tower_package_v2_9_ceo_email_scorecard_{args.date}.json"
    md_path = out_dir / f"ceo_control_tower_package_v2_9_ceo_email_scorecard_{args.date}.md"

    json_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2, default=json_safe), encoding="utf-8")

    md_parts = [
        f"# HH CEO Control Tower Package v2.9 - {args.date}",
        "",
        f"- Data quality: **{pkg.get('data_quality',{}).get('status')}** | can_send: **{pkg.get('data_quality',{}).get('can_send')}**",
        "- CEO Email Scorecard source: **send_staff_scorecard_emails.py**",
        "- Analytical Scorecard: **Discount/Voucher/Return hidden from report rendering**",
        "",
        markdown_scorecard_without_discount(pkg),
        markdown_ceo_email_scorecard(pkg["ceo_email_scorecard"]),
    ]
    md_path.write_text("\n".join(md_parts), encoding="utf-8")

    result = {
        "status": "success",
        "json_path": str(json_path),
        "markdown_path": str(md_path),
        "ceo_email_scorecard_rows": len(pkg["ceo_email_scorecard"]["rows"]),
        "hidden_scorecard_columns": ["discount_voucher_return"],
    }
    if args.store_db:
        result.update(store_package(pkg, md_path))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_safe))


if __name__ == "__main__":
    main()
