#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict validator for CEO Daily P&L package v4.7.4."""
from __future__ import annotations

import argparse
import copy
import csv
import importlib.util
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

PACKAGE_VERSION = "v4.7.4"
PACKAGE_VERSION_INT = 474
BASE_FILENAME = "validate_ceo_daily_pnl_package_v4_7_3.py"
CHANNELS: Tuple[str, ...] = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
MONEY_TOL = 1.0
QTY_TOL = 1e-6


def _load_base():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7.4 validator requires v4.7.3 validator")
    spec = importlib.util.spec_from_file_location("hh_validate_v473_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        out = float(value)
        return default if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return default


def close(a: Any, b: Any, tol: float = QTY_TOL) -> bool:
    return abs(f(a) - f(b)) <= tol


def _validate_alias_file(path: Path, errors: List[str]) -> None:
    if not path.exists():
        errors.append(f"alias master not found: {path}")
        return
    short_to_ean: Dict[str, str] = {}
    ean_to_short: Dict[str, str] = {}
    approved = 0
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        for row in rd:
            if str(row.get("status") or "").strip().upper() != "APPROVED":
                continue
            ean = str(row.get("canonical_ean") or "").strip()
            short = str(row.get("short_sku") or "").strip()
            approved += 1
            if not re.fullmatch(r"\d{8,14}", ean):
                errors.append(f"invalid approved EAN: {ean}")
            sn = short.casefold(); en = ean.casefold()
            if sn in short_to_ean and short_to_ean[sn] != ean:
                errors.append(f"alias conflict: {short} -> {short_to_ean[sn]} and {ean}")
            if en in ean_to_short and ean_to_short[en].casefold() != sn:
                errors.append(f"EAN conflict: {ean} -> {ean_to_short[en]} and {short}")
            short_to_ean[sn] = ean; ean_to_short[en] = short
    if approved <= 0:
        errors.append("alias master has no approved rows")


def validate_package(p: Mapping[str, Any], alias_file: Path | None = None, skip_base: bool = False) -> List[str]:
    errors: List[str] = []
    meta = p.get("metadata") or {}
    if meta.get("package_version") != PACKAGE_VERSION:
        errors.append(f"package_version={meta.get('package_version')}, expected {PACKAGE_VERSION}")
    if meta.get("package_version_int") != PACKAGE_VERSION_INT:
        errors.append(f"package_version_int={meta.get('package_version_int')}, expected {PACKAGE_VERSION_INT}")

    if not skip_base:
        base = _load_base()
        p473 = copy.deepcopy(dict(p))
        p473.setdefault("metadata", {})["package_version"] = "v4.7.3"
        p473["metadata"]["package_version_int"] = 473
        for e in base.validate_package(p473):
            errors.append(f"v4.7.3-base: {e}")

    alias_block = p.get("sku_alias_master_v4_7_4") or {}
    if alias_block.get("available") is not True:
        errors.append("sku_alias_master_v4_7_4.available must be true")
    if f(alias_block.get("approved_mapping_count")) <= 0:
        errors.append("sku_alias_master_v4_7_4.approved_mapping_count must be > 0")
    if alias_file is not None:
        _validate_alias_file(alias_file, errors)

    top = p.get("top_20_sku_by_channel_v4_7_4") or {}
    top_audit = p.get("top20_mapping_audit_v4_7_4") or {}
    valid_status = {"MAPPED_SINGLE", "MAPPED_IDENTITY_NO_STOCK", "MAPPED_BUNDLE_COMPONENTS"}
    for ch in CHANNELS:
        rows = top.get(ch) if isinstance(top, Mapping) else None
        if not isinstance(rows, Sequence):
            errors.append(f"top_20_sku_by_channel_v4_7_4 missing {ch}")
            continue
        for idx, row in enumerate(rows, 1):
            if int(f(row.get("rank_by_sold_qty"))) != idx:
                errors.append(f"top20 {ch} rank mismatch at row {idx}")
            status = str(row.get("mapping_resolution_status") or "")
            if status not in valid_status:
                errors.append(f"top20 {ch} rank {idx} unresolved: {status}")
            if status == "MAPPED_SINGLE" and not row.get("inventory_key"):
                errors.append(f"top20 {ch} rank {idx} MAPPED_SINGLE without inventory_key")
            if status == "MAPPED_IDENTITY_NO_STOCK":
                if not row.get("primary_ean"):
                    errors.append(f"top20 {ch} rank {idx} identity-only row missing EAN")
                if row.get("current_stock_qty") is not None:
                    errors.append(f"top20 {ch} rank {idx} identity-only stock must be null")
            if status == "MAPPED_BUNDLE_COMPONENTS":
                comps = row.get("bundle_components") or []
                if not comps:
                    errors.append(f"top20 {ch} rank {idx} bundle has no components")
                for c in comps:
                    if str(c.get("mapping_status")) not in {"MAPPED", "IDENTITY_ONLY"}:
                        errors.append(f"top20 {ch} rank {idx} unresolved bundle component {c.get('component_sku')}")
        cov = ((top_audit.get("mapping_coverage_by_channel") or {}).get(ch))
        if rows and not close(cov, 1.0, 1e-9):
            errors.append(f"top20 {ch} mapping coverage must be 100%, got {cov}")

    push_by_ch = p.get("inventory_push_by_channel") or {}
    seen: set[Tuple[str, str]] = set()
    channel_totals: Dict[str, Dict[str, float]] = {}
    for ch in CHANNELS:
        rows = push_by_ch.get(ch) if isinstance(push_by_ch, Mapping) else None
        if not isinstance(rows, Sequence):
            errors.append(f"inventory_push_by_channel missing {ch}")
            continue
        totals = {"sold": 0.0, "gift": 0.0, "bundle": 0.0, "push": 0.0}
        for row in rows:
            key = str(row.get("inventory_key") or "")
            if not key:
                errors.append(f"inventory_push_by_channel.{ch} row missing identity key")
                continue
            if (ch, key) in seen:
                errors.append(f"duplicate inventory push row {ch}/{key}")
            seen.add((ch, key))
            sold = f(row.get("sold_qty_mtd")); gift = f(row.get("gift_qty_mtd")); bundle = f(row.get("bundle_unallocated_push_qty_mtd")); push = f(row.get("total_push_qty_mtd"))
            if not close(push, sold + gift + bundle):
                errors.append(f"inventory push equation mismatch {ch}/{key}: {push} != {sold}+{gift}+{bundle}")
            totals["sold"] += sold; totals["gift"] += gift; totals["bundle"] += bundle; totals["push"] += push
        channel_totals[ch] = totals

    company = p.get("company_inventory_push_by_product") or []
    company_by_key = {str(r.get("inventory_key")): r for r in company if r.get("inventory_key")}
    if len(company_by_key) != len(company):
        errors.append("company_inventory_push_by_product has duplicate/missing inventory_key")
    for key, row in company_by_key.items():
        sold = sum(f(r.get("sold_qty_mtd")) for ch in CHANNELS for r in (push_by_ch.get(ch) or []) if str(r.get("inventory_key")) == key)
        gift = sum(f(r.get("gift_qty_mtd")) for ch in CHANNELS for r in (push_by_ch.get(ch) or []) if str(r.get("inventory_key")) == key)
        bundle = sum(f(r.get("bundle_unallocated_push_qty_mtd")) for ch in CHANNELS for r in (push_by_ch.get(ch) or []) if str(r.get("inventory_key")) == key)
        push = sum(f(r.get("total_push_qty_mtd")) for ch in CHANNELS for r in (push_by_ch.get(ch) or []) if str(r.get("inventory_key")) == key)
        if not close(row.get("sold_qty_mtd"), sold) or not close(row.get("gift_qty_mtd"), gift) or not close(row.get("bundle_unallocated_push_qty_mtd"), bundle) or not close(row.get("total_push_qty_mtd"), push):
            errors.append(f"company inventory push reconciliation mismatch {key}")

    gift_audit = p.get("shopee_gift_classification_audit_v4_7_4") or {}
    if not close(gift_audit.get("total_cogs_mtd"), f(gift_audit.get("sold_cogs_mtd")) + f(gift_audit.get("gift_cogs_mtd")), MONEY_TOL):
        errors.append("Shopee gift audit COGS split does not reconcile")
    shopee_mtd = (((p.get("mtd_pnl_comparison_by_channel") or {}).get("SHOPEE") or {}).get("current_mtd") or {})
    if shopee_mtd and not close(gift_audit.get("total_cogs_mtd"), shopee_mtd.get("cogs_total"), MONEY_TOL):
        errors.append("Shopee gift audit total COGS != canonical current MTD COGS")
    gift_rows = ((p.get("gift_sku_mtd_by_channel") or {}).get("SHOPEE") or [])
    gift_cogs_sum = sum(f(r.get("gift_cogs_mtd")) for r in gift_rows)
    gift_qty_sum = sum(f(r.get("gift_qty_mtd")) for r in gift_rows)
    if not close(gift_cogs_sum, gift_audit.get("gift_cogs_mtd"), MONEY_TOL):
        errors.append("Shopee gift SKU COGS != gift audit")
    if not close(gift_qty_sum, gift_audit.get("gift_qty_mtd"), QTY_TOL):
        errors.append("Shopee gift SKU qty != gift audit")

    contract = p.get("excel_render_contract") or {}
    for key, expected in {
        "top20_sku_source_v4_7_4": "top_20_sku_by_channel_v4_7_4",
        "gift_sku_source": "gift_sku_mtd_by_channel",
        "inventory_push_source": "inventory_push_by_channel",
        "company_inventory_push_source": "company_inventory_push_by_product",
    }.items():
        if contract.get(key) != expected:
            errors.append(f"excel_render_contract.{key} changed")
    return list(dict.fromkeys(errors))


def self_test() -> int:
    fixture = {
        "metadata": {"package_version": "v4.7.4", "package_version_int": 474},
        "sku_alias_master_v4_7_4": {"available": True, "approved_mapping_count": 1},
        "top_20_sku_by_channel_v4_7_4": {ch: [] for ch in CHANNELS},
        "top20_mapping_audit_v4_7_4": {"mapping_coverage_by_channel": {ch: None for ch in CHANNELS}},
        "inventory_push_by_channel": {ch: [] for ch in CHANNELS},
        "company_inventory_push_by_product": [],
        "shopee_gift_classification_audit_v4_7_4": {"sold_cogs_mtd": 90, "gift_cogs_mtd": 10, "total_cogs_mtd": 100, "gift_qty_mtd": 1},
        "gift_sku_mtd_by_channel": {**{ch: [] for ch in CHANNELS}, "SHOPEE": [{"gift_cogs_mtd": 10, "gift_qty_mtd": 1}]},
        "excel_render_contract": {
            "top20_sku_source_v4_7_4": "top_20_sku_by_channel_v4_7_4",
            "gift_sku_source": "gift_sku_mtd_by_channel",
            "inventory_push_source": "inventory_push_by_channel",
            "company_inventory_push_source": "company_inventory_push_by_product",
        },
    }
    errors = validate_package(fixture, skip_base=True)
    if errors:
        print("SELF-TEST FAILED", file=sys.stderr)
        for e in errors: print("-", e, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Push equation guard: PASS")
    print("Gift COGS reconciliation guard: PASS")
    print("Top20 mapping contract: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("package", nargs="?")
    ap.add_argument("--sku-map", default=None)
    ap.add_argument("--skip-base", action="store_true", help="Development only; do not use for production validation")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.package:
        ap.error("package JSON path is required unless --self-test")
    path = Path(args.package)
    package = json.loads(path.read_text(encoding="utf-8"))
    alias = Path(args.sku_map) if args.sku_map else Path(__file__).resolve().parents[1] / "config/product_sku_alias_master_v4_7_4.csv"
    errors = validate_package(package, alias_file=alias, skip_base=args.skip_base)
    if errors:
        print("VALIDATION FAILED")
        for e in errors: print("-", e)
        return 1
    print("VALIDATION PASSED")
    print(f"Package: {path}")
    print("v4.7.4 alias mapping / Top20 / gift / inventory-push contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
