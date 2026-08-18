#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEO Daily P&L package v4.7.1 — Top20 SKU + inventory sales mapping.

Layered on v4.7. Financial P&L/target/status rules are not changed.

Adds:
- top_20_sku_by_channel: exactly the top 20 *sold* SKUs per channel by MTD
  sold_qty (gifts do not affect rank). Inventory is attached only to these
  report-facing top rows.
- inventory_sheet_sku_summary: one row per inventory SKU/product, summing stock
  across all lots/expiry dates, with mapped MTD/day sold quantities from NHANH,
  SHOPEE and TIKTOK.
- inventory_sales_mapping_audit: explicit mapped/unmapped quantities so no stock
  or sales is invented for unresolved/composite identifiers.

Important:
- current_stock_qty is the actual resolved inventory snapshot total across lots.
- sold_qty_mtd_to_current_stock_pct means MTD sold quantity / current stock only.
  It is NOT sell-through from opening inventory and is never labelled as such.
- Near-date pushed quantity is still unavailable without order-to-lot attribution.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import importlib.util
import json
import math
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

PACKAGE_VERSION = "v4.7.1"
PACKAGE_VERSION_INT = 471
BASE_FILENAME = "build_ceo_daily_pnl_package_v4_7.py"
CHANNELS: Tuple[str, ...] = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
INVENTORY_SALES_CHANNELS: Tuple[str, ...] = ("NHANH", "SHOPEE", "TIKTOK")
TOP_N = 20
EPSILON = 1e-9


def _load_v47():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7.1 must be installed on top of v4.7")
    spec = importlib.util.spec_from_file_location("hh_ceo_daily_pnl_v47_base", path)
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


def _pct(num: Any, den: Any) -> Optional[float]:
    d = _f(den)
    if abs(d) < 1e-12:
        return None
    return _f(num) / d


