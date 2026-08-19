#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEO Daily P&L package v4.7.3 — full previous-MTD same-period P&L.

Layered strictly on the current GitHub main v4.7.2 package builder.

v4.7.3 closes one reporting gap without changing existing business math:
- current report-date daily P&L remains unchanged;
- previous-month same-day full P&L from v4.7 remains unchanged;
- current MTD daily P&L remains unchanged;
- NEW: full previous-MTD same-period daily P&L and aggregate P&L are emitted
  using the exact same v4.6.1 financial/status logic as the current package;
- NEW: current-MTD vs previous-MTD P&L comparison lines are emitted for all
  five channels and TOTAL, ending at Profit/LỢI NHUẬN.

Example for report date 2026-08-18:
- current MTD:  2026-08-01 .. 2026-08-18
- previous MTD: 2026-07-01 .. 2026-07-18

Locked non-changes:
- no EBITDA;
- Profit remains the last P&L line;
- CM2 remains separate from Profit;
- no cancellation-rule changes;
- no target logic changes;
- no MT/GT customer changes;
- no Top20 SKU/inventory mapping changes;
- no inventory alert threshold changes.
"""
from __future__ import annotations

import argparse
import calendar
import copy
import datetime as dt
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

PACKAGE_VERSION = "v4.7.3"
PACKAGE_VERSION_INT = 473
BASE_FILENAME = "build_ceo_daily_pnl_package_v4_7_2.py"
CHANNELS: Tuple[str, ...] = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
EPSILON = 1.0

# Exact GitHub main blobs inspected before this upgrade was produced.
UPGRADE_BASIS_GITHUB_BLOBS = {
    "scripts/build_ceo_daily_pnl_package_v4_7_2.py": "714c83df86bf605ee1d0e8fa263cdbbbd996d9c7",
    "scripts/validate_ceo_daily_pnl_package_v4_7_2.py": "a48b2a57d1c5279b53801f00244b4793f1cb3a77",
    "scripts/build_ceo_daily_pnl_package_v4_7_1.py": "8ebf4a4aa6f415d340a837cb8de5bf45fcc7e284",
    "scripts/build_ceo_daily_pnl_package_v4_7.py": "bef819cbcc5d4521b3113e14ed61ceee49729892",
    "scripts/build_ceo_daily_pnl_package_v4_6_1.py": "9e7ce7c40b12f2aa5cab0d861afa2f4e8a4b8e7e",
}

# Additive P&L fields used by the strict previous-MTD reconciliation guard.
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


def _load_v472():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}; v4.7.3 must be installed on top of the current v4.7.2 builder"
        )
    spec = importlib.util.spec_from_file_location("hh_ceo_daily_pnl_v472_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        out = float(value)
        return default if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return default


def _add_months(value: dt.date, months: int) -> dt.date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(value.day, last_day))


def _periods(report_date: dt.date) -> Dict[str, Any]:
    current_start = report_date.replace(day=1)
    previous_end = _add_months(report_date, -1)
    previous_start = previous_end.replace(day=1)
    return {
        "current_mtd_start": current_start,
        "current_mtd_end": report_date,
        "previous_mtd_start": previous_start,
        "previous_mtd_end": previous_end,
        "current_day_count": (report_date - current_start).days + 1,
        "previous_day_count": (previous_end - previous_start).days + 1,
    }


def _expected_dates(start: dt.date, end: dt.date) -> List[str]:
    out: List[str] = []
    day = start
    while day <= end:
        out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


def _aggregate_channel(base, rows: Sequence[Mapping[str, Any]], period_end: dt.date, channel: str) -> Dict[str, Any]:
    summary = dict(base.aggregate(list(rows), period_end))
    summary["channel"] = channel
    return base.clean(summary)


def _aggregate_total(base, by_channel: Mapping[str, Mapping[str, Any]], period_end: dt.date) -> Dict[str, Any]:
    summary = dict(base.aggregate([by_channel[ch] for ch in CHANNELS], period_end))
    summary["channel"] = "TOTAL"
    return base.clean(summary)


def _sum_rows(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(_f(row.get(field)) for row in rows)


def _coverage_issues(rows: Sequence[Mapping[str, Any]], start: dt.date, end: dt.date) -> List[str]:
    expected = _expected_dates(start, end)
    actual = [str(r.get("report_date") or "") for r in rows]
    issues: List[str] = []
    if len(actual) != len(expected):
        issues.append(f"row_count={len(actual)}, expected={len(expected)}")
    if len(set(actual)) != len(actual):
        issues.append("duplicate report_date rows")
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        issues.append(f"missing_dates={missing}")
    if extra:
        issues.append(f"unexpected_dates={extra}")
    return issues


def _fetch_previous_mtd_full_pnl(
    v472,
    report_date: dt.date,
    vat_rates: Mapping[str, float],
) -> Tuple[Any, Dict[str, Any]]:
    """Rebuild previous same-period MTD with exact v4.6.1 business semantics.

    This intentionally calls v4.6.1 for previous_mtd_end rather than trying to
    derive prior-period costs from the current package. That guarantees the same
    cancellation, COGS, CM1, CM2 and Profit logic used by the canonical daily P&L.
    """
    periods = _periods(report_date)
    previous_start: dt.date = periods["previous_mtd_start"]
    previous_end: dt.date = periods["previous_mtd_end"]

    v471 = v472._load_v471()
    v47 = v471._load_v47()
    v461 = v47._load_v461()
    base_prev, prev_pkg = v461.build_package_v461(
        previous_end.isoformat(),
        "previous_day",
        None,
        vat_rates,
        include_growth_context=False,
    )

    source_detail = prev_pkg.get("mtd_daily_detail_by_channel") or {}
    daily: Dict[str, List[Dict[str, Any]]] = {}
    by_channel: Dict[str, Dict[str, Any]] = {}
    for ch in CHANNELS:
        rows = copy.deepcopy((source_detail.get(ch) or []) if isinstance(source_detail, dict) else [])
        daily[ch] = rows
        by_channel[ch] = _aggregate_channel(base_prev, rows, previous_end, ch)
        by_channel[ch]["period_start"] = previous_start.isoformat()
        by_channel[ch]["period_end"] = previous_end.isoformat()
        by_channel[ch]["day_count"] = periods["previous_day_count"]

    total = _aggregate_total(base_prev, by_channel, previous_end)
    total["period_start"] = previous_start.isoformat()
    total["period_end"] = previous_end.isoformat()
    total["day_count"] = periods["previous_day_count"]

    source_dq = copy.deepcopy(prev_pkg.get("data_quality") or {})
    block = {
        "available": True,
        "period_start": previous_start.isoformat(),
        "period_end": previous_end.isoformat(),
        "day_count": periods["previous_day_count"],
        "comparison_basis": "same calendar-day coverage in previous month",
        "source_builder": "build_ceo_daily_pnl_package_v4_6_1.py",
        "source_semantics": "exact canonical v4.6.1 daily P&L/status/cost semantics; include_growth_context=false",
        "source_data_quality": source_dq,
        "daily_detail_by_channel": daily,
        "pnl_by_channel": by_channel,
        "summary": total,
    }
    return base_prev, base_prev.clean(block)


def _build_current_mtd(base, package: Mapping[str, Any], report_date: dt.date) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    detail = package.get("mtd_daily_detail_by_channel") or {}
    by_channel: Dict[str, Dict[str, Any]] = {}
    for ch in CHANNELS:
        rows = (detail.get(ch) or []) if isinstance(detail, dict) else []
        by_channel[ch] = _aggregate_channel(base, rows, report_date, ch)
    total = _aggregate_total(base, by_channel, report_date)
    return by_channel, total


def _build_mtd_comparison(
    base,
    report_date: dt.date,
    current_by_channel: Mapping[str, Mapping[str, Any]],
    current_total: Mapping[str, Any],
    previous_block: Mapping[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    periods = _periods(report_date)
    previous_by_channel = previous_block.get("pnl_by_channel") or {}
    previous_total = previous_block.get("summary") or {}

    current_period = {
        "start": periods["current_mtd_start"].isoformat(),
        "end": periods["current_mtd_end"].isoformat(),
        "day_count": periods["current_day_count"],
    }
    previous_period = {
        "start": periods["previous_mtd_start"].isoformat(),
        "end": periods["previous_mtd_end"].isoformat(),
        "day_count": periods["previous_day_count"],
    }

    by_channel: Dict[str, Dict[str, Any]] = {}
    for ch in CHANNELS:
        cur = copy.deepcopy(dict(current_by_channel.get(ch) or {}))
        prev = copy.deepcopy(dict(previous_by_channel.get(ch) or {}))
        cur["channel"] = ch
        prev["channel"] = ch
        lines = base.pnl_lines(cur, prev)
        by_channel[ch] = {
            "channel": ch,
            "current_period": copy.deepcopy(current_period),
            "previous_period": copy.deepcopy(previous_period),
            "current_mtd": cur,
            "previous_mtd": prev,
            "pnl_lines": lines,
            "comparison_rule": "Current MTD vs same-calendar-day MTD of previous month; full P&L through Profit.",
        }

    cur_total = copy.deepcopy(dict(current_total))
    prev_total = copy.deepcopy(dict(previous_total))
    cur_total["channel"] = "TOTAL"
    prev_total["channel"] = "TOTAL"
    total = {
        "channel": "TOTAL",
        "current_period": current_period,
        "previous_period": previous_period,
        "current_mtd": cur_total,
        "previous_mtd": prev_total,
        "pnl_lines": base.pnl_lines(cur_total, prev_total),
        "comparison_rule": "Company TOTAL current MTD vs same-calendar-day previous MTD; full P&L through Profit.",
    }
    return base.clean(by_channel), base.clean(total)


def _append_error(package: MutableMapping[str, Any], text: str) -> None:
    dq = package.setdefault("data_quality", {})
    errors = dq.setdefault("errors", [])
    if text not in errors:
        errors.append(text)


def _append_warning(package: MutableMapping[str, Any], text: str) -> None:
    dq = package.setdefault("data_quality", {})
    warnings = dq.setdefault("warnings", [])
    if text not in warnings:
        warnings.append(text)


def _postprocess_v473(
    package: MutableMapping[str, Any],
    base,
    report_date: dt.date,
    previous_block: Mapping[str, Any],
) -> None:
    periods = _periods(report_date)
    current_by_channel, current_total = _build_current_mtd(base, package, report_date)
    comparison_by_channel, comparison_total = _build_mtd_comparison(
        base,
        report_date,
        current_by_channel,
        current_total,
        previous_block,
    )

    package["previous_mtd_daily_detail_by_channel"] = copy.deepcopy(previous_block.get("daily_detail_by_channel") or {})
    package["previous_mtd_pnl_by_channel"] = copy.deepcopy(previous_block.get("pnl_by_channel") or {})
    package["previous_mtd_summary"] = copy.deepcopy(previous_block.get("summary") or {})
    package["previous_mtd_source_data_quality"] = copy.deepcopy(previous_block.get("source_data_quality") or {})
    package["mtd_pnl_comparison_by_channel"] = comparison_by_channel
    package["mtd_pnl_comparison_total"] = comparison_total

    meta = package.setdefault("metadata", {})
    meta["package_version"] = PACKAGE_VERSION
    meta["package_version_int"] = PACKAGE_VERSION_INT
    meta["upgrade_scope_v4_7_3"] = "Full previous-MTD same-period P&L + current-vs-previous MTD full P&L comparison"
    meta["v4_7_3_hotfix_revision"] = "HF1"
    meta["upgrade_basis_github_blobs_v4_7_3"] = copy.deepcopy(UPGRADE_BASIS_GITHUB_BLOBS)
    meta["mtd_comparison_periods"] = {
        "current_start": periods["current_mtd_start"].isoformat(),
        "current_end": periods["current_mtd_end"].isoformat(),
        "previous_start": periods["previous_mtd_start"].isoformat(),
        "previous_end": periods["previous_mtd_end"].isoformat(),
    }

    contract = package.setdefault("excel_render_contract", {})
    contract["mtd_current_full_pnl_source"] = "mtd_daily_detail_by_channel"
    contract["mtd_previous_full_pnl_source"] = "previous_mtd_daily_detail_by_channel"
    contract["mtd_previous_summary_source"] = "previous_mtd_pnl_by_channel / previous_mtd_summary"
    contract["mtd_pnl_comparison_source"] = "mtd_pnl_comparison_by_channel"
    contract["mtd_pnl_total_comparison_source"] = "mtd_pnl_comparison_total"
    contract["mtd_comparison_rule"] = (
        "Compare month-start..report_date with previous-month-start..same calendar day. "
        "Use the full P&L waterfall through LỢI NHUẬN; never substitute daily_pnl_by_channel_map.<CHANNEL>.previous."
    )
    contract["mtd_pnl_last_line"] = "LỢI NHUẬN"
    contract["mtd_profit_last_line_only"] = True

    policy = package.setdefault("report_rendering_policy", {})
    policy["mtd_same_period_comparison"] = {
        "current_source": "mtd_daily_detail_by_channel",
        "previous_source": "previous_mtd_daily_detail_by_channel",
        "comparison_source": "mtd_pnl_comparison_by_channel",
        "current_period_start": periods["current_mtd_start"].isoformat(),
        "current_period_end": periods["current_mtd_end"].isoformat(),
        "previous_period_start": periods["previous_mtd_start"].isoformat(),
        "previous_period_end": periods["previous_mtd_end"].isoformat(),
        "full_waterfall": "Gross Sales -> Net Sales -> GM1 -> CM1 -> CM2 -> LỢI NHUẬN",
        "daily_previous_warning": "daily_pnl_by_channel_map.<CHANNEL>.previous follows compare_mode and is NOT the previous-MTD source.",
        "profit_last_line_only": True,
    }
    package.setdefault("pnl_rules", {})["v4_7_3_scope"] = (
        "No P&L formula change. v4.7.3 rebuilds the previous same-period MTD using exact v4.6.1 semantics and exposes a full MTD comparison contract."
    )

    # Strict source-period data quality. Missing prior-period P&L must block send;
    # silently falling back to current/previous-day comparison would be wrong.
    source_dq = previous_block.get("source_data_quality") or {}
    if source_dq.get("can_send") is False or str(source_dq.get("status") or "").upper() == "ERROR":
        _append_error(package, "v4.7.3 previous-MTD source package has ERROR/can_send=false.")
    elif str(source_dq.get("status") or "").upper() == "WARNING":
        _append_warning(package, "v4.7.3 previous-MTD source package has WARNING; inspect previous_mtd_source_data_quality.")

    prev_daily = package.get("previous_mtd_daily_detail_by_channel") or {}
    prev_by_channel = package.get("previous_mtd_pnl_by_channel") or {}
    for ch in CHANNELS:
        rows = (prev_daily.get(ch) or []) if isinstance(prev_daily, dict) else []
        issues = _coverage_issues(rows, periods["previous_mtd_start"], periods["previous_mtd_end"])
        if issues:
            _append_error(package, f"v4.7.3 {ch} previous-MTD daily coverage invalid: {'; '.join(issues)}")
        summary = (prev_by_channel.get(ch) or {}) if isinstance(prev_by_channel, dict) else {}
        for field in RECON_FIELDS:
            daily_sum = _sum_rows(rows, field)
            if abs(daily_sum - _f(summary.get(field))) > EPSILON:
                _append_error(
                    package,
                    f"v4.7.3 {ch} previous-MTD {field} summary mismatch: daily={daily_sum:,.2f}, summary={_f(summary.get(field)):,.2f}",
                )

    prev_total = package.get("previous_mtd_summary") or {}
    for field in RECON_FIELDS:
        channel_sum = sum(_f((prev_by_channel.get(ch) or {}).get(field)) for ch in CHANNELS)
        if abs(channel_sum - _f(prev_total.get(field))) > EPSILON:
            _append_error(
                package,
                f"v4.7.3 TOTAL previous-MTD {field} mismatch: channels={channel_sum:,.2f}, total={_f(prev_total.get(field)):,.2f}",
            )

    for ch in CHANNELS:
        lines = ((comparison_by_channel.get(ch) or {}).get("pnl_lines") or [])
        if not lines or str(lines[-1].get("code") or "").lower() != "profit":
            _append_error(package, f"v4.7.3 {ch} MTD comparison P&L does not end at Profit/LỢI NHUẬN.")
    total_lines = comparison_total.get("pnl_lines") or []
    if not total_lines or str(total_lines[-1].get("code") or "").lower() != "profit":
        _append_error(package, "v4.7.3 TOTAL MTD comparison P&L does not end at Profit/LỢI NHUẬN.")

    dq = package.setdefault("data_quality", {})
    dq["v4_7_3_scope"] = {
        "current_mtd_start": periods["current_mtd_start"].isoformat(),
        "current_mtd_end": periods["current_mtd_end"].isoformat(),
        "previous_mtd_start": periods["previous_mtd_start"].isoformat(),
        "previous_mtd_end": periods["previous_mtd_end"].isoformat(),
        "previous_mtd_rows_by_channel": {
            ch: len((prev_daily.get(ch) or []) if isinstance(prev_daily, dict) else []) for ch in CHANNELS
        },
        "full_previous_mtd_pnl_available": True,
        "mtd_comparison_channels": list(CHANNELS),
        "pnl_last_line": "profit",
    }

    # Reuse the existing v4.7 DQ status calculator rather than creating a new one.
    v472 = _load_v472()
    v471 = v472._load_v471()
    v47 = v471._load_v47()
    if hasattr(v47, "_refresh_dq_status"):
        v47._refresh_dq_status(package)


def build_package_v473(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    include_growth_context: bool = True,
    require_targets: bool = True,
    require_inventory: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    v472 = _load_v472()
    base, package = v472.build_package_v472(
        report_date_str,
        compare_mode,
        explicit_compare_date,
        vat_rates,
        include_growth_context=include_growth_context,
        require_targets=require_targets,
        require_inventory=require_inventory,
    )
    report_date = base.parse_date(report_date_str)
    _base_prev, previous_block = _fetch_previous_mtd_full_pnl(v472, report_date, vat_rates)
    _postprocess_v473(package, base, report_date, previous_block)
    base.PACKAGE_VERSION = PACKAGE_VERSION
    base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
    return base, base.clean(package)


def self_test() -> int:
    failures: List[str] = []

    p = _periods(dt.date(2026, 8, 18))
    if p["current_mtd_start"] != dt.date(2026, 8, 1) or p["previous_mtd_end"] != dt.date(2026, 7, 18):
        failures.append("August same-period bounds")
    if p["current_day_count"] != 18 or p["previous_day_count"] != 18:
        failures.append("same-period day count")

    # Calendar clamp is required for 29/30/31 day report dates.
    p2 = _periods(dt.date(2026, 3, 31))
    if p2["previous_mtd_end"] != dt.date(2026, 2, 28):
        failures.append("calendar clamp 31-Mar -> 28-Feb")
    if p2["previous_day_count"] != 28:
        failures.append("February day count")

    good_rows = [{"report_date": f"2026-07-{day:02d}", "net_sales": day} for day in range(1, 19)]
    if _coverage_issues(good_rows, dt.date(2026, 7, 1), dt.date(2026, 7, 18)):
        failures.append("valid coverage incorrectly rejected")
    bad_rows = good_rows[:-1]
    if not _coverage_issues(bad_rows, dt.date(2026, 7, 1), dt.date(2026, 7, 18)):
        failures.append("missing previous-MTD date not detected")

    if abs(_sum_rows(good_rows, "net_sales") - sum(range(1, 19))) > 1e-9:
        failures.append("daily additive reconciliation helper")

    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for item in failures:
            print("-", item, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Previous-MTD period bounds: PASS")
    print("31-day calendar clamp: PASS")
    print("Daily coverage guard: PASS")
    print("Additive reconciliation helper: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="Report date YYYY-MM-DD")
    ap.add_argument(
        "--compare-mode",
        choices=("previous_day", "previous_week_same_day", "previous_month_same_day"),
        default="previous_day",
    )
    ap.add_argument("--compare-date", default=None)
    ap.add_argument("--vat-rates", default=os.getenv("DAILY_PNL_VAT_RATES", ""))
    ap.add_argument("--require-vat-rates", action="store_true")
    ap.add_argument("--without-growth-context", action="store_true")
    ap.add_argument("--allow-missing-targets", action="store_true")
    ap.add_argument("--allow-missing-inventory", action="store_true")
    ap.add_argument("--out-dir", default="outbox/packages")
    ap.add_argument("--store-db", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.date:
        ap.error("--date is required unless --self-test is used")

    v472 = _load_v472()
    v471 = v472._load_v471()
    v47 = v471._load_v47()
    v461 = v47._load_v461()
    v46 = v461._load_v46()
    v45 = v46._load_base()
    try:
        report_date = v45.parse_date(args.date).isoformat()
        if args.compare_date:
            v45.parse_date(args.compare_date)
        vat_rates = v45.parse_vat_rates(args.vat_rates)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.require_vat_rates:
        missing = [ch for ch in v45.DAILY_PNL_SHEETS if ch not in vat_rates]
        if missing:
            print("ERROR: missing VAT rates for " + ", ".join(missing), file=sys.stderr)
            return 2

    base, package = build_package_v473(
        report_date,
        args.compare_mode,
        args.compare_date,
        vat_rates,
        include_growth_context=not args.without_growth_context,
        require_targets=not args.allow_missing_targets,
        require_inventory=not args.allow_missing_inventory,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"ceo_daily_pnl_package_v4_7_3_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_7_3_{report_date}.md"
    json_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2, default=base.json_default),
        encoding="utf-8",
    )

    md = base.markdown_summary(package)
    periods = _periods(base.parse_date(report_date))
    md += "\n\n## v4.7.3 Full Previous-MTD Same-Period P&L\n"
    md += f"- Current MTD: {periods['current_mtd_start']} .. {periods['current_mtd_end']}\n"
    md += f"- Previous MTD: {periods['previous_mtd_start']} .. {periods['previous_mtd_end']}\n"
    md += "- Channels: GT, MT, NHANH, SHOPEE, TIKTOK + TOTAL\n"
    md += "- Full waterfall: Gross -> Net Sales -> GM1 -> CM1 -> CM2 -> LỢI NHUẬN\n"
    md += "- EBITDA: forbidden / not calculated\n"
    md_path.write_text(md, encoding="utf-8")

    if args.store_db:
        base.PACKAGE_VERSION = PACKAGE_VERSION
        base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
        base.store_best_effort(report_date, package, md, str(json_path), str(md_path))

    dq = package.get("data_quality") or {}
    print(f"JSON package: {json_path}")
    print(f"Markdown summary: {md_path}")
    print(f"Package version: {PACKAGE_VERSION}")
    print(f"Current MTD: {periods['current_mtd_start']} -> {periods['current_mtd_end']}")
    print(f"Previous MTD: {periods['previous_mtd_start']} -> {periods['previous_mtd_end']}")
    print(f"Previous-MTD rows/channel: {periods['previous_day_count']}")
    print(f"Data quality: {dq.get('status')}")
    for warning in dq.get("warnings") or []:
        print(f"WARNING: {warning}")
    for error in dq.get("errors") or []:
        print(f"ERROR: {error}")
    return 0 if dq.get("can_send") else 1


if __name__ == "__main__":
    raise SystemExit(main())
