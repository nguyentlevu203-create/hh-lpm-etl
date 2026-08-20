#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic Shopee item gift classifier for v4.7.4.

This module does not guess that a normal sellable SKU is always a gift.
Classification priority:
1) explicit zero effective-price signal from known source columns;
2) original price <= 0;
3) explicit gift/accessory wording in product name;
4) approved promotion rule with order/group context;
5) otherwise SOLD.

If an approved promotion rule can only explain part of a SKU row quantity, the
row is marked REVIEW_REQUIRED rather than silently splitting/guessing.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

EFFECTIVE_PRICE_HEADERS = (
    "Giá sau khi giảm",
    "Giá sau giảm",
    "Giá ưu đãi",
    "Giá bán",
    "Đơn giá sau khuyến mãi",
    "Giá sản phẩm sau khi giảm",
)
GIFT_NAME_KEYWORDS = (
    "quà tặng", "qua tang", "gift",
    "bông tắm", "bong tam",
    "túi", "tui",
    "lược", "luoc",
    "kệ", "ke trung bay",
    "găng tay", "gang tay",
)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", " ").replace("\u200b", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _norm(value: Any) -> str:
    return unicodedata.normalize("NFC", _clean(value)).casefold()


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            x = float(value)
            return None if math.isnan(x) or math.isinf(x) else x
        except Exception:
            return None
    text = _clean(value)
    if not text:
        return None
    text = text.replace("₫", "").replace("đ", "").replace(" ", "")
    # VN exports commonly use thousand separators. Keep the final decimal point only
    # when there is clearly one; otherwise remove separators.
    if re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", text):
        text = text.replace(".", "").replace(",", "")
    else:
        text = text.replace(",", "")
    try:
        return float(text)
    except Exception:
        return None


def _date(value: Any) -> Optional[dt.date]:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return dt.datetime.strptime(text[:19], fmt).date()
        except Exception:
            pass
    try:
        return dt.date.fromisoformat(text[:10])
    except Exception:
        return None


def _find_value(row: Mapping[str, Any], header: str) -> Any:
    target = _norm(header)
    for key, value in row.items():
        if _norm(key) == target:
            return value
    return None