def _norm_identifier(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def _is_composite(value: Any) -> bool:
    return "+" in str(value or "")


def _report_date(package: Mapping[str, Any]) -> str:
    value = str((package.get("metadata") or {}).get("report_date") or "")
    dt.date.fromisoformat(value)
    return value


def _inventory_catalog(package: Mapping[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, set[str]], Dict[str, str]]:
    """Build inventory catalog and exact identifier index.

    Alias sources:
    - product_id (direct map)
    - canonical_sku / primary_ean from inventory_sku_master
    - source_sku / ean from inventory lot rows, attached only when the lot row can
      be linked unambiguously to an inventory master row.
    """
    catalog: Dict[str, Dict[str, Any]] = {}
    alias_index: Dict[str, set[str]] = defaultdict(set)
    pid_index: Dict[str, str] = {}

    for idx, raw in enumerate(package.get("inventory_sku_master") or []):
        row = copy.deepcopy(dict(raw))
        key = str(row.get("inventory_key") or row.get("product_id") or row.get("canonical_sku") or row.get("primary_ean") or f"ROW:{idx}")
        row["inventory_key"] = key
        row.setdefault("inventory_source_skus", [])
        row.setdefault("inventory_eans", [])
        catalog[key] = row
        pid = str(row.get("product_id") or "")
        if pid:
            pid_index[pid] = key
        for value in (row.get("canonical_sku"), row.get("primary_ean")):
            norm = _norm_identifier(value)
            if norm:
                alias_index[norm].add(key)

    # First pass: attach source identifiers to an existing row by product_id or
    # by a unique canonical/EAN alias. This makes exact source SKU matching useful
    # even when a sales fact lacks product_id.
    for lot in (package.get("inventory") or {}).get("lot_detail") or []:
        key: Optional[str] = None
        pid = str(lot.get("product_id") or "")
        if pid and pid in pid_index:
            key = pid_index[pid]
        if not key:
            candidates: set[str] = set()
            for value in (lot.get("canonical_sku"), lot.get("primary_ean"), lot.get("ean")):
                norm = _norm_identifier(value)
                if norm:
                    candidates |= alias_index.get(norm, set())
            if len(candidates) == 1:
                key = next(iter(candidates))
        if not key or key not in catalog:
            continue
        row = catalog[key]
        source_sku = str(lot.get("source_sku") or "").strip()
        ean = str(lot.get("ean") or "").strip()
        if source_sku and source_sku not in row["inventory_source_skus"]:
            row["inventory_source_skus"].append(source_sku)
            alias_index[_norm_identifier(source_sku)].add(key)
        if ean and ean not in row["inventory_eans"]:
            row["inventory_eans"].append(ean)
            alias_index[_norm_identifier(ean)].add(key)

    for row in catalog.values():
        row["inventory_source_skus"] = sorted(row.get("inventory_source_skus") or [])
        row["inventory_eans"] = sorted(row.get("inventory_eans") or [])
    return catalog, alias_index, pid_index


def _match_inventory_row(
    row: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
    alias_index: Mapping[str, set[str]],
    pid_index: Mapping[str, str],
) -> Tuple[Optional[str], str, List[str]]:
    source_skus = list(row.get("source_skus") or [])
    if row.get("source_sku"):
        source_skus.append(row.get("source_sku"))
    if row.get("is_composite_sku") or any(_is_composite(x) for x in source_skus):
        return None, "COMPOSITE_REQUIRES_COMPONENT_ALLOCATION", []

    pid = str(row.get("product_id") or "")
    if pid and pid in pid_index:
        return pid_index[pid], "MAPPED_PRODUCT_ID", [pid_index[pid]]

    candidates: set[str] = set()
    values: List[Any] = [row.get("canonical_sku"), row.get("primary_ean"), row.get("ean")]
    values.extend(source_skus)
    for value in values:
        norm = _norm_identifier(value)
        if norm:
            candidates |= set(alias_index.get(norm) or set())
    if len(candidates) == 1:
        key = next(iter(candidates))
        return key, "MAPPED_EXACT_IDENTIFIER", [key]
    if len(candidates) > 1:
        return None, "AMBIGUOUS_IDENTIFIER", sorted(candidates)
    return None, "UNMAPPED", []


def _expiry_breakdown(package: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]], alias_index, pid_index) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, Dict[Optional[str], float]] = defaultdict(lambda: defaultdict(float))
    for lot in (package.get("inventory") or {}).get("lot_detail") or []:
        key, status, _ = _match_inventory_row(lot, catalog, alias_index, pid_index)
        if not key:
            continue
        expiry = lot.get("expiry_date")
        expiry_s = str(expiry) if expiry not in (None, "") else None
        grouped[key][expiry_s] += _f(lot.get("stock_qty"))
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, values in grouped.items():
        rows = [{"expiry_date": expiry, "stock_qty": qty} for expiry, qty in values.items()]
        rows.sort(key=lambda r: (r["expiry_date"] is None, str(r["expiry_date"] or "9999-12-31")))
        out[key] = rows
    return out


