#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict production validator for CEO Daily P&L package v4.5.

Exit 0 only when the package satisfies:
- exact normalized "Đã hủy" cancellation only;
- no return/refund order classification/output;
- no true platform-funded voucher output/deduction;
- Shopee seller-funded Shop voucher is exposed as seller_voucher only;
- Gross all statuses and exact-cancel/non-cancel buckets reconcile;
- sold COGS + gift COGS = total COGS for NHANH/SHOPEE/TIKTOK;
- Net Sales -> GM1 -> CM1 -> CM2 -> Profit equations reconcile;
- Affiliate/platform/ads/profit reconciliations are within 1 VND;
- inherited weekly/monthly marketplace Gross/order/cancel/Affiliate agree with
  the same v4.5 daily source.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

TOL = 1.0
EXACT_CANCEL = "đã hủy"
FORBIDDEN_DAILY_KEYS = {"gross_returned_sales", "returned_orders", "return_rate", "platform_voucher", "platform_voucher_source"}

NHANH_LEAF_KEYS = [
    "FB_LPM_VIETNAM",
    "FB_LPM_VIETNAM_168",
    "FB_BLEDINA_BRASSES_VN",
    "LANDING_PAGE",
    "WEBSITE_LPM",
    "ZALO_OA_LPM",
    "INSTAGRAM",
]
NHANH_INTERNAL_UNALLOCATED_KEY = "_UNALLOCATED"



def f(x: Any) -> float:
    try:
        v = float(x or 0)
    except Exception:
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def close(a: Any, b: Any, tol: float = TOL) -> bool:
    return abs(f(a) - f(b)) <= tol


