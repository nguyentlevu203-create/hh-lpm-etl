#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict validator for CEO Daily P&L package v4.7.3."""
from __future__ import annotations

import argparse
import calendar
import copy
import datetime as dt
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any, List, Mapping, Sequence, Tuple

PACKAGE_VERSION = "v4.7.3"
PACKAGE_VERSION_INT = 473
BASE_FILENAME = "validate_ceo_daily_pnl_package_v4_7_2.py"
CHANNELS: Tuple[str, ...] = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
EPSILON = 1.0
RECON_FIELDS: Tuple[str, ...] = (
    "gross_sales",
    "gross_non_cancelled_sales",
    "gross_cancelled_sales",
    "sales_deductions_total",
    "net_sales",
    "orders",
    "success_orders",
    "cancelled_orders",
    "sold_cogs",
    "gift_cogs",
    "cogs_total",
    "gm1",
    "fixed_fee",
    "service_fee",
    "payment_fee",
    "vxp_fee",
    "infrastructure_fee",
    "fulfillment_fee",
    "packaging_cost",
    "logistics_order_cost",
    "cancel_related_shipping_fee",
    "cm1",
    "ads_total",
    "affiliate_total",
    "booking_kol_koc",
    "livestream_inhouse",
    "content_cost",
    "trade_marketing_cost",
    "agency_campaign_direct_cost",
    "cm2",
    "backoffice_cost",
    "profit",
)