def _inventory_sheet_summary(package: Mapping[str, Any], catalog, alias_index, pid_index) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    report_date = _report_date(package)
    expiry_map = _expiry_breakdown(package, catalog, alias_index, pid_index)
    rows_by_key: Dict[str, Dict[str, Any]] = {}

    for key, inv in catalog.items():
        breakdown = copy.deepcopy(expiry_map.get(key) or [])
        non_null_dates = [str(x.get("expiry_date")) for x in breakdown if x.get("expiry_date")]
        row = {
            "inventory_key": key,
            "product_id": inv.get("product_id"),
            "canonical_sku": inv.get("canonical_sku"),
            "primary_ean": inv.get("primary_ean"),
            "inventory_source_skus": copy.deepcopy(inv.get("inventory_source_skus") or []),
            "inventory_eans": copy.deepcopy(inv.get("inventory_eans") or []),
            "product_name": inv.get("product_name"),
            "current_stock_qty": _f(inv.get("current_stock_qty")),
            "stock_hn": inv.get("stock_hn"),
            "stock_hcm": inv.get("stock_hcm"),
            "stock_hcm_nhan_tem": inv.get("stock_hcm_nhan_tem"),
            "stock_hcm_total": inv.get("stock_hcm_total"),
            "stock_by_warehouse": copy.deepcopy(inv.get("stock_by_warehouse") or {}),
            "lot_count": inv.get("lot_count"),
            "expiry_date_count": len(set(non_null_dates)),
            "expiry_dates": sorted(set(non_null_dates)),
            "expiry_breakdown": breakdown,
            "earliest_expiry_date": min(non_null_dates) if non_null_dates else None,
            "latest_expiry_date": max(non_null_dates) if non_null_dates else None,
            "uom_values": copy.deepcopy(inv.get("uom_values") or []),
            "sold_qty_day_nhanh": 0.0,
            "sold_qty_day_shopee": 0.0,
            "sold_qty_day_tiktok": 0.0,
            "sold_qty_mtd_nhanh": 0.0,
            "sold_qty_mtd_shopee": 0.0,
            "sold_qty_mtd_tiktok": 0.0,
            "gift_qty_mtd_nhanh": 0.0,
            "gift_qty_mtd_shopee": 0.0,
            "gift_qty_mtd_tiktok": 0.0,
            "sales_skus_nhanh": set(),
            "sales_skus_shopee": set(),
            "sales_skus_tiktok": set(),
            "top20_channels": [],
        }
        rows_by_key[key] = row

    audit = {
        "total_sales_fact_sold_qty_by_channel": {ch: 0.0 for ch in INVENTORY_SALES_CHANNELS},
        "mapped_sold_qty_by_channel": {ch: 0.0 for ch in INVENTORY_SALES_CHANNELS},
        "unmapped_sold_qty_by_channel": {ch: 0.0 for ch in INVENTORY_SALES_CHANNELS},
        "mapped_fact_rows_by_channel": {ch: 0 for ch in INVENTORY_SALES_CHANNELS},
        "unmapped_fact_rows_by_channel": {ch: 0 for ch in INVENTORY_SALES_CHANNELS},
        "unmapped_examples": [],
    }

    for fact in package.get("sku_daily_fact") or []:
        ch = str(fact.get("channel") or "").upper()
        if ch not in INVENTORY_SALES_CHANNELS:
            continue
        sold = _f(fact.get("sold_qty"))
        gift = _f(fact.get("gift_qty"))
        audit["total_sales_fact_sold_qty_by_channel"][ch] += sold
        key, status, candidates = _match_inventory_row(fact, catalog, alias_index, pid_index)
        if key and key in rows_by_key:
            r = rows_by_key[key]
            r[f"sold_qty_mtd_{ch.lower()}"] += sold
            r[f"gift_qty_mtd_{ch.lower()}"] += gift
            if str(fact.get("report_date")) == report_date:
                r[f"sold_qty_day_{ch.lower()}"] += sold
            sku = str(fact.get("source_sku") or "").strip()
            if sku:
                r[f"sales_skus_{ch.lower()}"].add(sku)
            audit["mapped_sold_qty_by_channel"][ch] += sold
            audit["mapped_fact_rows_by_channel"][ch] += 1
        else:
            audit["unmapped_sold_qty_by_channel"][ch] += sold
            audit["unmapped_fact_rows_by_channel"][ch] += 1
            if len(audit["unmapped_examples"]) < 50 and (sold > 0 or gift > 0):
                audit["unmapped_examples"].append({
                    "channel": ch,
                    "source_sku": fact.get("source_sku"),
                    "ean": fact.get("ean"),
                    "product_name": fact.get("product_name"),
                    "sold_qty": sold,
                    "mapping_status": status,
                    "candidate_inventory_keys": candidates,
                })

    out: List[Dict[str, Any]] = []
    for row in rows_by_key.values():
        for ch in INVENTORY_SALES_CHANNELS:
            row[f"sales_skus_{ch.lower()}"] = sorted(row[f"sales_skus_{ch.lower()}"])
        row["sold_qty_mtd_3_channels"] = sum(row[f"sold_qty_mtd_{ch.lower()}"] for ch in INVENTORY_SALES_CHANNELS)
        row["gift_qty_mtd_3_channels"] = sum(row[f"gift_qty_mtd_{ch.lower()}"] for ch in INVENTORY_SALES_CHANNELS)
        row["sold_qty_day_3_channels"] = sum(row[f"sold_qty_day_{ch.lower()}"] for ch in INVENTORY_SALES_CHANNELS)
        row["sold_qty_mtd_3ch_to_current_stock_pct"] = _pct(row["sold_qty_mtd_3_channels"], row["current_stock_qty"])
        row["sold_stock_ratio_label"] = "SL bán MTD 3 kênh / Tồn hiện tại (không phải sell-through)"
        # Verify the requested all-date stock total is explicit.
        row["stock_qty_sum_all_expiry_dates"] = sum(_f(x.get("stock_qty")) for x in row.get("expiry_breakdown") or [])
        out.append(row)

    out.sort(key=lambda r: (-_f(r.get("sold_qty_mtd_3_channels")), -_f(r.get("current_stock_qty")), str(r.get("canonical_sku") or r.get("primary_ean") or "")))
    for ch in INVENTORY_SALES_CHANNELS:
        total = audit["total_sales_fact_sold_qty_by_channel"][ch]
        audit.setdefault("mapping_coverage_by_channel", {})[ch] = _pct(audit["mapped_sold_qty_by_channel"][ch], total) if total > 0 else 1.0
    return out, audit