def daterange(a: dt.date, b: dt.date):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def recurse_has_key(obj: Any, needle: str) -> bool:
    if isinstance(obj, dict):
        return any(str(k).casefold() == needle.casefold() or recurse_has_key(v, needle) for k, v in obj.items())
    if isinstance(obj, list):
        return any(recurse_has_key(v, needle) for v in obj)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    args = ap.parse_args()
    p = json.loads(Path(args.package).read_text(encoding="utf-8"))
    errors: list[str] = []

    meta = p.get("metadata", {})
    dq = p.get("data_quality", {})
    if meta.get("package_version") != "v4.5":
        errors.append(f"package_version={meta.get('package_version')}, expected v4.5")
    if meta.get("package_version_int") != 45:
        errors.append(f"package_version_int={meta.get('package_version_int')}, expected 45")
    if not dq.get("can_send"):
        errors.append("data_quality.can_send is not true")
    if dq.get("errors"):
        errors.append("data_quality.errors is not empty")

    rule = dq.get("marketplace_status_rule", {})
    if rule.get("cancelled_status_exact") != EXACT_CANCEL:
        errors.append('exact cancelled status rule is not "đã hủy"')
    if rule.get("returns_refunds") != "DISABLED_TEMPORARILY":
        errors.append("return/refund classification is not disabled")
    for ch in ("SHOPEE", "TIKTOK"):
        aud = (p.get("marketplace_status_audit") or {}).get(ch, {})
        if f(aud.get("flag_mismatch_count")) != 0:
            errors.append(f'{ch} DB is_cancelled flag mismatch count = {aud.get("flag_mismatch_count")}')

    if recurse_has_key(p, "ebitda"):
        errors.append("package contains an EBITDA key")

    report_date = dt.date.fromisoformat(meta["report_date"])
    mstart = report_date.replace(day=1)
    week_start = report_date - dt.timedelta(days=report_date.weekday())
    detail = p.get("mtd_daily_detail_by_channel", {})

    for ch in ("SHOPEE", "TIKTOK"):
        rows = detail.get(ch, [])
        by_date = {r.get("report_date"): r for r in rows}
        expected_dates = [d.isoformat() for d in daterange(mstart, report_date)]
        if sorted(by_date) != expected_dates:
            errors.append(f"{ch} MTD dates are not complete/unique from {mstart} to {report_date}")

        for day in expected_dates:
            r = by_date.get(day, {})
            forbidden_present = sorted(k for k in FORBIDDEN_DAILY_KEYS if k in r)
            if forbidden_present:
                errors.append(f"{ch} {day}: forbidden return/platform-voucher keys present: {forbidden_present}")

            if not close(r.get("gross_sales"), f(r.get("gross_non_cancelled_sales")) + f(r.get("gross_cancelled_sales"))):
                errors.append(f"{ch} {day}: Gross != non-cancel Gross + exact-cancel Gross")
            if not close(r.get("orders"), f(r.get("success_orders")) + f(r.get("cancelled_orders"))):
                errors.append(f"{ch} {day}: orders != non-cancel orders + cancelled orders")
            if not close(f(r.get("gross_sales")) - f(r.get("sales_deductions_total")), r.get("net_sales")):
                errors.append(f"{ch} {day}: Gross - deductions != Net Sales")
            if not close(
                r.get("sales_deductions_total"),
                f(r.get("gross_cancelled_sales")) + f(r.get("seller_discount")) + f(r.get("seller_voucher")) + f(r.get("other_sales_adjustment")),
            ):
                errors.append(f"{ch} {day}: revenue-bridge components != total deductions")
            if ch == "TIKTOK" and abs(f(r.get("seller_voucher"))) > TOL:
                errors.append(f"TIKTOK {day}: seller_voucher should be 0/not applicable")
            if not close(f(r.get("net_sales")) - f(r.get("cogs_total")), r.get("gm1")):
                errors.append(f"{ch} {day}: Net Sales - COGS != GM1")
            if not close(f(r.get("gm1")) - f(r.get("cm1_cost_total")), r.get("cm1")):
                errors.append(f"{ch} {day}: GM1 - CM1 cost != CM1")
            if not close(f(r.get("cm1")) - f(r.get("cm2_cost_total")), r.get("cm2")):
                errors.append(f"{ch} {day}: CM1 - CM2 cost != CM2")
            if not close(f(r.get("cm2")) - f(r.get("backoffice_cost")), r.get("profit")):
                errors.append(f"{ch} {day}: CM2 - backoffice != Profit")

            for k in (
                "gross_status_reconciliation_gap",
                "sales_deductions_reconciliation_gap",
                "platform_reconciliation_gap",
                "ads_reconciliation_gap",
                "affiliate_reconciliation_gap",
                "profit_vs_legacy_profit",
            ):
                if abs(f(r.get(k))) > TOL:
                    errors.append(f"{ch} {day}: {k}={f(r.get(k)):.2f} > {TOL}")

            if ch == "TIKTOK" and not close(
                r.get("affiliate_total"),
                f(r.get("affiliate_creator")) + f(r.get("affiliate_partner")) + f(r.get("affiliate_other")),
            ):
                errors.append(f"TIKTOK {day}: affiliate split != affiliate_total")

    # v4.5 strict sold/gift COGS split for all e-commerce channels.
    for ch in ("NHANH", "SHOPEE", "TIKTOK"):
        rows = detail.get(ch, [])
        by_date = {r.get("report_date"): r for r in rows}
        expected_dates = [d.isoformat() for d in daterange(mstart, report_date)]
        if sorted(by_date) != expected_dates:
            errors.append(f"{ch} COGS split validation cannot run: MTD dates incomplete/duplicate")
            continue
        for day in expected_dates:
            r = by_date.get(day, {})
            if abs(f(r.get("cogs_total"))) > TOL and not bool(r.get("cogs_split_available")):
                errors.append(f"{ch} {day}: cogs_split_available is false while cogs_total is non-zero")
            if not close(r.get("cogs_total"), f(r.get("sold_cogs")) + f(r.get("gift_cogs"))):
                errors.append(f"{ch} {day}: sold_cogs + gift_cogs != cogs_total")
            if abs(f(r.get("cogs_split_reconciliation_gap"))) > TOL:
                errors.append(f"{ch} {day}: cogs_split_reconciliation_gap={f(r.get('cogs_split_reconciliation_gap')):.2f} > {TOL}")

    render = p.get("report_rendering_policy", {}).get("daily_detail_tables", {})
    for sheet in ("04_Shopee", "05_TikTok"):
        cols = (render.get(sheet) or {}).get("columns", [])
        bad = [c for c in cols if c in FORBIDDEN_DAILY_KEYS or "return_rate" in str(c) or "gross_returned" in str(c) or "returned_orders" in str(c)]
        if bad:
            errors.append(f"{sheet} rendering exposes forbidden return/platform-voucher fields: {bad}")
    shopee_cols = (render.get("04_Shopee") or {}).get("columns", [])
    if "seller_voucher" not in shopee_cols:
        errors.append("04_Shopee rendering is missing seller_voucher (Shop-funded voucher)")
    for sheet in ("03_Nhanh", "04_Shopee", "05_TikTok"):
        cols = (render.get(sheet) or {}).get("columns", [])
        if "sold_cogs" not in cols or "gift_cogs" not in cols:
            errors.append(f"{sheet} rendering must expose sold_cogs and gift_cogs")
        if any(x in cols for x in ("main_cogs", "gift_promo_cogs", "cogs_other")):
            errors.append(f"{sheet} rendering contains deprecated/ambiguous COGS fields")

    weekly_sc = {r.get("channel"): r for r in p.get("weekly_scorecard_by_channel", [])}
    monthly_sc = {r.get("channel"): r for r in p.get("monthly_scorecard_by_channel", [])}
    aff = {r.get("channel"): r for r in p.get("affiliate_cost_by_channel", [])}
    for ch in ("SHOPEE", "TIKTOK"):
        rows = detail.get(ch, [])
        wrows = [r for r in rows if dt.date.fromisoformat(r["report_date"]) >= week_start]
        checks = [
            ("gross_sales_wtd", sum(f(r.get("gross_sales")) for r in wrows), weekly_sc.get(ch, {}).get("gross_sales_wtd")),
            ("orders_wtd", sum(f(r.get("orders")) for r in wrows), weekly_sc.get(ch, {}).get("orders_wtd")),
            ("cancelled_orders_wtd", sum(f(r.get("cancelled_orders")) for r in wrows), weekly_sc.get(ch, {}).get("cancelled_orders_wtd")),
            ("gross_sales_mtd", sum(f(r.get("gross_sales")) for r in rows), monthly_sc.get(ch, {}).get("gross_sales_mtd")),
            ("orders_mtd", sum(f(r.get("orders")) for r in rows), monthly_sc.get(ch, {}).get("orders_mtd")),
            ("cancelled_orders_mtd", sum(f(r.get("cancelled_orders")) for r in rows), monthly_sc.get(ch, {}).get("cancelled_orders_mtd")),
            ("affiliate_cost_wtd", sum(f(r.get("affiliate_total")) for r in wrows), aff.get(ch, {}).get("affiliate_cost_wtd")),
            ("affiliate_cost_mtd", sum(f(r.get("affiliate_total")) for r in rows), aff.get(ch, {}).get("affiliate_cost_mtd")),
        ]
        for name, expected, actual in checks:
            if not close(expected, actual):
                errors.append(f"{ch}: {name} growth={f(actual):.2f}, daily={expected:.2f}")

    # Nhanh leaf-channel contract.
    catalog = p.get("nhanh_leaf_channel_catalog", [])
    catalog_keys = [r.get("key") for r in catalog if isinstance(r, dict)]
    if catalog_keys != NHANH_LEAF_KEYS:
        errors.append(f"Nhanh leaf catalog mismatch/order: {catalog_keys}")

    leaf_map = p.get("nhanh_leaf_channel_map", {})
    leaf_daily = p.get("nhanh_leaf_daily_detail", {})
    visible_leaf = p.get("nhanh_leaf_visible_channels_mtd", [])
    hidden_leaf = p.get("nhanh_leaf_hidden_zero_sales_channels", [])
    if sorted(set(visible_leaf) | set(hidden_leaf)) != sorted(NHANH_LEAF_KEYS):
        errors.append("Nhanh visible+hidden leaf lists do not cover exactly the seven leaf channels")
    if set(visible_leaf) & set(hidden_leaf):
        errors.append("Nhanh leaf channel appears in both visible and hidden lists")

    expected_dates = [d.isoformat() for d in daterange(mstart, report_date)]
    for key in NHANH_LEAF_KEYS:
        if key not in leaf_map:
            errors.append(f"Nhanh leaf map missing {key}")
            continue
        rows = leaf_daily.get(key, [])
        by_date = {r.get("report_date"): r for r in rows}
        if sorted(by_date) != expected_dates:
            errors.append(f"Nhanh leaf {key}: MTD dates are not complete/unique")
        mtd = leaf_map[key].get("mtd", {})
        has_sales_mtd = abs(f(mtd.get("gross_sales"))) > TOL or abs(f(mtd.get("net_sales"))) > TOL
        show = bool(leaf_map[key].get("show_in_report"))
        if show != has_sales_mtd:
            errors.append(f"Nhanh leaf {key}: show_in_report={show} but has_sales_mtd={has_sales_mtd}")
        if show and key not in visible_leaf:
            errors.append(f"Nhanh leaf {key}: show_in_report=true but absent from visible list")
        if (not show) and key not in hidden_leaf:
            errors.append(f"Nhanh leaf {key}: show_in_report=false but absent from hidden list")
        # Zero-fill must be numeric, never null, for core daily metrics.
        for day in expected_dates:
            r = by_date.get(day, {})
            for field in ("gross_sales","net_sales","orders","sold_cogs","gift_cogs","cogs_total","gm1","cm1","cm2","profit"):
                if r.get(field) is None:
                    errors.append(f"Nhanh leaf {key} {day}: {field} is null; zero-fill contract broken")
            if not close(r.get("cogs_total"), f(r.get("sold_cogs")) + f(r.get("gift_cogs"))):
                errors.append(f"Nhanh leaf {key} {day}: sold_cogs + gift_cogs != cogs_total")
            if abs(f(r.get("cogs_split_reconciliation_gap"))) > TOL:
                errors.append(f"Nhanh leaf {key} {day}: cogs split gap > {TOL}")

    internal = p.get("nhanh_leaf_internal_cost_bucket", {})
    if internal.get("key") != NHANH_INTERNAL_UNALLOCATED_KEY:
        errors.append("Nhanh internal unallocated bucket missing")
    if internal.get("show_in_report") is not False:
        errors.append("Nhanh internal unallocated bucket must never render as a sales channel")

    # Reconciliation produced by builder must be fully PASS.
    rec_rows = p.get("nhanh_leaf_reconciliation_daily", [])
    rec_by_date = {r.get("report_date"): r for r in rec_rows}
    if sorted(rec_by_date) != expected_dates:
        errors.append("Nhanh leaf reconciliation does not cover every MTD date")
    for day in expected_dates:
        rr = rec_by_date.get(day, {})
        if rr.get("status") != "PASS" or abs(f(rr.get("max_abs_gap"))) > TOL:
            errors.append(f"Nhanh leaf reconciliation failed on {day}: max_abs_gap={f(rr.get('max_abs_gap')):.2f}")

    # Parent visibility: zero-sales channels are excluded from the AI analysis list.
    visibility = p.get("channel_visibility", {})
    ai_channels = p.get("ai_analysis_channels", [])
    hidden_parent = set((p.get("data_quality") or {}).get("hidden_zero_sales_parent_channels", []))
    for ch in ("GT","MT","NHANH","SHOPEE","TIKTOK"):
        v = visibility.get(ch, {})
        should_show = abs(f(v.get("gross_sales_mtd"))) > TOL or abs(f(v.get("net_sales_mtd"))) > TOL
        if bool(v.get("show_in_report")) != should_show:
            errors.append(f"{ch}: parent show_in_report does not match MTD revenue visibility rule")
        if should_show and ch not in ai_channels:
            errors.append(f"{ch}: has MTD sales but is absent from ai_analysis_channels")
        if (not should_show) and ch in ai_channels:
            errors.append(f"{ch}: zero MTD sales but appears in ai_analysis_channels")
        if (not should_show) and ch not in hidden_parent:
            errors.append(f"{ch}: zero MTD sales but absent from hidden_zero_sales_parent_channels")

    render_policy = p.get("report_rendering_policy", {})
    visibility_policy = render_policy.get("visibility_policy", {})
    if visibility_policy.get("hide_zero_sales_channels") is not True:
        errors.append("report_rendering_policy.visibility_policy.hide_zero_sales_channels must be true")
    leaf_rule = ((render_policy.get("daily_detail_tables") or {}).get("03_Nhanh") or {})
    if leaf_rule.get("leaf_channel_source") != "nhanh_leaf_channel_map":
        errors.append("03_Nhanh rendering policy does not point to nhanh_leaf_channel_map")
    if leaf_rule.get("leaf_visibility_field") != "show_in_report":
        errors.append("03_Nhanh rendering policy leaf_visibility_field must be show_in_report")

    if errors:
        print("VALIDATION FAILED")
        for e in errors:
            print("-", e)
        return 1

    print("VALIDATION PASSED")
    print(f"- Package: {args.package}")
    print(f"- Report date: {report_date}")
    print(f'- Cancellation: exact normalized status "{EXACT_CANCEL}" only')
    print("- Returns/refunds: no classification/output; all other statuses normal")
    print("- Platform-funded voucher: ignored; Shopee seller-funded Shop voucher is seller_voucher")
    print("- Shopee/TikTok Gross, P&L, Affiliate and growth blocks reconcile")
    print("- NHANH/SHOPEE/TIKTOK COGS split: sold_cogs + gift_cogs = cogs_total")
    print("- Nhanh: 7 exact-label leaf channels, zero-filled daily rows, zero-sales leaves hidden from AI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