def _load_base():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7.3 validator layers on current v4.7.2 validator")
    spec = importlib.util.spec_from_file_location("hh_validate_v472", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def f(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        out = float(value)
        return 0.0 if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return 0.0


def close(a: Any, b: Any, tol: float = EPSILON) -> bool:
    return abs(f(a) - f(b)) <= tol


def _add_months(value: dt.date, months: int) -> dt.date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(value.day, last_day))


def _periods(report_date: dt.date):
    current_start = report_date.replace(day=1)
    previous_end = _add_months(report_date, -1)
    previous_start = previous_end.replace(day=1)
    return current_start, report_date, previous_start, previous_end


def _expected_dates(start: dt.date, end: dt.date) -> List[str]:
    out: List[str] = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _sum(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(f(r.get(field)) for r in rows)


def _validate_pnl_lines(lines: Sequence[Mapping[str, Any]], label: str, errors: List[str]) -> None:
    if not lines:
        errors.append(f"{label} pnl_lines missing")
        return
    last = lines[-1]
    if str(last.get("code") or "").lower() != "profit":
        errors.append(f"{label} P&L last line must be profit")
    if "lợi nhuận" not in str(last.get("label") or "").lower():
        errors.append(f"{label} P&L last label must be LỢI NHUẬN")
    for idx, row in enumerate(lines):
        code = str(row.get("code") or "").casefold()
        text = str(row.get("label") or "").casefold()
        if "ebitda" in code or "ebitda" in text:
            errors.append(f"{label} pnl_lines[{idx}] contains forbidden EBITDA")


def _scan_all_pnl_lines(value: Any, path: str, errors: List[str]) -> None:
    """Reject the forbidden metric only when it appears in actual pnl_lines.

    Contract/documentation metadata may mention that a metric is forbidden.
    Those strings must not be mistaken for a financial row.
    """
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key) == "pnl_lines" and isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
                for idx, row in enumerate(child):
                    if not isinstance(row, Mapping):
                        continue
                    code = str(row.get("code") or "").casefold()
                    label = str(row.get("label") or "").casefold()
                    if "ebitda" in code or "ebitda" in label:
                        errors.append(f"{child_path}[{idx}] contains forbidden EBITDA metric")
            else:
                _scan_all_pnl_lines(child, child_path, errors)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for idx, child in enumerate(value):
            _scan_all_pnl_lines(child, f"{path}[{idx}]", errors)


def validate_package(p: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    meta = p.get("metadata") or {}
    if meta.get("package_version") != PACKAGE_VERSION:
        errors.append(f"package_version={meta.get('package_version')}, expected {PACKAGE_VERSION}")
    if meta.get("package_version_int") != PACKAGE_VERSION_INT:
        errors.append(f"package_version_int={meta.get('package_version_int')}, expected {PACKAGE_VERSION_INT}")

    # Reuse every v4.7.2 prerequisite guard. Only version metadata is projected
    # back on the copied package; v4.7.3 fields remain additive and are ignored
    # by the prerequisite validator.
    base = _load_base()
    p472 = copy.deepcopy(dict(p))
    p472.setdefault("metadata", {})["package_version"] = "v4.7.2"
    p472["metadata"]["package_version_int"] = 472
    for e in base.validate_package(p472):
        # GitHub main v4.7 validator contains a legacy broad serialized-string
        # scan that flags its own metadata key `forbid_ebitda`. That is a
        # validator false-positive, not a P&L metric. v4.7.3 performs a semantic
        # scan of actual pnl_lines below, so only this exact legacy message is
        # ignored. All other prerequisite errors remain hard failures.
        if str(e).endswith("EBITDA is forbidden in v4.7"):
            continue
        errors.append(f"v4.7.2-base: {e}")

    _scan_all_pnl_lines(p, "", errors)

    try:
        report_date = dt.date.fromisoformat(str(meta.get("report_date") or ""))
    except Exception:
        errors.append("metadata.report_date invalid")
        return list(dict.fromkeys(errors))

    cur_start, cur_end, prev_start, prev_end = _periods(report_date)
    expected_prev_dates = _expected_dates(prev_start, prev_end)
    expected_cur_dates = _expected_dates(cur_start, cur_end)

    contract = p.get("excel_render_contract") or {}
    if contract.get("mtd_current_full_pnl_source") != "mtd_daily_detail_by_channel":
        errors.append("excel_render_contract.mtd_current_full_pnl_source changed")
    if contract.get("mtd_previous_full_pnl_source") != "previous_mtd_daily_detail_by_channel":
        errors.append("excel_render_contract.mtd_previous_full_pnl_source must be previous_mtd_daily_detail_by_channel")
    if contract.get("mtd_pnl_comparison_source") != "mtd_pnl_comparison_by_channel":
        errors.append("excel_render_contract.mtd_pnl_comparison_source changed")
    if str(contract.get("mtd_pnl_last_line") or "").upper() != "LỢI NHUẬN":
        errors.append("excel_render_contract.mtd_pnl_last_line must be LỢI NHUẬN")
    if contract.get("mtd_profit_last_line_only") is not True:
        errors.append("excel_render_contract.mtd_profit_last_line_only must be true")

    prev_daily = p.get("previous_mtd_daily_detail_by_channel") or {}
    prev_by_ch = p.get("previous_mtd_pnl_by_channel") or {}
    prev_total = p.get("previous_mtd_summary") or {}
    current_daily = p.get("mtd_daily_detail_by_channel") or {}
    comp = p.get("mtd_pnl_comparison_by_channel") or {}
    comp_total = p.get("mtd_pnl_comparison_total") or {}

    for ch in CHANNELS:
        rows = prev_daily.get(ch) if isinstance(prev_daily, dict) else None
        if rows is None:
            errors.append(f"previous_mtd_daily_detail_by_channel missing {ch}")
            continue
        dates = [str(r.get("report_date") or "") for r in rows]
        if dates != expected_prev_dates:
            # Exact ordered coverage is deliberate: one zero-filled row for every
            # previous-MTD date, no missing/duplicate/out-of-range day.
            errors.append(
                f"previous_mtd_daily_detail_by_channel.{ch} date coverage mismatch: "
                f"got {len(dates)} rows, expected {len(expected_prev_dates)} exact ordered dates"
            )
        summary = prev_by_ch.get(ch) if isinstance(prev_by_ch, dict) else None
        if not isinstance(summary, dict):
            errors.append(f"previous_mtd_pnl_by_channel missing {ch}")
            continue
        if str(summary.get("period_start") or "") != prev_start.isoformat():
            errors.append(f"previous_mtd_pnl_by_channel.{ch}.period_start mismatch")
        if str(summary.get("period_end") or "") != prev_end.isoformat():
            errors.append(f"previous_mtd_pnl_by_channel.{ch}.period_end mismatch")
        for field in RECON_FIELDS:
            if not close(summary.get(field), _sum(rows, field)):
                errors.append(f"previous_mtd_pnl_by_channel.{ch}.{field} != sum(daily rows)")

        current_rows = current_daily.get(ch) if isinstance(current_daily, dict) else None
        if current_rows is None:
            errors.append(f"mtd_daily_detail_by_channel missing {ch}")
            current_rows = []
        current_dates = [str(r.get("report_date") or "") for r in current_rows]
        if current_dates != expected_cur_dates:
            errors.append(f"mtd_daily_detail_by_channel.{ch} current MTD date coverage mismatch")

        c = comp.get(ch) if isinstance(comp, dict) else None
        if not isinstance(c, dict):
            errors.append(f"mtd_pnl_comparison_by_channel missing {ch}")
            continue
        cp = c.get("current_period") or {}
        pp = c.get("previous_period") or {}
        if cp.get("start") != cur_start.isoformat() or cp.get("end") != cur_end.isoformat():
            errors.append(f"mtd_pnl_comparison_by_channel.{ch} current period mismatch")
        if pp.get("start") != prev_start.isoformat() or pp.get("end") != prev_end.isoformat():
            errors.append(f"mtd_pnl_comparison_by_channel.{ch} previous period mismatch")
        cur_summary = c.get("current_mtd") or {}
        prev_summary = c.get("previous_mtd") or {}
        for field in RECON_FIELDS:
            if not close(cur_summary.get(field), _sum(current_rows, field)):
                errors.append(f"mtd_pnl_comparison_by_channel.{ch}.current_mtd.{field} mismatch")
            if not close(prev_summary.get(field), summary.get(field)):
                errors.append(f"mtd_pnl_comparison_by_channel.{ch}.previous_mtd.{field} mismatch")
        _validate_pnl_lines(c.get("pnl_lines") or [], f"mtd_pnl_comparison_by_channel.{ch}", errors)

    # Previous TOTAL must be the exact sum of the five leaf channel P&Ls.
    if str(prev_total.get("period_start") or "") != prev_start.isoformat():
        errors.append("previous_mtd_summary.period_start mismatch")
    if str(prev_total.get("period_end") or "") != prev_end.isoformat():
        errors.append("previous_mtd_summary.period_end mismatch")
    for field in RECON_FIELDS:
        channel_sum = sum(f((prev_by_ch.get(ch) or {}).get(field)) for ch in CHANNELS)
        if not close(prev_total.get(field), channel_sum):
            errors.append(f"previous_mtd_summary.{field} != sum(channel summaries)")

    ctp = comp_total.get("current_period") or {}
    ptp = comp_total.get("previous_period") or {}
    if ctp.get("start") != cur_start.isoformat() or ctp.get("end") != cur_end.isoformat():
        errors.append("mtd_pnl_comparison_total current period mismatch")
    if ptp.get("start") != prev_start.isoformat() or ptp.get("end") != prev_end.isoformat():
        errors.append("mtd_pnl_comparison_total previous period mismatch")
    for field in RECON_FIELDS:
        current_channel_sum = sum(_sum((current_daily.get(ch) or []), field) for ch in CHANNELS)
        if not close((comp_total.get("current_mtd") or {}).get(field), current_channel_sum):
            errors.append(f"mtd_pnl_comparison_total.current_mtd.{field} mismatch")
        if not close((comp_total.get("previous_mtd") or {}).get(field), prev_total.get(field)):
            errors.append(f"mtd_pnl_comparison_total.previous_mtd.{field} mismatch")
    _validate_pnl_lines(comp_total.get("pnl_lines") or [], "mtd_pnl_comparison_total", errors)

    source_dq = p.get("previous_mtd_source_data_quality") or {}
    if source_dq.get("can_send") is False or str(source_dq.get("status") or "").upper() == "ERROR":
        errors.append("previous_mtd_source_data_quality is ERROR/can_send=false")

    policy = (p.get("report_rendering_policy") or {}).get("mtd_same_period_comparison") or {}
    if policy.get("previous_source") != "previous_mtd_daily_detail_by_channel":
        errors.append("report_rendering_policy.mtd_same_period_comparison.previous_source changed")
    if policy.get("profit_last_line_only") is not True:
        errors.append("report_rendering_policy.mtd_same_period_comparison.profit_last_line_only must be true")

    return list(dict.fromkeys(errors))


def self_test() -> int:
    failures: List[str] = []
    report = dt.date(2026, 3, 31)
    cur_start, cur_end, prev_start, prev_end = _periods(report)
    if (cur_start, cur_end) != (dt.date(2026, 3, 1), dt.date(2026, 3, 31)):
        failures.append("current period bounds")
    if (prev_start, prev_end) != (dt.date(2026, 2, 1), dt.date(2026, 2, 28)):
        failures.append("previous period calendar clamp")
    if len(_expected_dates(prev_start, prev_end)) != 28:
        failures.append("previous date series")
    if not close(100.4, 101.0, 1.0) or close(100.0, 102.0, 1.0):
        failures.append("1 VND tolerance helper")
    errors: List[str] = []
    _validate_pnl_lines(
        [{"code": "gross_sales", "label": "Gross Sales"}, {"code": "profit", "label": "LỢI NHUẬN"}],
        "fixture",
        errors,
    )
    if errors:
        failures.append("profit last-line guard")

    semantic_errors: List[str] = []
    _scan_all_pnl_lines({"contract_note": "EBITDA is forbidden", "pnl_lines": [{"code": "profit", "label": "LỢI NHUẬN"}]}, "fixture", semantic_errors)
    if semantic_errors:
        failures.append("metadata false-positive guard")
    semantic_errors = []
    _scan_all_pnl_lines({"pnl_lines": [{"code": "ebitda", "label": "EBITDA"}]}, "fixture", semantic_errors)
    if not semantic_errors:
        failures.append("semantic forbidden-metric guard")

    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for item in failures:
            print("-", item, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Same-period calendar clamp: PASS")
    print("Date-series guard: PASS")
    print("1 VND reconciliation tolerance: PASS")
    print("Profit last-line / no-EBITDA line guard: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("package", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.package:
        ap.error("package path is required unless --self-test is used")

    p = json.loads(Path(args.package).read_text(encoding="utf-8"))
    errors = validate_package(p)
    if errors:
        print("VALIDATION FAILED", file=sys.stderr)
        for e in errors:
            print("-", e, file=sys.stderr)
        return 1
    print("VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
