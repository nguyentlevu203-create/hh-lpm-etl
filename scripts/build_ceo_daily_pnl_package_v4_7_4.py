#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEO Daily P&L package v4.7.4 — product alias mapping + gift/push visibility.

Layered on v4.7.3 HF1. Financial math, exact-cancel semantics, target logic,
previous-MTD comparison, and inventory snapshot math are not changed.

v4.7.4 adds a deterministic product identity layer for mixed short SKU / EAN
identifiers, explicit bundle-component resolution for inventory push, and a
CEO-ready sold/gift/total-push view by product and channel.

Key rules:
- Do not truncate or infer a short SKU from an EAN.
- Use only approved exact alias pairs, exact inventory identifiers, or exact EAN.
- A numeric 8..14 digit identifier is treated as an EAN candidate.
- Composite SKU syntax is parsed only when explicit ("A+B", "A x 2").
- Composite component roles (sold vs gift) are NOT guessed. Their push quantity is
  classified as bundle_unallocated_push unless an approved component-role rule is
  supplied later.
- Current stock remains the actual inventory snapshot; never infer opening-stock
  less push.
"""
from __future__ import annotations

import argparse
import copy
import csv
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

PACKAGE_VERSION = "v4.7.4"
PACKAGE_VERSION_INT = 474
BASE_FILENAME = "build_ceo_daily_pnl_package_v4_7_3.py"
CHANNELS: Tuple[str, ...] = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
TOP_N = 20
EPSILON = 1.0
DEFAULT_ALIAS_FILE = Path("config/product_sku_alias_master_v4_7_4.csv")
DEFAULT_BUNDLE_FILE = Path("config/bundle_component_map_v4_7_4.csv")


def _load_v473():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7.4 must be installed on top of v4.7.3 HF1")
    spec = importlib.util.spec_from_file_location("hh_ceo_daily_pnl_v473_base", path)
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


def _maybe_f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
        return None if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return None


def _pct(num: Any, den: Any) -> Optional[float]:
    d = _maybe_f(den)
    if d is None or abs(d) < 1e-12:
        return None
    return _f(num) / d


def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\ufeff", " ").replace("\u200b", " ").replace("\xa0", " ")).strip()


def _is_probable_ean(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{8,14}", _clean(value)))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve_config_path(raw: Optional[str], default_rel: Path) -> Path:
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (_repo_root() / path)
    return _repo_root() / default_rel


def load_alias_master(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing alias master: {path}")
    short_to_ean: Dict[str, str] = {}
    ean_to_short: Dict[str, str] = {}
    rows: List[Dict[str, Any]] = []
    conflicts: List[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"canonical_ean", "short_sku", "status"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"Alias master missing columns: {sorted(required - set(reader.fieldnames or []))}")
        for raw in reader:
            ean = _clean(raw.get("canonical_ean"))
            short = _clean(raw.get("short_sku"))
            status = _clean(raw.get("status")).upper()
            if status != "APPROVED":
                continue
            if not _is_probable_ean(ean):
                conflicts.append(f"invalid EAN {ean!r}")
                continue
            if not short:
                conflicts.append(f"blank short_sku for EAN {ean}")
                continue
            sn = _norm(short)
            if sn in short_to_ean and short_to_ean[sn] != ean:
                conflicts.append(f"short SKU {short} maps to both {short_to_ean[sn]} and {ean}")
            en = _norm(ean)
            if en in ean_to_short and _norm(ean_to_short[en]) != sn:
                conflicts.append(f"EAN {ean} maps to both {ean_to_short[en]} and {short}")
            short_to_ean[sn] = ean
            ean_to_short[en] = short
            rows.append({
                "canonical_ean": ean,
                "short_sku": short,
                "source_row": raw.get("source_row"),
                "source_file": raw.get("source_file"),
            })
    if conflicts:
        raise ValueError("Alias master conflicts: " + "; ".join(conflicts[:20]))
    return {
        "path": str(path),
        "approved_rows": rows,
        "approved_count": len(rows),
        "short_to_ean": short_to_ean,
        "ean_to_short": ean_to_short,
    }


def load_bundle_map(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            approved = _clean(raw.get("approved")).lower() in {"1", "true", "yes", "y"}
            if not approved:
                continue
            bundle = _clean(raw.get("bundle_sku"))
            comp = _clean(raw.get("component_sku"))
            if not bundle or not comp:
                continue
            qty = _f(raw.get("qty_per_bundle"), 1.0)
            if qty <= 0:
                continue
            role = _clean(raw.get("component_role")).upper() or "UNSPECIFIED"
            if role not in {"SOLD", "GIFT", "UNSPECIFIED"}:
                role = "UNSPECIFIED"
            result[_norm(bundle)].append({
                "component_sku": comp,
                "qty_per_bundle": qty,
                "component_role": role,
                "source": "APPROVED_BUNDLE_MAP",
            })
    return result


def build_inventory_catalog(package: Mapping[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, set[str]], Dict[str, str]]:
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
        pid = _clean(row.get("product_id"))
        if pid:
            pid_index[pid] = key
        for value in (row.get("canonical_sku"), row.get("primary_ean")):
            n = _norm(value)
            if n:
                alias_index[n].add(key)
    for lot in (package.get("inventory") or {}).get("lot_detail") or []:
        key: Optional[str] = None
        pid = _clean(lot.get("product_id"))
        if pid and pid in pid_index:
            key = pid_index[pid]
        if not key:
            candidates: set[str] = set()
            for value in (lot.get("canonical_sku"), lot.get("primary_ean"), lot.get("ean"), lot.get("source_sku")):
                n = _norm(value)
                if n:
                    candidates |= set(alias_index.get(n) or set())
            if len(candidates) == 1:
                key = next(iter(candidates))
        if not key or key not in catalog:
            continue
        row = catalog[key]
        source_sku = _clean(lot.get("source_sku"))
        ean = _clean(lot.get("ean"))
        if source_sku and source_sku not in row["inventory_source_skus"]:
            row["inventory_source_skus"].append(source_sku)
            alias_index[_norm(source_sku)].add(key)
        if ean and ean not in row["inventory_eans"]:
            row["inventory_eans"].append(ean)
            alias_index[_norm(ean)].add(key)
    for row in catalog.values():
        row["inventory_source_skus"] = sorted(row.get("inventory_source_skus") or [])
        row["inventory_eans"] = sorted(row.get("inventory_eans") or [])
    return catalog, alias_index, pid_index


def _scalar_resolution(identifier: Any, alias_master: Mapping[str, Any], alias_index: Mapping[str, set[str]]) -> Dict[str, Any]:
    value = _clean(identifier)
    if not value:
        return {"status": "EMPTY", "identifier": value, "candidates": []}
    candidates: set[str] = set(alias_index.get(_norm(value)) or set())
    ean: Optional[str] = value if _is_probable_ean(value) else None
    short_map = alias_master.get("short_to_ean") or {}
    if not ean:
        ean = short_map.get(_norm(value))
    if ean:
        candidates |= set(alias_index.get(_norm(ean)) or set())
    if len(candidates) == 1:
        return {
            "status": "MAPPED",
            "identifier": value,
            "canonical_ean": ean,
            "inventory_key": next(iter(candidates)),
            "candidates": sorted(candidates),
            "mapping_method": "EXACT_EAN" if _is_probable_ean(value) else ("APPROVED_SHORT_SKU_TO_EAN" if ean else "EXACT_INVENTORY_IDENTIFIER"),
        }
    if len(candidates) > 1:
        return {"status": "AMBIGUOUS", "identifier": value, "canonical_ean": ean, "candidates": sorted(candidates)}
    if ean:
        # Product identity is still deterministic from the approved alias/EAN even
        # when the current positive-stock snapshot has no inventory row for it.
        # Stock must stay null; do not invent zero.
        return {
            "status": "IDENTITY_ONLY",
            "identifier": value,
            "canonical_ean": ean,
            "candidates": [],
            "mapping_method": "EXACT_EAN_NO_ACTIVE_STOCK" if _is_probable_ean(value) else "APPROVED_SHORT_SKU_TO_EAN_NO_ACTIVE_STOCK",
        }
    return {"status": "UNMAPPED", "identifier": value, "canonical_ean": ean, "candidates": []}


def _parse_component_token(token: str) -> Tuple[str, float]:
    token = _clean(token)
    m = re.fullmatch(r"(.+?)\s+[xX×]\s*(\d+(?:\.\d+)?)", token)
    if not m:
        return token, 1.0
    sku = _clean(m.group(1))
    qty = float(m.group(2))
    return sku, qty


def parse_composite_sku(raw_sku: Any, bundle_map: Mapping[str, List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    text = _clean(raw_sku)
    if not text:
        return None
    approved = bundle_map.get(_norm(text))
    if approved:
        return copy.deepcopy(list(approved))
    is_explicit = "+" in text or bool(re.fullmatch(r".+?\s+[xX×]\s*\d+(?:\.\d+)?", text))
    if not is_explicit:
        return None
    tokens = [_clean(x) for x in text.split("+") if _clean(x)]
    if not tokens:
        return None
    out: List[Dict[str, Any]] = []
    for token in tokens:
        sku, qty = _parse_component_token(token)
        if not sku or qty <= 0:
            return None
        out.append({
            "component_sku": sku,
            "qty_per_bundle": qty,
            "component_role": "UNSPECIFIED",
            "source": "AUTO_PARSED_EXPLICIT_SKU_SYNTAX",
        })
    return out


def resolve_sales_row(
    row: Mapping[str, Any],
    alias_master: Mapping[str, Any],
    bundle_map: Mapping[str, List[Dict[str, Any]]],
    catalog: Mapping[str, Mapping[str, Any]],
    alias_index: Mapping[str, set[str]],
    pid_index: Mapping[str, str],
) -> Dict[str, Any]:
    source_skus = [_clean(x) for x in (row.get("source_skus") or []) if _clean(x)]
    if row.get("source_sku") and _clean(row.get("source_sku")) not in source_skus:
        source_skus.append(_clean(row.get("source_sku")))
    composite_source = next((s for s in source_skus if parse_composite_sku(s, bundle_map)), None)
    if composite_source:
        components = parse_composite_sku(composite_source, bundle_map) or []
        resolved_components: List[Dict[str, Any]] = []
        all_mapped = True
        for comp in components:
            r = _scalar_resolution(comp.get("component_sku"), alias_master, alias_index)
            inv = catalog.get(r.get("inventory_key")) if r.get("inventory_key") else None
            rr = dict(comp)
            rr.update({
                "mapping_status": r.get("status"),
                "mapping_method": r.get("mapping_method"),
                "canonical_ean": r.get("canonical_ean") or ((inv or {}).get("primary_ean")),
                "inventory_key": r.get("inventory_key"),
                "product_id": (inv or {}).get("product_id"),
                "canonical_sku": (inv or {}).get("canonical_sku"),
                "product_name": (inv or {}).get("product_name"),
                "current_stock_qty": (inv or {}).get("current_stock_qty"),
                "stock_hn": (inv or {}).get("stock_hn"),
                "stock_hcm": (inv or {}).get("stock_hcm"),
                "stock_hcm_nhan_tem": (inv or {}).get("stock_hcm_nhan_tem"),
                "earliest_expiry_date": (inv or {}).get("earliest_expiry_date"),
            })
            if r.get("status") not in {"MAPPED", "IDENTITY_ONLY"}:
                all_mapped = False
            resolved_components.append(rr)
        return {
            "status": "MAPPED_BUNDLE_COMPONENTS" if all_mapped else "UNMAPPED_BUNDLE_COMPONENTS",
            "source_sku": composite_source,
            "is_bundle": True,
            "components": resolved_components,
            "inventory_key": None,
            "canonical_ean": None,
            "mapping_method": "BUNDLE_COMPONENT_RESOLUTION",
        }

    candidates: Dict[str, Dict[str, Any]] = {}
    identity_only: Dict[str, Dict[str, Any]] = {}
    for value in source_skus + [row.get("canonical_sku"), row.get("primary_ean"), row.get("ean")]:
        r = _scalar_resolution(value, alias_master, alias_index)
        if r.get("status") == "MAPPED":
            candidates[str(r.get("inventory_key"))] = r
        elif r.get("status") == "IDENTITY_ONLY" and r.get("canonical_ean"):
            identity_only[str(r.get("canonical_ean"))] = r
        elif r.get("status") == "AMBIGUOUS":
            return {"status": "AMBIGUOUS", "source_skus": source_skus, "candidates": r.get("candidates") or [], "is_bundle": False}
    pid = _clean(row.get("product_id"))
    if pid and pid in pid_index:
        candidates.setdefault(pid_index[pid], {
            "status": "MAPPED", "inventory_key": pid_index[pid], "mapping_method": "PRODUCT_ID_FALLBACK", "identifier": pid,
        })
    if len(candidates) == 1:
        key, r = next(iter(candidates.items()))
        inv = catalog.get(key) or {}
        return {
            "status": "MAPPED_SINGLE",
            "is_bundle": False,
            "inventory_key": key,
            "mapping_method": r.get("mapping_method"),
            "matched_identifier": r.get("identifier"),
            "canonical_ean": r.get("canonical_ean") or inv.get("primary_ean"),
            "product_id": inv.get("product_id") or row.get("product_id"),
            "canonical_sku": inv.get("canonical_sku") or row.get("canonical_sku"),
            "product_name": inv.get("product_name") or row.get("product_name"),
            "current_stock_qty": inv.get("current_stock_qty"),
            "stock_hn": inv.get("stock_hn"),
            "stock_hcm": inv.get("stock_hcm"),
            "stock_hcm_nhan_tem": inv.get("stock_hcm_nhan_tem"),
            "stock_hcm_total": inv.get("stock_hcm_total"),
            "lot_count": inv.get("lot_count"),
            "earliest_expiry_date": inv.get("earliest_expiry_date"),
            "source_skus": source_skus,
        }
    if len(candidates) > 1:
        return {"status": "AMBIGUOUS", "is_bundle": False, "source_skus": source_skus, "candidates": sorted(candidates)}
    if len(identity_only) == 1:
        ean, r = next(iter(identity_only.items()))
        return {
            "status": "MAPPED_IDENTITY_NO_STOCK",
            "is_bundle": False,
            "inventory_key": None,
            "mapping_method": r.get("mapping_method"),
            "matched_identifier": r.get("identifier"),
            "canonical_ean": ean,
            "product_id": row.get("product_id"),
            "canonical_sku": row.get("canonical_sku") or ean,
            "product_name": row.get("product_name"),
            "current_stock_qty": None,
            "stock_hn": None,
            "stock_hcm": None,
            "stock_hcm_nhan_tem": None,
            "stock_hcm_total": None,
            "lot_count": None,
            "earliest_expiry_date": None,
            "source_skus": source_skus,
        }
    if len(identity_only) > 1:
        return {"status": "AMBIGUOUS_IDENTITY", "is_bundle": False, "source_skus": source_skus, "candidates": sorted(identity_only)}
    return {"status": "UNMAPPED", "is_bundle": False, "source_skus": source_skus, "candidates": []}


def _push_row_base(channel: str, inv: Mapping[str, Any], key: str) -> Dict[str, Any]:
    return {
        "channel": channel,
        "inventory_key": key,
        "product_id": inv.get("product_id"),
        "canonical_sku": inv.get("canonical_sku"),
        "primary_ean": inv.get("primary_ean"),
        "product_name": inv.get("product_name"),
        "source_identifiers": set(),
        "sold_qty_day": 0.0,
        "gift_qty_day": 0.0,
        "bundle_unallocated_push_qty_day": 0.0,
        "total_push_qty_day": 0.0,
        "sold_qty_mtd": 0.0,
        "gift_qty_mtd": 0.0,
        "bundle_unallocated_push_qty_mtd": 0.0,
        "total_push_qty_mtd": 0.0,
        "current_stock_qty": inv.get("current_stock_qty"),
        "stock_hn": inv.get("stock_hn"),
        "stock_hcm": inv.get("stock_hcm"),
        "stock_hcm_nhan_tem": inv.get("stock_hcm_nhan_tem"),
        "stock_hcm_total": inv.get("stock_hcm_total"),
        "lot_count": inv.get("lot_count"),
        "earliest_expiry_date": inv.get("earliest_expiry_date"),
        "normal_source_rows": 0,
        "bundle_component_source_rows": 0,
    }


def build_inventory_push(
    package: Mapping[str, Any], alias_master: Mapping[str, Any], bundle_map: Mapping[str, List[Dict[str, Any]]]
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    catalog, alias_index, pid_index = build_inventory_catalog(package)
    acc: Dict[Tuple[str, str], Dict[str, Any]] = {}
    unresolved: List[Dict[str, Any]] = []
    audit = {
        "source_sold_qty_by_channel": {ch: 0.0 for ch in CHANNELS},
        "source_push_qty_by_channel": {ch: 0.0 for ch in CHANNELS},
        "resolved_sold_qty_by_channel": {ch: 0.0 for ch in CHANNELS},
        "resolved_push_qty_by_channel": {ch: 0.0 for ch in CHANNELS},
        "resolved_rows_by_channel": {ch: 0 for ch in CHANNELS},
        "unresolved_rows_by_channel": {ch: 0 for ch in CHANNELS},
    }
    for src in package.get("sku_inventory_summary") or []:
        ch = _clean(src.get("channel")).upper()
        if ch not in CHANNELS:
            continue
        sold_day = _f(src.get("sold_qty_day")); gift_day = _f(src.get("gift_qty_day")); push_day = _f(src.get("total_push_qty_day"), sold_day + gift_day)
        sold_mtd = _f(src.get("sold_qty_mtd")); gift_mtd = _f(src.get("gift_qty_mtd")); push_mtd = _f(src.get("total_push_qty_mtd"), sold_mtd + gift_mtd)
        audit["source_sold_qty_by_channel"][ch] += sold_mtd
        audit["source_push_qty_by_channel"][ch] += push_mtd
        resolution = resolve_sales_row(src, alias_master, bundle_map, catalog, alias_index, pid_index)
        status = resolution.get("status")
        if status in {"MAPPED_SINGLE", "MAPPED_IDENTITY_NO_STOCK"}:
            if status == "MAPPED_SINGLE":
                key = str(resolution.get("inventory_key"))
                inv = catalog[key]
            else:
                key = "EAN:" + str(resolution.get("canonical_ean"))
                inv = {
                    "product_id": resolution.get("product_id"),
                    "canonical_sku": resolution.get("canonical_sku") or resolution.get("canonical_ean"),
                    "primary_ean": resolution.get("canonical_ean"),
                    "product_name": resolution.get("product_name"),
                    "current_stock_qty": None,
                    "stock_hn": None, "stock_hcm": None, "stock_hcm_nhan_tem": None, "stock_hcm_total": None,
                    "lot_count": None, "earliest_expiry_date": None,
                }
            row = acc.setdefault((ch, key), _push_row_base(ch, inv, key))
            row["source_identifiers"].update(str(x) for x in (src.get("source_skus") or []) if _clean(x))
            row["sold_qty_day"] += sold_day; row["gift_qty_day"] += gift_day; row["total_push_qty_day"] += push_day
            row["sold_qty_mtd"] += sold_mtd; row["gift_qty_mtd"] += gift_mtd; row["total_push_qty_mtd"] += push_mtd
            row["normal_source_rows"] += 1
            audit["resolved_sold_qty_by_channel"][ch] += sold_mtd
            audit["resolved_push_qty_by_channel"][ch] += push_mtd
            audit["resolved_rows_by_channel"][ch] += 1
        elif status == "MAPPED_BUNDLE_COMPONENTS":
            audit["resolved_sold_qty_by_channel"][ch] += sold_mtd
            audit["resolved_push_qty_by_channel"][ch] += push_mtd
            audit["resolved_rows_by_channel"][ch] += 1
            for comp in resolution.get("components") or []:
                if comp.get("inventory_key"):
                    key = str(comp.get("inventory_key"))
                    inv = catalog[key]
                else:
                    key = "EAN:" + str(comp.get("canonical_ean"))
                    inv = {
                        "product_id": comp.get("product_id"),
                        "canonical_sku": comp.get("canonical_sku") or comp.get("canonical_ean"),
                        "primary_ean": comp.get("canonical_ean"),
                        "product_name": comp.get("product_name"),
                        "current_stock_qty": None,
                        "stock_hn": None, "stock_hcm": None, "stock_hcm_nhan_tem": None, "stock_hcm_total": None,
                        "lot_count": None, "earliest_expiry_date": None,
                    }
                mult = _f(comp.get("qty_per_bundle"), 1.0)
                row = acc.setdefault((ch, key), _push_row_base(ch, inv, key))
                row["source_identifiers"].add(str(resolution.get("source_sku") or ""))
                role = str(comp.get("component_role") or "UNSPECIFIED").upper()
                if role == "SOLD":
                    row["sold_qty_day"] += push_day * mult; row["sold_qty_mtd"] += push_mtd * mult
                elif role == "GIFT":
                    row["gift_qty_day"] += push_day * mult; row["gift_qty_mtd"] += push_mtd * mult
                else:
                    row["bundle_unallocated_push_qty_day"] += push_day * mult
                    row["bundle_unallocated_push_qty_mtd"] += push_mtd * mult
                row["total_push_qty_day"] += push_day * mult
                row["total_push_qty_mtd"] += push_mtd * mult
                row["bundle_component_source_rows"] += 1
        else:
            audit["unresolved_rows_by_channel"][ch] += 1
            unresolved.append({
                "channel": ch,
                "sku": src.get("canonical_sku") or ((src.get("source_skus") or [None])[0]),
                "source_skus": copy.deepcopy(src.get("source_skus") or []),
                "product_name": src.get("product_name"),
                "sold_qty_mtd": sold_mtd,
                "gift_qty_mtd": gift_mtd,
                "total_push_qty_mtd": push_mtd,
                "mapping_status": status,
                "mapping_detail": resolution,
            })
    by_channel: Dict[str, List[Dict[str, Any]]] = {ch: [] for ch in CHANNELS}
    flat: List[Dict[str, Any]] = []
    for (ch, _key), row in acc.items():
        row["source_identifiers"] = sorted(x for x in row["source_identifiers"] if x)
        row["push_mtd_to_current_stock_pct"] = _pct(row["total_push_qty_mtd"], row.get("current_stock_qty"))
        row["current_stock_to_push_mtd"] = _pct(row.get("current_stock_qty"), row["total_push_qty_mtd"])
        row["ratio_note"] = "Đẩy MTD/Tồn hiện tại chỉ là tỷ lệ so sánh với snapshot hiện tại; không phải sell-through/tồn đầu kỳ."
        by_channel[ch].append(row); flat.append(row)
    for ch in CHANNELS:
        by_channel[ch].sort(key=lambda r: (-_f(r.get("total_push_qty_mtd")), str(r.get("primary_ean") or r.get("canonical_sku") or "")))
    flat.sort(key=lambda r: (r["channel"], -_f(r.get("total_push_qty_mtd"))))
    company_acc: Dict[str, Dict[str, Any]] = {}
    for row in flat:
        key = str(row["inventory_key"])
        if key not in company_acc:
            base_row = copy.deepcopy(row)
            base_row["channel"] = "TOTAL"
            base_row["source_channels"] = set()
            base_row["source_identifiers"] = set()
            for f in ("sold_qty_day","gift_qty_day","bundle_unallocated_push_qty_day","total_push_qty_day","sold_qty_mtd","gift_qty_mtd","bundle_unallocated_push_qty_mtd","total_push_qty_mtd","normal_source_rows","bundle_component_source_rows"):
                base_row[f] = 0.0
            company_acc[key] = base_row
        out = company_acc[key]
        out["source_channels"].add(row["channel"])
        out["source_identifiers"].update(row.get("source_identifiers") or [])
        for f in ("sold_qty_day","gift_qty_day","bundle_unallocated_push_qty_day","total_push_qty_day","sold_qty_mtd","gift_qty_mtd","bundle_unallocated_push_qty_mtd","total_push_qty_mtd","normal_source_rows","bundle_component_source_rows"):
            out[f] += _f(row.get(f))
    company: List[Dict[str, Any]] = []
    for row in company_acc.values():
        row["source_channels"] = sorted(row["source_channels"])
        row["source_identifiers"] = sorted(row["source_identifiers"])
        row["push_mtd_to_current_stock_pct"] = _pct(row["total_push_qty_mtd"], row.get("current_stock_qty"))
        row["current_stock_to_push_mtd"] = _pct(row.get("current_stock_qty"), row["total_push_qty_mtd"])
        company.append(row)
    company.sort(key=lambda r: -_f(r.get("total_push_qty_mtd")))
    for ch in CHANNELS:
        audit["sold_mapping_coverage_by_channel"] = audit.get("sold_mapping_coverage_by_channel", {})
        audit["push_mapping_coverage_by_channel"] = audit.get("push_mapping_coverage_by_channel", {})
        audit["sold_mapping_coverage_by_channel"][ch] = _pct(audit["resolved_sold_qty_by_channel"][ch], audit["source_sold_qty_by_channel"][ch])
        audit["push_mapping_coverage_by_channel"][ch] = _pct(audit["resolved_push_qty_by_channel"][ch], audit["source_push_qty_by_channel"][ch])
    audit["unresolved_example_count"] = len(unresolved)
    return by_channel, company, audit, unresolved


def build_top20_v474(package: Mapping[str, Any], alias_master: Mapping[str, Any], bundle_map: Mapping[str, List[Dict[str, Any]]]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    catalog, alias_index, pid_index = build_inventory_catalog(package)
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in package.get("sku_inventory_summary") or []:
        ch = _clean(row.get("channel")).upper()
        if ch in CHANNELS and _f(row.get("sold_qty_mtd")) > 0:
            grouped[ch].append(row)
    out: Dict[str, List[Dict[str, Any]]] = {ch: [] for ch in CHANNELS}
    audit = {"resolved_rows_by_channel": {}, "row_count_by_channel": {}, "mapping_coverage_by_channel": {}}
    inv_snapshot = (package.get("inventory") or {}).get("snapshot_date")
    for ch in CHANNELS:
        rows = sorted(grouped[ch], key=lambda r: (-_f(r.get("sold_qty_mtd")), -_f(r.get("total_push_qty_mtd")), str(r.get("canonical_sku") or r.get("source_skus") or "")))[:TOP_N]
        resolved = 0
        total_sold = sum(_f(r.get("sold_qty_mtd")) for r in grouped[ch])
        for rank, src in enumerate(rows, 1):
            res = resolve_sales_row(src, alias_master, bundle_map, catalog, alias_index, pid_index)
            row = {
                "channel": ch,
                "rank_by_sold_qty": rank,
                "rank_metric": "sold_qty_mtd_excluding_gifts",
                "sku": src.get("canonical_sku") or ((src.get("source_skus") or [None])[0]),
                "source_skus": copy.deepcopy(src.get("source_skus") or []),
                "product_name": src.get("product_name"),
                "sold_qty_day": _f(src.get("sold_qty_day")),
                "sold_qty_mtd": _f(src.get("sold_qty_mtd")),
                "gift_qty_day": _f(src.get("gift_qty_day")),
                "gift_qty_mtd": _f(src.get("gift_qty_mtd")),
                "total_push_qty_day": _f(src.get("total_push_qty_day")),
                "total_push_qty_mtd": _f(src.get("total_push_qty_mtd")),
                "channel_sold_qty_share_pct": _pct(src.get("sold_qty_mtd"), total_sold),
                "inventory_snapshot_date": inv_snapshot,
                "mapping_resolution_status": res.get("status"),
                "mapping_method": res.get("mapping_method"),
                "bundle_components": copy.deepcopy(res.get("components") or []),
                "inventory_key": res.get("inventory_key"),
                "product_id": res.get("product_id"),
                "canonical_sku": res.get("canonical_sku"),
                "primary_ean": res.get("canonical_ean"),
                "current_stock_qty": res.get("current_stock_qty"),
                "stock_hn": res.get("stock_hn"),
                "stock_hcm": res.get("stock_hcm"),
                "stock_hcm_nhan_tem": res.get("stock_hcm_nhan_tem"),
                "stock_hcm_total": res.get("stock_hcm_total"),
                "inventory_lot_count": res.get("lot_count"),
                "earliest_expiry_date": res.get("earliest_expiry_date"),
                "sold_qty_mtd_to_current_stock_pct": _pct(src.get("sold_qty_mtd"), res.get("current_stock_qty")) if res.get("status") == "MAPPED_SINGLE" else None,
                "current_stock_qty_to_sold_qty_mtd": _pct(res.get("current_stock_qty"), src.get("sold_qty_mtd")) if res.get("status") == "MAPPED_SINGLE" else None,
                "ratio_note": "Single-SKU ratios use current snapshot only. Bundle rows expose component stocks and do not invent one combined stock figure.",
            }
            if res.get("status") in {"MAPPED_SINGLE", "MAPPED_IDENTITY_NO_STOCK", "MAPPED_BUNDLE_COMPONENTS"}:
                resolved += 1
            out[ch].append(row)
        audit["resolved_rows_by_channel"][ch] = resolved
        audit["row_count_by_channel"][ch] = len(rows)
        audit["mapping_coverage_by_channel"][ch] = _pct(resolved, len(rows))
    return out, audit


def build_gift_sku_view(package: Mapping[str, Any], alias_master: Mapping[str, Any], bundle_map: Mapping[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    catalog, alias_index, pid_index = build_inventory_catalog(package)
    out: Dict[str, List[Dict[str, Any]]] = {ch: [] for ch in CHANNELS}
    for src in package.get("sku_inventory_summary") or []:
        ch = _clean(src.get("channel")).upper()
        if ch not in CHANNELS or _f(src.get("gift_qty_mtd")) <= 0:
            continue
        res = resolve_sales_row(src, alias_master, bundle_map, catalog, alias_index, pid_index)
        out[ch].append({
            "channel": ch,
            "sku": src.get("canonical_sku") or ((src.get("source_skus") or [None])[0]),
            "source_skus": copy.deepcopy(src.get("source_skus") or []),
            "product_name": src.get("product_name"),
            "gift_qty_day": _f(src.get("gift_qty_day")),
            "gift_qty_mtd": _f(src.get("gift_qty_mtd")),
            "gift_cogs_mtd": _f(src.get("gift_cogs_mtd")),
            "mapping_resolution_status": res.get("status"),
            "primary_ean": res.get("canonical_ean"),
            "product_id": res.get("product_id"),
            "current_stock_qty": res.get("current_stock_qty"),
            "stock_hn": res.get("stock_hn"),
            "stock_hcm": res.get("stock_hcm"),
            "bundle_components": copy.deepcopy(res.get("components") or []),
        })
    for ch in CHANNELS:
        out[ch].sort(key=lambda r: (-_f(r.get("gift_qty_mtd")), -_f(r.get("gift_cogs_mtd")), str(r.get("sku") or "")))
    return out


def _shopee_gift_audit(package: Mapping[str, Any]) -> Dict[str, Any]:
    rows = [r for r in package.get("sku_inventory_summary") or [] if _clean(r.get("channel")).upper() == "SHOPEE"]
    sold_qty = sum(_f(r.get("sold_qty_mtd")) for r in rows)
    gift_qty = sum(_f(r.get("gift_qty_mtd")) for r in rows)
    sold_cogs = sum(_f(r.get("sold_cogs_mtd")) for r in rows)
    gift_cogs = sum(_f(r.get("gift_cogs_mtd")) for r in rows)
    return {
        "classification_source": "shopee.order_items.is_gift from ETL; v4.7.4 ETL patch enriches classification rule in raw_payload",
        "sold_qty_mtd": sold_qty,
        "gift_qty_mtd": gift_qty,
        "sold_cogs_mtd": sold_cogs,
        "gift_cogs_mtd": gift_cogs,
        "total_cogs_mtd": sold_cogs + gift_cogs,
        "gift_sku_rows": sum(1 for r in rows if _f(r.get("gift_qty_mtd")) > 0),
        "note": "A normal sellable SKU used as a promotion gift is only reclassified when source evidence or an approved promotion rule exists. No SKU-name guessing is introduced.",
    }


def _append_warning(package: MutableMapping[str, Any], text: str) -> None:
    dq = package.setdefault("data_quality", {})
    warnings = dq.setdefault("warnings", [])
    if text not in warnings:
        warnings.append(text)


def _append_error(package: MutableMapping[str, Any], text: str) -> None:
    dq = package.setdefault("data_quality", {})
    errors = dq.setdefault("errors", [])
    if text not in errors:
        errors.append(text)


def _refresh_dq(package: MutableMapping[str, Any]) -> None:
    dq = package.setdefault("data_quality", {})
    errors = dq.get("errors") or []
    warnings = dq.get("warnings") or []
    if errors:
        dq["status"] = "ERROR"; dq["can_send"] = False
    elif warnings:
        dq["status"] = "WARNING"; dq["can_send"] = True
    else:
        dq["status"] = "PASS"; dq["can_send"] = True


def postprocess_v474(
    package: MutableMapping[str, Any], alias_file: Path, bundle_file: Path, require_top20_mapping: bool = True
) -> None:
    alias_master = load_alias_master(alias_file)
    bundle_map = load_bundle_map(bundle_file)
    push_by_channel, company_push, mapping_audit, unresolved = build_inventory_push(package, alias_master, bundle_map)
    top20, top20_audit = build_top20_v474(package, alias_master, bundle_map)
    gift_view = build_gift_sku_view(package, alias_master, bundle_map)
    gift_audit = _shopee_gift_audit(package)

    package["sku_alias_master_v4_7_4"] = {
        "available": True,
        "source_file": str(alias_file),
        "approved_mapping_count": alias_master["approved_count"],
        "mapping_rule": "Exact approved short SKU -> exact EAN; exact EAN accepted directly; no truncation/fuzzy matching.",
    }
    package["inventory_push_by_channel"] = push_by_channel
    package["company_inventory_push_by_product"] = company_push
    package["inventory_push_mapping_audit_v4_7_4"] = mapping_audit
    package["unmapped_sales_skus_v4_7_4"] = unresolved
    package["top_20_sku_by_channel_v4_7_4"] = top20
    package["top20_mapping_audit_v4_7_4"] = top20_audit
    package["gift_sku_mtd_by_channel"] = gift_view
    package["shopee_gift_classification_audit_v4_7_4"] = gift_audit

    meta = package.setdefault("metadata", {})
    meta["package_version"] = PACKAGE_VERSION
    meta["package_version_int"] = PACKAGE_VERSION_INT
    meta["upgrade_scope_v4_7_4"] = "Approved short-SKU/EAN product identity + bundle component push mapping + sold/gift/total-push inventory visibility + Shopee gift classification audit"
    meta["alias_master_approved_count"] = alias_master["approved_count"]

    contract = package.setdefault("excel_render_contract", {})
    contract["top20_sku_source_v4_7_4"] = "top_20_sku_by_channel_v4_7_4"
    contract["gift_sku_source"] = "gift_sku_mtd_by_channel"
    contract["inventory_push_source"] = "inventory_push_by_channel"
    contract["company_inventory_push_source"] = "company_inventory_push_by_product"
    contract["mapping_audit_source"] = "top20_mapping_audit_v4_7_4 / inventory_push_mapping_audit_v4_7_4"
    contract["inventory_push_formula"] = "Normal item: sold_qty + gift_qty. Bundle: explicit component quantity x source total_push; sold/gift component role remains unallocated unless approved role mapping exists."
    contract["stock_semantics"] = "current_stock_qty is actual latest snapshot <= report_date; never opening_stock - push"

    policy = package.setdefault("report_rendering_policy", {})
    policy["product_identity_v4_7_4"] = {
        "identity": "one product_id may have exact EAN and multiple short/source SKUs",
        "resolver_order": ["explicit bundle components", "approved short SKU -> EAN", "exact EAN", "exact inventory identifier", "product_id fallback"],
        "forbidden": ["truncate EAN to guess short SKU", "fuzzy product-name auto-map", "invent stock for unmapped SKU"],
    }

    if alias_master["approved_count"] <= 0:
        _append_error(package, "v4.7.4 alias master has no approved mappings.")
    for ch in CHANNELS:
        cov = top20_audit["mapping_coverage_by_channel"].get(ch)
        if cov is not None and cov < 0.999999:
            text = f"v4.7.4 Top20 {ch} mapping coverage is {cov:.1%}; unresolved Top20 rows remain."
            if require_top20_mapping:
                _append_error(package, text)
            else:
                _append_warning(package, text)
    if unresolved:
        _append_warning(package, f"v4.7.4 still has {len(unresolved)} unresolved sales SKU summary rows outside/including Top20; see unmapped_sales_skus_v4_7_4.")
    if gift_audit["gift_qty_mtd"] <= 0:
        _append_warning(package, "v4.7.4 Shopee gift_qty_mtd is zero; verify ETL gift classifier/source promotion evidence before interpreting gift performance.")

    dq = package.setdefault("data_quality", {})
    dq["v4_7_4_scope"] = {
        "alias_master_approved_count": alias_master["approved_count"],
        "top20_mapping_coverage_by_channel": copy.deepcopy(top20_audit["mapping_coverage_by_channel"]),
        "push_mapping_coverage_by_channel": copy.deepcopy(mapping_audit.get("push_mapping_coverage_by_channel") or {}),
        "unresolved_sales_sku_rows": len(unresolved),
        "shopee_gift_qty_mtd": gift_audit["gift_qty_mtd"],
        "shopee_gift_cogs_mtd": gift_audit["gift_cogs_mtd"],
    }
    _refresh_dq(package)


def build_package_v474(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    alias_file: Path,
    bundle_file: Path,
    include_growth_context: bool = True,
    require_targets: bool = True,
    require_inventory: bool = True,
    require_top20_mapping: bool = True,
):
    v473 = _load_v473()
    base, package = v473.build_package_v473(
        report_date_str,
        compare_mode,
        explicit_compare_date,
        vat_rates,
        include_growth_context=include_growth_context,
        require_targets=require_targets,
        require_inventory=require_inventory,
    )
    postprocess_v474(package, alias_file, bundle_file, require_top20_mapping=require_top20_mapping)
    base.PACKAGE_VERSION = PACKAGE_VERSION
    base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
    return base, base.clean(package)


def enrich_existing_package(source_package: Path, alias_file: Path, bundle_file: Path, require_top20_mapping: bool = True) -> Dict[str, Any]:
    package = json.loads(source_package.read_text(encoding="utf-8"))
    if str((package.get("metadata") or {}).get("package_version")) != "v4.7.3":
        raise ValueError("--source-package must be a v4.7.3 package")
    postprocess_v474(package, alias_file, bundle_file, require_top20_mapping=require_top20_mapping)
    return package


def self_test() -> int:
    failures: List[str] = []
    alias = {
        "short_to_ean": {_norm("ST00526"): "3574661700526", _norm("ST01172"): "3574661701172"},
        "ean_to_short": {_norm("3574661700526"): "ST00526", _norm("3574661701172"): "ST01172"},
    }
    catalog = {
        "P1": {"inventory_key": "P1", "product_id": "P1", "primary_ean": "3574661700526", "canonical_sku": "3574661700526", "current_stock_qty": 100},
        "P2": {"inventory_key": "P2", "product_id": "P2", "primary_ean": "3574661701172", "canonical_sku": "3574661701172", "current_stock_qty": 200},
    }
    idx = defaultdict(set)
    idx[_norm("3574661700526")].add("P1"); idx[_norm("3574661701172")].add("P2")
    r1 = _scalar_resolution("ST00526", alias, idx)
    r2 = _scalar_resolution("3574661700526", alias, idx)
    if r1.get("inventory_key") != "P1" or r2.get("inventory_key") != "P1":
        failures.append("short SKU and EAN do not converge to same inventory key")
    c = parse_composite_sku("ST00526 + ST01172", {})
    if not c or len(c) != 2 or c[0]["qty_per_bundle"] != 1:
        failures.append("plus composite parser")
    c2 = parse_composite_sku("ST01172 x 2", {})
    if not c2 or len(c2) != 1 or c2[0]["qty_per_bundle"] != 2:
        failures.append("xN composite parser")
    try:
        # In-memory conflict shape check mirrors file loader contract.
        d = {_norm("ST00526"): "3574661700526"}
        if d.get(_norm("ST00526")) == "3574661700991":
            failures.append("conflict fixture")
    except Exception:
        failures.append("conflict fixture exception")
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for item in failures:
            print("-", item, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Short SKU -> EAN -> inventory identity: PASS")
    print("Direct EAN identity: PASS")
    print("Explicit + bundle parser: PASS")
    print("Explicit xN bundle parser: PASS")
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
    ap.add_argument("--allow-unmapped-top20", action="store_true")
    ap.add_argument("--sku-map", default=None)
    ap.add_argument("--bundle-map", default=None)
    ap.add_argument("--source-package", help="Offline: enrich an existing v4.7.3 JSON instead of querying DB")
    ap.add_argument("--out-dir", default="outbox/packages")
    ap.add_argument("--store-db", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    alias_file = _resolve_config_path(args.sku_map, DEFAULT_ALIAS_FILE)
    bundle_file = _resolve_config_path(args.bundle_map, DEFAULT_BUNDLE_FILE)
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = _repo_root() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.source_package:
        src = Path(args.source_package)
        package = enrich_existing_package(src, alias_file, bundle_file, require_top20_mapping=not args.allow_unmapped_top20)
        report_date = str((package.get("metadata") or {}).get("report_date"))
        json_path = out_dir / f"ceo_daily_pnl_package_v4_7_4_{report_date}.json"
        json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON package: {json_path}")
        print(f"Package version: {PACKAGE_VERSION}")
        print(f"Data quality: {(package.get('data_quality') or {}).get('status')}")
        return 0 if (package.get("data_quality") or {}).get("can_send") else 1

    if not args.date:
        ap.error("--date is required unless --source-package or --self-test is used")
    v473 = _load_v473()
    v472 = v473._load_v472(); v471 = v472._load_v471(); v47 = v471._load_v47(); v461 = v47._load_v461(); v46 = v461._load_v46(); v45 = v46._load_base()
    try:
        report_date = v45.parse_date(args.date).isoformat()
        if args.compare_date:
            v45.parse_date(args.compare_date)
        vat_rates = v45.parse_vat_rates(args.vat_rates)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 2
    if args.require_vat_rates:
        missing = [ch for ch in v45.DAILY_PNL_SHEETS if ch not in vat_rates]
        if missing:
            print("ERROR: missing VAT rates for " + ", ".join(missing), file=sys.stderr); return 2

    base, package = build_package_v474(
        report_date,
        args.compare_mode,
        args.compare_date,
        vat_rates,
        alias_file,
        bundle_file,
        include_growth_context=not args.without_growth_context,
        require_targets=not args.allow_missing_targets,
        require_inventory=not args.allow_missing_inventory,
        require_top20_mapping=not args.allow_unmapped_top20,
    )
    json_path = out_dir / f"ceo_daily_pnl_package_v4_7_4_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_7_4_{report_date}.md"
    json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=base.json_default), encoding="utf-8")
    md = base.markdown_summary(package)
    md += "\n\n## v4.7.4 Product Mapping + Gift + Inventory Push\n"
    md += f"- Approved alias mappings: {package['sku_alias_master_v4_7_4']['approved_mapping_count']}\n"
    md += "- Product identity: exact short SKU -> exact EAN -> one inventory product.\n"
    md += "- Inventory push: sold + gift; explicit bundle components are expanded for push quantity.\n"
    md += "- Current stock remains actual snapshot; no opening-stock inference.\n"
    md_path.write_text(md, encoding="utf-8")
    if args.store_db:
        base.PACKAGE_VERSION = PACKAGE_VERSION; base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
        base.store_best_effort(report_date, package, md, str(json_path), str(md_path))
    dq = package.get("data_quality") or {}
    print(f"JSON package: {json_path}")
    print(f"Markdown summary: {md_path}")
    print(f"Package version: {PACKAGE_VERSION}")
    print(f"Alias mappings: {package['sku_alias_master_v4_7_4']['approved_mapping_count']}")
    print(f"Top20 mapping: {(package.get('top20_mapping_audit_v4_7_4') or {}).get('mapping_coverage_by_channel')}")
    print(f"Data quality: {dq.get('status')}")
    for warning in dq.get("warnings") or []: print(f"WARNING: {warning}")
    for error in dq.get("errors") or []: print(f"ERROR: {error}")
    return 0 if dq.get("can_send") else 1


if __name__ == "__main__":
    raise SystemExit(main())
