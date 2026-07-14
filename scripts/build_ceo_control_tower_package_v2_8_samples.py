#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build CEO Control Tower package and enrich with multi-channel sample_orders block.

This wrapper calls the current package builder's build_package(report_date), then adds:
- sample_orders.total
- sample_orders.by_channel
- sample_orders.status_breakdown
- sample_orders.mtd
- sample_orders.notes

Usage:
  python scripts/build_ceo_control_tower_package_v2_8_samples.py --date 2026-06-04 --out-dir outbox/packages --store-db
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import psycopg2
from psycopg2.extras import RealDictCursor, Json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_BUILDER_PATH = PROJECT_ROOT / "scripts" / "build_ceo_control_tower_package_v2_scorecard.py"
REPORT_TYPE = "CEO_DAILY_CONTROL_TOWER"


def load_base_builder():
    spec = importlib.util.spec_from_file_location("base_builder", BASE_BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load base builder: {BASE_BUILDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def connect(cursor_factory=None):
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5433")),
        dbname=os.getenv("PGDATABASE", "DuLieu"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD"),
        cursor_factory=cursor_factory,
    )


def safe_float(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def fetch_all(cur, sql: str, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def fetch_one(cur, sql: str, params=()):
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else {}


def scorecard_sales_by_channel(pkg: Dict[str, Any]) -> Dict[str, float]:
    out = {}
    for r in pkg.get("scorecard_by_channel", []):
        ch = str(r.get("channel") or "").upper()
        out[ch] = safe_float(r.get("net_sales"))
    return out


def fetch_sample_orders_block(report_date: str, pkg: Dict[str, Any]) -> Dict[str, Any]:
    channel_sales = scorecard_sales_by_channel(pkg)
    total_net_sales = sum(channel_sales.values())

    conn = connect(cursor_factory=RealDictCursor)
    cur = conn.cursor()
    try:
        daily_rows = fetch_all(cur, """
            SELECT
                report_date,
                channel,
                source_channel,
                shop_label,
                sample_order_count,
                sample_qty,
                sample_sku_count,
                sample_cogs_amount,
                cancelled_sample_order_count
            FROM control_tower.v_sample_order_daily_by_channel
            WHERE report_date = %s
            ORDER BY channel, shop_label
        """, (report_date,))

        status_rows = fetch_all(cur, """
            SELECT
                report_date,
                channel,
                order_status,
                sample_order_count,
                sample_qty,
                sample_cogs_amount
            FROM control_tower.v_sample_order_status_daily
            WHERE report_date = %s
            ORDER BY channel, order_status
        """, (report_date,))

        # Month-to-date up to report_date.
        mtd_rows = fetch_all(cur, """
            SELECT
                channel,
                SUM(sample_order_count)::bigint AS sample_order_count_mtd,
                SUM(sample_qty)::numeric(18,2) AS sample_qty_mtd,
                SUM(sample_sku_count)::integer AS sample_sku_count_mtd,
                SUM(sample_cogs_amount)::numeric(18,2) AS sample_cogs_amount_mtd
            FROM control_tower.v_sample_order_daily_by_channel
            WHERE report_date >= date_trunc('month', %s::date)::date
              AND report_date <= %s::date
            GROUP BY channel
            ORDER BY channel
        """, (report_date, report_date))

        by_channel = []
        for r in daily_rows:
            ch = str(r.get("channel") or "").upper()
            channel_net_sales = channel_sales.get(ch, 0.0)
            sample_cogs = safe_float(r.get("sample_cogs_amount"))
            by_channel.append({
                "report_date": str(r.get("report_date")),
                "channel": ch,
                "source_channel": r.get("source_channel") or ch,
                "shop_label": r.get("shop_label") or "",
                "sample_order_count": int(r.get("sample_order_count") or 0),
                "sample_qty": safe_float(r.get("sample_qty")),
                "sample_sku_count": int(r.get("sample_sku_count") or 0),
                "sample_cogs_amount": sample_cogs,
                "cancelled_sample_order_count": int(r.get("cancelled_sample_order_count") or 0),
                "channel_net_sales": channel_net_sales,
                "sample_cogs_pct_of_channel_net_sales": round(sample_cogs / channel_net_sales * 100, 2) if channel_net_sales else 0,
            })

        total = {
            "report_date": report_date,
            "sample_order_count": sum(r["sample_order_count"] for r in by_channel),
            "sample_qty": sum(r["sample_qty"] for r in by_channel),
            "sample_sku_count": sum(r["sample_sku_count"] for r in by_channel),
            "sample_cogs_amount": round(sum(r["sample_cogs_amount"] for r in by_channel), 2),
            "total_net_sales": total_net_sales,
        }
        total["sample_cogs_pct_of_total_net_sales"] = round(total["sample_cogs_amount"] / total_net_sales * 100, 2) if total_net_sales else 0

        mtd_total_cogs = sum(safe_float(r.get("sample_cogs_amount_mtd")) for r in mtd_rows)
        mtd = {
            "from_date": report_date[:8] + "01",
            "to_date": report_date,
            "by_channel": [
                {
                    "channel": str(r.get("channel") or "").upper(),
                    "sample_order_count_mtd": int(r.get("sample_order_count_mtd") or 0),
                    "sample_qty_mtd": safe_float(r.get("sample_qty_mtd")),
                    "sample_sku_count_mtd": int(r.get("sample_sku_count_mtd") or 0),
                    "sample_cogs_amount_mtd": safe_float(r.get("sample_cogs_amount_mtd")),
                }
                for r in mtd_rows
            ],
            "total": {
                "sample_order_count_mtd": sum(int(r.get("sample_order_count_mtd") or 0) for r in mtd_rows),
                "sample_qty_mtd": sum(safe_float(r.get("sample_qty_mtd")) for r in mtd_rows),
                "sample_sku_count_mtd": sum(int(r.get("sample_sku_count_mtd") or 0) for r in mtd_rows),
                "sample_cogs_amount_mtd": round(mtd_total_cogs, 2),
            }
        }

        return {
            "report_date": report_date,
            "source": "control_tower.sample_orders",
            "scope": "multi_channel_sample_orders",
            "total": total,
            "by_channel": by_channel,
            "status_breakdown": [
                {
                    "report_date": str(r.get("report_date")),
                    "channel": str(r.get("channel") or "").upper(),
                    "order_status": r.get("order_status") or "",
                    "sample_order_count": int(r.get("sample_order_count") or 0),
                    "sample_qty": safe_float(r.get("sample_qty")),
                    "sample_cogs_amount": safe_float(r.get("sample_cogs_amount")),
                }
                for r in status_rows
            ],
            "mtd": mtd,
            "notes": [
                "Đơn mẫu được tính từ control_tower.sample_orders.",
                "Nếu file không có SKU/quantity, quy ước 1 dòng = 1 đơn mẫu = 1 quantity mẫu.",
                "sample_cogs_pct_of_total_net_sales = sample_cogs_amount / tổng Net Sales trong scorecard_by_channel.",
                "sample_cogs_pct_of_channel_net_sales = sample_cogs_amount / Net Sales của kênh tương ứng.",
                "Chi phí đơn mẫu chưa bao gồm phí sàn/phí vận hành nếu file không cung cấp."
            ],
        }
    finally:
        cur.close()
        conn.close()


def markdown_append_samples(md: str, sample_block: Dict[str, Any]) -> str:
    lines = [md.rstrip(), "", "## Sample Orders / Đơn mẫu"]
    total = sample_block.get("total", {})
    lines.append("")
    lines.append(f"- Số đơn mẫu: **{total.get('sample_order_count', 0)}**")
    lines.append(f"- Số lượng mẫu: **{total.get('sample_qty', 0)}**")
    lines.append(f"- Giá vốn mẫu: **{total.get('sample_cogs_amount', 0):,.0f}**")
    lines.append(f"- % giá vốn mẫu / tổng Net Sales: **{total.get('sample_cogs_pct_of_total_net_sales', 0)}%**")
    lines.append("")
    lines.append("| Channel | Orders | Qty | COGS mẫu | Net Sales kênh | % mẫu / DS kênh |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for r in sample_block.get("by_channel", []):
        lines.append(
            f"| {r.get('channel')} | {r.get('sample_order_count',0)} | {r.get('sample_qty',0)} | "
            f"{r.get('sample_cogs_amount',0):,.0f} | {r.get('channel_net_sales',0):,.0f} | "
            f"{r.get('sample_cogs_pct_of_channel_net_sales',0)}% |"
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
                "build_ceo_control_tower_package_v2_8_samples",
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--out-dir", default="outbox/packages")
    ap.add_argument("--store-db", action="store_true")
    args = ap.parse_args()

    base_builder = load_base_builder()
    pkg = base_builder.build_package(args.date)
    pkg.setdefault("metadata", {})["sample_orders_policy"] = "multi_channel_v2_8"
    pkg["sample_orders"] = fetch_sample_orders_block(args.date, pkg)
    pkg.setdefault("instructions_for_agent", {})["sample_orders_source"] = "sample_orders"
    pkg.setdefault("instructions_for_agent", {})["sample_orders_rules"] = [
        "Tạo section riêng Đơn mẫu / Sample cost nếu sample_orders tồn tại.",
        "Không đưa đơn mẫu vào bảng 4 Scorecard theo kênh.",
        "Phân tích số đơn mẫu, số lượng mẫu, giá vốn mẫu, % mẫu / tổng doanh số và % mẫu / doanh số kênh.",
    ]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"ceo_control_tower_package_v2_8_samples_{args.date}.json"
    md_path = out_dir / f"ceo_control_tower_package_v2_8_samples_{args.date}.md"

    json_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if hasattr(base_builder, "markdown_package"):
        md = base_builder.markdown_package(pkg)
    else:
        md = f"# HH CEO Control Tower Package {args.date}\n"
    md = markdown_append_samples(md, pkg["sample_orders"])
    md_path.write_text(md, encoding="utf-8")

    result = {
        "status": "success",
        "json_path": str(json_path),
        "markdown_path": str(md_path),
        "sample_orders": pkg["sample_orders"]["total"],
    }
    if args.store_db:
        result.update(store_package(pkg, md_path))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