def _build_top20(package: Mapping[str, Any], catalog, alias_index, pid_index) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in package.get("sku_inventory_summary") or []:
        ch = str(row.get("channel") or "").upper()
        if ch in CHANNELS and _f(row.get("sold_qty_mtd")) > 0:
            grouped[ch].append(row)

    result: Dict[str, List[Dict[str, Any]]] = {ch: [] for ch in CHANNELS}
    flat: List[Dict[str, Any]] = []
    report_date = _report_date(package)
    month_start = report_date[:8] + "01"

    for ch in CHANNELS:
        rows = sorted(
            grouped.get(ch) or [],
            key=lambda r: (-_f(r.get("sold_qty_mtd")), -_f(r.get("total_push_qty_mtd")), str(r.get("product_name") or ""), str(r.get("canonical_sku") or "")),
        )[:TOP_N]
        total_channel_sold = sum(_f(r.get("sold_qty_mtd")) for r in grouped.get(ch) or [])
        for rank, src in enumerate(rows, start=1):
            key, mapping_status, candidates = _match_inventory_row(src, catalog, alias_index, pid_index)
            inv = catalog.get(key) if key else None
            stock = _f(inv.get("current_stock_qty")) if inv else None
            sold = _f(src.get("sold_qty_mtd"))
            row = {
                "as_of_date": report_date,
                "month_start": month_start,
                "channel": ch,
                "rank_by_sold_qty": rank,
                "rank_metric": "sold_qty_mtd_excluding_gifts",
                "product_id": src.get("product_id") or (inv.get("product_id") if inv else None),
                "canonical_sku": src.get("canonical_sku") or (inv.get("canonical_sku") if inv else None),
                "primary_ean": src.get("primary_ean") or (inv.get("primary_ean") if inv else None),
                "source_skus": copy.deepcopy(src.get("source_skus") or []),
                "product_name": src.get("product_name") or (inv.get("product_name") if inv else None),
                "sold_qty_day": _f(src.get("sold_qty_day")),
                "sold_qty_mtd": sold,
                "gift_qty_day": _f(src.get("gift_qty_day")),
                "gift_qty_mtd": _f(src.get("gift_qty_mtd")),
                "total_push_qty_day": _f(src.get("total_push_qty_day")),
                "total_push_qty_mtd": _f(src.get("total_push_qty_mtd")),
                "channel_sold_qty_share_pct": _pct(sold, total_channel_sold),
                "inventory_key": key,
                "inventory_link_status": mapping_status,
                "inventory_candidate_keys": candidates,
                # Per user contract: stock is a report-facing field only for Top20.
                "current_stock_qty": stock if inv else None,
                "stock_hn": inv.get("stock_hn") if inv else None,
                "stock_hcm": inv.get("stock_hcm") if inv else None,
                "stock_hcm_nhan_tem": inv.get("stock_hcm_nhan_tem") if inv else None,
                "stock_hcm_total": inv.get("stock_hcm_total") if inv else None,
                "inventory_lot_count": inv.get("lot_count") if inv else None,
                "earliest_expiry_date": inv.get("earliest_expiry_date") if inv else None,
                "sold_qty_mtd_to_current_stock_pct": _pct(sold, stock) if inv and stock is not None else None,
                "sold_stock_ratio_label": "SL bán MTD / Tồn hiện tại (không phải sell-through)",
            }
            result[ch].append(row)
            flat.append(row)
    return result, flat


