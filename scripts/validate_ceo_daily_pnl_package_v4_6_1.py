#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict validator for CEO Daily P&L package v4.6.1."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, List, Mapping

BASE_FILENAME = "validate_ceo_daily_pnl_package_v4_6.py"
PACKAGE_VERSION = "v4.6.1"
PACKAGE_VERSION_INT = 461

NHANH_CANCEL = {"Đã hủy", "Hệ Thống hủy", "Khách hủy", "HVC hủy"}
NHANH_RETURN = {"Đang hoàn", "Xác nhận hoàn", "Đã hoàn"}
NHANH_FAILED = {"Thất bại"}


def _load_base_validator():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.6.1 validator layers on v4.6 validator")
    spec = importlib.util.spec_from_file_location("hh_validate_v46", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canon(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def tokens(value: Any) -> set[str]:
    return set(re.findall(r"\w+", canon(value), flags=re.UNICODE))


def cancel_like(value: Any) -> bool:
    return bool(tokens(value) & {"hủy", "huỷ", "huy", "cancel", "canceled", "cancelled"})


def validate_package(p: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    meta = p.get("metadata") or {}
    dq = p.get("data_quality") or {}

    if meta.get("package_version") != PACKAGE_VERSION:
        errors.append(f"package_version={meta.get('package_version')}, expected {PACKAGE_VERSION}")
    if meta.get("package_version_int") != PACKAGE_VERSION_INT:
        errors.append(f"package_version_int={meta.get('package_version_int')}, expected {PACKAGE_VERSION_INT}")

    # Reuse every v4.6 financial/reconciliation guard after substituting only
    # the version fields expected by that validator.
    base = _load_base_validator()
    p46 = copy.deepcopy(dict(p))
    p46.setdefault("metadata", {})["package_version"] = "v4.6"
    p46["metadata"]["package_version_int"] = 46
    base_errors = base.validate_package(p46)
    errors.extend(f"v4.6-base: {e}" for e in base_errors)

    contract = dq.get("status_rule_by_channel") or p.get("status_rule_by_channel") or {}
    ncontract = contract.get("NHANH") or {}
    normalization = str(ncontract.get("normalization") or "")
    for required in ("case-insensitive", "trim/collapse whitespace", "no accent folding/substrings"):
        if required not in normalization:
            errors.append(f"Nhanh normalization contract missing {required!r}: {normalization}")

    cancel_keys = {canon(x) for x in NHANH_CANCEL}
    return_keys = {canon(x) for x in NHANH_RETURN}
    failed_keys = {canon(x) for x in NHANH_FAILED}
    naud = p.get("nhanh_status_audit") or {}
    if not naud.get("available"):
        errors.append("nhanh_status_audit unavailable")
    if naud.get("unmapped_cancel_like_status_rows"):
        errors.append(f"NHANH unmapped cancel-like statuses remain: {naud.get('unmapped_cancel_like_status_rows')}")
    for row in naud.get("status_rows") or []:
        raw = row.get("raw_status")
        c = canon(raw)
        classified_cancel = bool(row.get("classified_cancelled"))
        classified_return = bool(row.get("classified_return_status"))
        classified_failed = bool(row.get("classified_failed_status"))
        if c in cancel_keys and not classified_cancel:
            errors.append(f"Nhanh normalized cancel status {raw!r} is not cancelled")
        if c in return_keys and (classified_cancel or not classified_return):
            errors.append(f"Nhanh normalized return status {raw!r} classification wrong")
        if c in failed_keys and (classified_cancel or not classified_failed):
            errors.append(f"Nhanh normalized failed status {raw!r} classification wrong")
        if cancel_like(raw) and c not in cancel_keys and not classified_cancel:
            errors.append(f"Nhanh genuine cancel-like token is unmapped: {raw!r}")
        if canon(raw) == canon("Đang chuyển") and cancel_like(raw):
            errors.append("Đang chuyển is incorrectly treated as cancel-like")

    # Audit must be scoped to actual reporting periods rather than the entire
    # continuous previous-month-to-report-date interval.
    periods = dq.get("audit_periods") or []
    labels = {str(x.get("label")) for x in periods if isinstance(x, dict)}
    for label in ("current_mtd", "previous_mtd_same_period", "previous_wtd_same_days", "comparison_date"):
        if label not in labels:
            errors.append(f"data_quality.audit_periods missing {label}")

    # Full Nhanh financial harmonization: WTD/MTD growth scorecards must be
    # exact sums of the authoritative direct daily Nhanh P&L, not merely
    # Gross/order/cancel overrides. This guards Backoffice/Profit after a
    # normalized status changes classification.
    detail = p.get("mtd_daily_detail_by_channel") or {}
    nrows = detail.get("NHANH") or [] if isinstance(detail, dict) else []
    report_date_text = str(meta.get("report_date") or "")
    try:
        import datetime as _dt
        report_date = _dt.date.fromisoformat(report_date_text)
        week_start = report_date - _dt.timedelta(days=report_date.weekday())
    except Exception:
        report_date = None
        week_start = None
    wrows = []
    if week_start is not None:
        for r in nrows:
            try:
                if _dt.date.fromisoformat(str(r.get("report_date"))) >= week_start:
                    wrows.append(r)
            except Exception:
                pass

    def _sum(rows, field):
        return sum(float(r.get(field) or 0) for r in rows)

    def _close(a, b, tol=1.0):
        return abs(float(a or 0) - float(b or 0)) <= tol

    weekly = next((r for r in (p.get("weekly_scorecard_by_channel") or []) if str(r.get("channel")).upper() == "NHANH"), {})
    monthly = next((r for r in (p.get("monthly_scorecard_by_channel") or []) if str(r.get("channel")).upper() == "NHANH"), {})
    metric_map = {
        "gross_sales": "gross_sales",
        "net_sales": "net_sales",
        "orders": "orders",
        "success_orders": "success_orders",
        "cancelled_orders": "cancelled_orders",
        "cogs": "cogs_total",
        "gross_margin": "gm1",
        "shipping_return_fee": "logistics_order_cost",
        "backoffice_fee": "backoffice_cost",
        "ads_cost": "ads_total",
        "live_cost": "livestream_inhouse",
        "booking_fee": "booking_kol_koc",
        "packaging_cost": "packaging_cost",
        "profit": "profit",
    }
    for suffix, rows, score in (("wtd", wrows, weekly), ("mtd", nrows, monthly)):
        for growth_metric, daily_field in metric_map.items():
            expected = _sum(rows, daily_field)
            actual = score.get(f"{growth_metric}_{suffix}")
            if not _close(expected, actual):
                errors.append(
                    f"NHANH full financial harmonization mismatch {growth_metric}_{suffix}: "
                    f"daily={expected:.2f}, growth={float(actual or 0):.2f}"
                )
        # Derived ratios.
        net = _sum(rows, "net_sales")
        orders = _sum(rows, "orders")
        expected_ratios = {
            f"gm_pct_{suffix}": (_sum(rows, "gm1") / net if net else 0.0),
            f"profit_margin_pct_{suffix}": (_sum(rows, "profit") / net if net else 0.0),
            f"ads_sales_pct_{suffix}": (_sum(rows, "ads_total") / net if net else 0.0),
            f"cancellation_rate_{suffix}": (_sum(rows, "cancelled_orders") / orders if orders else 0.0),
        }
        for key, expected in expected_ratios.items():
            if key in score and abs(float(score.get(key) or 0) - expected) > 1e-9:
                errors.append(f"NHANH derived growth ratio mismatch {key}")

    # TOTAL summaries must equal channel scorecard sums for the same financial
    # metrics after Nhanh is rewritten.
    for prefix, suffix in (("weekly", "wtd"), ("monthly", "mtd")):
        rows = p.get(f"{prefix}_scorecard_by_channel") or []
        summary = p.get(f"{prefix}_summary") or {}
        for metric in (
            "gross_sales", "net_sales", "orders", "success_orders", "cancelled_orders",
            "cogs", "gross_margin", "platform_fees", "shipping_return_fee",
            "backoffice_fee", "ads_cost", "live_cost", "booking_fee",
            "packaging_cost", "profit",
        ):
            key = f"{metric}_{suffix}"
            expected = sum(float(r.get(key) or 0) for r in rows if str(r.get("channel")).upper() != "TOTAL")
            if key in summary and not _close(expected, summary.get(key)):
                errors.append(f"{prefix}_summary {key} does not equal channel scorecard sum")

    # Zero-filled/no-sales trend and chart points must be numeric 0, never null.
    trend_fields = (
        "current_gross_sales", "previous_gross_sales", "current_net_sales", "previous_net_sales",
        "current_profit", "previous_profit", "current_orders", "previous_orders", "net_sales_delta",
    )
    for block in ("weekly_channel_sales_trend_current_vs_previous", "monthly_channel_sales_trend_current_vs_previous"):
        rows = p.get(block) or []
        for i, row in enumerate(rows):
            for field in trend_fields:
                if row.get(field) is None:
                    errors.append(f"{block}[{i}].{field} is null; expected numeric zero/value")
    chart = p.get("chart_data") or {}
    if isinstance(chart, dict):
        for block in ("weekly_channel_net_sales_current_vs_previous", "monthly_channel_net_sales_current_vs_previous"):
            rows = chart.get(block) or []
            for i, row in enumerate(rows):
                for field in ("current_net_sales", "previous_net_sales", "net_sales_delta"):
                    if row.get(field) is None:
                        errors.append(f"chart_data.{block}[{i}].{field} is null")

    # Rate KPIs on explicit zero-filled parent daily rows must be 0, not null.
    detail = p.get("mtd_daily_detail_by_channel") or {}
    for ch, rows in detail.items() if isinstance(detail, dict) else []:
        for i, row in enumerate(rows or []):
            if bool(row.get("zero_filled")):
                for field in ("cancellation_rate", "gm1_margin", "cm1_margin", "cm2_margin", "profit_margin"):
                    if row.get(field) is None:
                        errors.append(f"mtd_daily_detail_by_channel.{ch}[{i}].{field} null on zero-filled row")

    render = p.get("report_rendering_policy") or {}
    tiktok_cogs = str((render.get("cogs_split_sources") or {}).get("TIKTOK") or "")
    if "Canceled" not in tiktok_cogs or "Đã hủy" in tiktok_cogs:
        errors.append(f"stale TikTok COGS metadata: {tiktok_cogs}")
    if "v4.5" in str(render.get("growth_override_rule") or ""):
        errors.append("report_rendering_policy.growth_override_rule still references v4.5")

    rules = p.get("pnl_rules") or {}
    for key in ("growth_harmonization", "cogs_split", "returns_refunds"):
        value = str(rules.get(key) or "")
        if "v4.5" in value:
            errors.append(f"pnl_rules.{key} still references v4.5")
    if "Canceled" not in str(rules.get("cogs_split") or ""):
        errors.append("pnl_rules.cogs_split does not state TikTok Canceled")

    display = p.get("display_semantics") or {}
    if display.get("success_orders") != "non_cancelled_compatibility":
        errors.append("display_semantics.success_orders must be non_cancelled_compatibility")
    nhanh_display = display.get("NHANH") or {}
    if nhanh_display.get("successful_orders_exact_field") != "successful_orders_exact":
        errors.append("display_semantics.NHANH.successful_orders_exact_field missing")
    order_policy = render.get("order_status_display_policy") or {}
    if "Đơn không hủy" not in str(order_policy.get("success_orders") or ""):
        errors.append("success_orders rendering label must explicitly be Đơn không hủy")

    return list(dict.fromkeys(errors))


def self_test() -> int:
    if canon(" HỆ   THỐNG HỦY ") != canon("Hệ Thống hủy"):
        print("SELF-TEST FAILED: normalization", file=sys.stderr)
        return 1
    if cancel_like("Đang chuyển"):
        print("SELF-TEST FAILED: Đang chuyển false positive", file=sys.stderr)
        return 1
    if not cancel_like("Hệ thống hủy"):
        print("SELF-TEST FAILED: cancel token", file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
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