def load_promotion_rules(path: str | Path | None) -> List[Dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            if _clean(raw.get("active")).lower() not in {"1", "true", "yes", "y"}:
                continue
            trigger = _clean(raw.get("trigger_sku"))
            gift = _clean(raw.get("gift_sku"))
            order_id = _clean(raw.get("order_id"))
            if not gift:
                continue
            qty = _num(raw.get("gift_qty_per_trigger")) or 1.0
            out.append({
                "rule_id": _clean(raw.get("rule_id")) or f"RULE_{len(out)+1}",
                "start_date": _date(raw.get("start_date")),
                "end_date": _date(raw.get("end_date")),
                "shop_label": _clean(raw.get("shop_label")),
                "trigger_sku": trigger,
                "gift_sku": gift,
                "gift_qty_per_trigger": qty,
                "order_id": order_id,
                "note": _clean(raw.get("note")),
            })
    return out


def _direct_decision(row: Mapping[str, Any], *, sku_key: str, name_key: str, original_price_key: str) -> Dict[str, Any]:
    sku = _clean(row.get(sku_key))
    name = _clean(row.get(name_key))
    original = _num(row.get(original_price_key))
    effective = None
    effective_header = None
    for header in EFFECTIVE_PRICE_HEADERS:
        value = _find_value(row, header)
        if value is None:
            continue
        parsed = _num(value)
        if parsed is not None:
            effective = parsed
            effective_header = header
            break
    if effective is not None and effective <= 0:
        return {"is_gift": True, "rule": f"SOURCE_EFFECTIVE_PRICE_ZERO:{effective_header}", "review_required": False, "effective_price": effective, "original_price": original, "sku": sku}
    if original is not None and original <= 0:
        return {"is_gift": True, "rule": "SOURCE_ORIGINAL_PRICE_ZERO", "review_required": False, "effective_price": effective, "original_price": original, "sku": sku}
    low = name.casefold()
    if any(k in low for k in GIFT_NAME_KEYWORDS):
        return {"is_gift": True, "rule": "EXPLICIT_GIFT_NAME", "review_required": False, "effective_price": effective, "original_price": original, "sku": sku}
    return {"is_gift": False, "rule": "DEFAULT_SOLD", "review_required": False, "effective_price": effective, "original_price": original, "sku": sku}


def classify_shopee_group(
    rows: Sequence[Mapping[str, Any]],
    *,
    sku_key: str,
    name_key: str,
    original_price_key: str,
    qty_key: str,
    order_date_key: str,
    order_id: str,
    shop_label: str,
    promotion_rules: Sequence[Mapping[str, Any]] = (),
) -> List[Dict[str, Any]]:
    decisions = [_direct_decision(row, sku_key=sku_key, name_key=name_key, original_price_key=original_price_key) for row in rows]
    order_date = _date(rows[0].get(order_date_key)) if rows else None
    oid = _clean(order_id)
    shop = _norm(shop_label)
    qty_by_sku: Dict[str, float] = {}
    for row in rows:
        sku = _norm(row.get(sku_key))
        qty_by_sku[sku] = qty_by_sku.get(sku, 0.0) + max(_num(row.get(qty_key)) or 0.0, 0.0)

    for rule in promotion_rules:
        rid = _clean(rule.get("rule_id"))
        start = rule.get("start_date"); end = rule.get("end_date")
        if order_date is not None:
            if start and order_date < start: continue
            if end and order_date > end: continue
        rshop = _norm(rule.get("shop_label"))
        if rshop and rshop != shop: continue
        roid = _clean(rule.get("order_id"))
        if roid and roid != oid: continue
        gift_sku = _norm(rule.get("gift_sku")); trigger_sku = _norm(rule.get("trigger_sku"))
        if not gift_sku or gift_sku not in qty_by_sku: continue
        if roid:
            entitlement = qty_by_sku.get(gift_sku, 0.0)
        else:
            if not trigger_sku or trigger_sku not in qty_by_sku: continue
            entitlement = qty_by_sku[trigger_sku] * float(rule.get("gift_qty_per_trigger") or 1.0)
        gift_total = qty_by_sku.get(gift_sku, 0.0)
        for pos, row in enumerate(rows):
            if _norm(row.get(sku_key)) != gift_sku: continue
            if decisions[pos]["is_gift"]:
                continue
            row_qty = max(_num(row.get(qty_key)) or 0.0, 0.0)
            if gift_total <= entitlement + 1e-9:
                decisions[pos].update({"is_gift": True, "rule": f"APPROVED_PROMOTION_RULE:{rid}", "review_required": False})
            else:
                decisions[pos].update({"is_gift": False, "rule": f"PROMOTION_PARTIAL_QTY_REVIEW:{rid}", "review_required": True})
    return decisions


def enrich_gift_raw_payload(raw_payload: Any, decision: Mapping[str, Any]) -> Any:
    if isinstance(raw_payload, dict):
        out = dict(raw_payload)
    else:
        out = {"source_raw_payload": raw_payload}
    out["gift_classification_rule"] = decision.get("rule")
    out["gift_review_required"] = bool(decision.get("review_required"))
    out["source_original_price"] = decision.get("original_price")
    out["source_effective_price"] = decision.get("effective_price")
    return out


def self_test() -> int:
    rows = [
        {"sku": "A", "name": "SP A", "price": 100, "qty": 1, "date": "2026-08-18"},
        {"sku": "B", "name": "SP B", "price": 90, "qty": 1, "date": "2026-08-18"},
    ]
    rules = [{"rule_id":"R1","start_date":dt.date(2026,8,1),"end_date":dt.date(2026,8,31),"shop_label":"SHOPMALL","trigger_sku":"A","gift_sku":"B","gift_qty_per_trigger":1,"order_id":""}]
    d = classify_shopee_group(rows, sku_key="sku", name_key="name", original_price_key="price", qty_key="qty", order_date_key="date", order_id="O1", shop_label="SHOPMALL", promotion_rules=rules)
    if d[0]["is_gift"] or not d[1]["is_gift"] or "APPROVED_PROMOTION_RULE" not in d[1]["rule"]:
        print("SELF-TEST FAILED", file=sys.stderr); return 1
    z = classify_shopee_group([{"sku":"G","name":"Quà tặng bông tắm","price":100,"qty":1,"date":"2026-08-18"}], sku_key="sku", name_key="name", original_price_key="price", qty_key="qty", order_date_key="date", order_id="O2", shop_label="SHOPMALL")
    if not z[0]["is_gift"]:
        print("SELF-TEST FAILED", file=sys.stderr); return 1
    print("SELF-TEST PASSED")
    print("Approved promotion group rule: PASS")
    print("Explicit gift-name rule: PASS")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(self_test())