def _apply_top20_flags(inventory_rows: List[Dict[str, Any]], top20: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    by_key = {str(r.get("inventory_key")): r for r in inventory_rows if r.get("inventory_key")}
    for ch, rows in top20.items():
        for row in rows:
            key = str(row.get("inventory_key") or "")
            if key and key in by_key and ch not in by_key[key]["top20_channels"]:
                by_key[key]["top20_channels"].append(ch)
    for r in inventory_rows:
        r["top20_channels"] = sorted(r.get("top20_channels") or [])
        for ch in CHANNELS:
            r[f"is_top20_{ch.lower()}"] = ch in r["top20_channels"]


def _postprocess_v471(package: MutableMapping[str, Any]) -> None:
    catalog, alias_index, pid_index = _inventory_catalog(package)
    inventory_rows, mapping_audit = _inventory_sheet_summary(package, catalog, alias_index, pid_index)
    top20, top20_flat = _build_top20(package, catalog, alias_index, pid_index)
    _apply_top20_flags(inventory_rows, top20)

    package["top_20_sku_by_channel"] = top20
    package["top_20_sku_rows"] = top20_flat
    package["inventory_sheet_sku_summary"] = inventory_rows
    package["inventory_sales_mapping_audit"] = mapping_audit
    package.setdefault("inventory", {})["sku_summary_for_excel"] = copy.deepcopy(inventory_rows)

    meta = package.setdefault("metadata", {})
    meta["package_version"] = PACKAGE_VERSION
    meta["package_version_int"] = PACKAGE_VERSION_INT
    meta["upgrade_scope_v4_7_1"] = "Top20 sold SKU per channel + summed inventory by SKU across expiry lots + NHANH/SHOPEE/TIKTOK sales mapped onto inventory SKU"

    contract = package.setdefault("excel_render_contract", {})
    contract["top_sku_source"] = "top_20_sku_by_channel"
    contract["top_sku_limit_per_channel"] = TOP_N
    contract["top_sku_rank_metric"] = "sold_qty_mtd (gifts excluded from ranking)"
    contract["top_sku_inventory_rule"] = "Show inventory quantity in the channel Top20 table only. Inventory is the actual current snapshot sum across all lots/expiry dates for the mapped SKU."
    contract["top_sku_columns"] = [
        "rank_by_sold_qty", "canonical_sku", "primary_ean", "product_name",
        "sold_qty_mtd", "gift_qty_mtd", "total_push_qty_mtd", "current_stock_qty",
        "sold_qty_mtd_to_current_stock_pct", "earliest_expiry_date",
    ]
    contract["inventory_primary_table_source"] = "inventory_sheet_sku_summary"
    contract["inventory_primary_table_grain"] = "one row per mapped inventory SKU/product; stock summed across all lots/expiry dates"
    contract["inventory_sales_columns"] = [
        "sold_qty_mtd_nhanh", "sold_qty_mtd_tiktok", "sold_qty_mtd_shopee",
        "sold_qty_mtd_3_channels", "current_stock_qty", "sold_qty_mtd_3ch_to_current_stock_pct",
    ]
    contract["inventory_lot_detail_role"] = "audit/drill-down only; do not duplicate one SKU into multiple rows in the primary inventory table"

    policy = package.setdefault("report_rendering_policy", {})
    policy["top_sku"] = {
        "source": "top_20_sku_by_channel",
        "limit": TOP_N,
        "rank_by": "sold_qty_mtd",
        "exclude_gifts_from_rank": True,
        "show_stock_only_for_top20": True,
    }
    policy["inventory_sheet"] = {
        "sheet": "06_Ton_kho_Can_date",
        "primary_source": "inventory_sheet_sku_summary",
        "lot_audit_source": "inventory.lot_detail",
        "stock_rule": "sum current stock for the SKU across all lots/expiry dates in the resolved snapshot",
        "sales_channels": list(INVENTORY_SALES_CHANNELS),
        "show_sales_qty": "MTD sold quantity; gifts kept separate and do not count as sold",
        "do_not_render_ND": True,
    }

    package.setdefault("display_semantics", {})["sold_qty_mtd_to_current_stock_pct"] = "MTD sold quantity divided by current stock snapshot. This is a comparison ratio, not sell-through from opening inventory."
    package.setdefault("pnl_rules", {})["v4_7_1_scope"] = "No P&L math change. v4.7.1 changes SKU ranking/rendering and inventory-to-sales mapping only."

    # DQ is deliberately transparent because unresolved sales SKU mapping must not
    # silently become zero stock.
    dq = package.setdefault("data_quality", {})
    mapped = sum(_f(v) for v in mapping_audit["mapped_sold_qty_by_channel"].values())
    total = sum(_f(v) for v in mapping_audit["total_sales_fact_sold_qty_by_channel"].values())
    dq["v4_7_1_top20_inventory"] = {
        "top20_rows_by_channel": {ch: len(top20.get(ch) or []) for ch in CHANNELS},
        "inventory_sheet_sku_rows": len(inventory_rows),
        "three_channel_sales_sold_qty_total": total,
        "three_channel_sales_sold_qty_mapped": mapped,
        "three_channel_sales_mapping_coverage": _pct(mapped, total) if total > 0 else 1.0,
        "mapping_coverage_by_channel": mapping_audit.get("mapping_coverage_by_channel"),
    }
    unmapped = total - mapped
    if unmapped > EPSILON:
        warning = (
            f"v4.7.1 inventory sales mapping leaves {unmapped:,.2f} sold units from NHANH/SHOPEE/TIKTOK unresolved; "
            "inventory stock is not invented for those SKU aliases/composites. Review inventory_sales_mapping_audit.unmapped_examples."
        )
        warnings = dq.setdefault("warnings", [])
        if warning not in warnings:
            warnings.append(warning)
    if hasattr(_load_v47(), "_refresh_dq_status"):
        _load_v47()._refresh_dq_status(package)


def build_package_v471(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    include_growth_context: bool = True,
    require_targets: bool = True,
    require_inventory: bool = True,
):
    v47 = _load_v47()
    base, package = v47.build_package_v47(
        report_date_str,
        compare_mode,
        explicit_compare_date,
        vat_rates,
        include_growth_context=include_growth_context,
        require_targets=require_targets,
        require_inventory=require_inventory,
    )
    _postprocess_v471(package)
    base.PACKAGE_VERSION = PACKAGE_VERSION
    base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
    return base, base.clean(package)


def self_test() -> int:
    failures: List[str] = []
    package: Dict[str, Any] = {
        "metadata": {"report_date": "2026-08-17"},
        "inventory_sku_master": [{
            "inventory_key": "P1", "product_id": "P1", "canonical_sku": "SKU1", "primary_ean": "123",
            "product_name": "Product 1", "current_stock_qty": 50, "stock_hn": 20, "stock_hcm": 20,
            "stock_hcm_nhan_tem": 10, "stock_hcm_total": 30, "stock_by_warehouse": {"HN": 20, "HCM": 20, "HCM - NHAN TEM": 10},
            "lot_count": 2, "uom_values": ["chai"], "earliest_expiry_date": "2026-09-01",
        }],
        "inventory": {"lot_detail": [
            {"product_id": "P1", "source_sku": "SKU1", "ean": "123", "canonical_sku": "SKU1", "primary_ean": "123", "expiry_date": "2026-09-01", "stock_qty": 20},
            {"product_id": "P1", "source_sku": "SKU1", "ean": "123", "canonical_sku": "SKU1", "primary_ean": "123", "expiry_date": "2026-12-01", "stock_qty": 30},
        ]},
        "sku_daily_fact": [
            {"report_date": "2026-08-17", "channel": "NHANH", "source_sku": "SKU1", "product_id": "P1", "canonical_sku": "SKU1", "primary_ean": "123", "sold_qty": 7, "gift_qty": 1},
            {"report_date": "2026-08-16", "channel": "SHOPEE", "source_sku": "SKU1", "product_id": "P1", "canonical_sku": "SKU1", "primary_ean": "123", "sold_qty": 3, "gift_qty": 0},
        ],
        "sku_inventory_summary": [
            {"channel": "NHANH", "product_id": "P1", "canonical_sku": "SKU1", "primary_ean": "123", "source_skus": ["SKU1"], "product_name": "Product 1", "sold_qty_day": 7, "sold_qty_mtd": 10, "gift_qty_day": 1, "gift_qty_mtd": 1, "total_push_qty_day": 8, "total_push_qty_mtd": 11},
        ],
        "excel_render_contract": {}, "report_rendering_policy": {}, "data_quality": {"warnings": [], "errors": [], "can_send": True, "status": "PASS"},
    }
    catalog, alias, pid = _inventory_catalog(package)
    inv_rows, audit = _inventory_sheet_summary(package, catalog, alias, pid)
    if len(inv_rows) != 1 or abs(inv_rows[0]["current_stock_qty"] - 50) > EPSILON:
        failures.append("inventory one-row SKU total failed")
    if abs(inv_rows[0]["stock_qty_sum_all_expiry_dates"] - 50) > EPSILON or inv_rows[0]["expiry_date_count"] != 2:
        failures.append("multi-expiry stock sum failed")
    if abs(inv_rows[0]["sold_qty_mtd_nhanh"] - 7) > EPSILON or abs(inv_rows[0]["sold_qty_mtd_shopee"] - 3) > EPSILON:
        failures.append("3-channel inventory sales mapping failed")
    top, flat = _build_top20(package, catalog, alias, pid)
    if len(top["NHANH"]) != 1 or top["NHANH"][0]["rank_by_sold_qty"] != 1:
        failures.append("top20 rank failed")
    if abs(_f(top["NHANH"][0]["current_stock_qty"]) - 50) > EPSILON:
        failures.append("top20 inventory attach failed")
    if abs(_f(top["NHANH"][0]["sold_qty_mtd_to_current_stock_pct"]) - 0.2) > EPSILON:
        failures.append("sold/current stock ratio failed")
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for x in failures:
            print("-", x, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Top20 sold-qty ranking: PASS")
    print("Inventory sum across expiry dates: PASS")
    print("NHANH/SHOPEE/TIKTOK inventory sales mapping: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="Report date YYYY-MM-DD")
    ap.add_argument("--compare-mode", choices=("previous_day", "previous_week_same_day", "previous_month_same_day"), default="previous_day")
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

    v47 = _load_v47()
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

    base, package = build_package_v471(
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
    json_path = out_dir / f"ceo_daily_pnl_package_v4_7_1_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_7_1_{report_date}.md"
    json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=base.json_default), encoding="utf-8")
    md = base.markdown_summary(package)
    md += "\n\n## v4.7.1 Top20 & Inventory Sales Mapping\n"
    md += f"- Top SKU limit/channel: {TOP_N}, ranked by sold_qty_mtd excluding gifts\n"
    inv = package.get("inventory") or {}
    md += f"- Inventory snapshot: {inv.get('snapshot_date')}\n"
    audit = package.get("inventory_sales_mapping_audit") or {}
    md += f"- Inventory sales mapping coverage: {(package.get('data_quality') or {}).get('v4_7_1_top20_inventory', {}).get('three_channel_sales_mapping_coverage')}\n"
    md_path.write_text(md, encoding="utf-8")
    if args.store_db:
        base.PACKAGE_VERSION = PACKAGE_VERSION
        base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
        base.store_best_effort(report_date, package, md, str(json_path), str(md_path))

    dq = package.get("data_quality") or {}
    print(f"JSON package: {json_path}")
    print(f"Markdown summary: {md_path}")
    print(f"Data quality: {dq.get('status')}")
    for warning in dq.get("warnings") or []:
        print(f"WARNING: {warning}")
    for error in dq.get("errors") or []:
        print(f"ERROR: {error}")
    return 0 if dq.get("can_send") else 1


if __name__ == "__main__":
    raise SystemExit(main())
