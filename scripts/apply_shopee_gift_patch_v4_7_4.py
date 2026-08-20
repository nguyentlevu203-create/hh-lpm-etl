#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch current GitHub-main scripts/shopee_etl.py for v4.7.4 gift classification.

The patch is deliberately narrow and fails closed if the expected current source
snippets are not present. Use --dry-run first. A .pre_v4_7_4 backup is created.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

EXPECTED_GIT_BLOB_SHA = "7da3c8b9ea9392a18c1d181899c43b893ea837d0"


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Patch target {label!r} expected exactly once, found {count}")
    return text.replace(old, new, 1)


def patched_text(text: str) -> str:
    text = replace_once(
        text,
        'SHOPEE_ETL_VERSION = "v4.11.3_windows_locked_file_archive_retry"',
        'SHOPEE_ETL_VERSION = "v4.12.0_gift_classifier_v4_7_4"',
        "version",
    )
    text = replace_once(
        text,
        'SHOPEE_ETL_VERSION = "v4.12.0_gift_classifier_v4_7_4"',
        'from shopee_gift_classifier_v4_7_4 import classify_shopee_group, enrich_gift_raw_payload, load_promotion_rules\n\nSHOPEE_ETL_VERSION = "v4.12.0_gift_classifier_v4_7_4"',
        "classifier import",
    )
    text = replace_once(
        text,
        'def process_order_files(aff_map: Dict[str, float], pay_map: Dict[str, float]) -> Tuple[List[dict], List[ProcessedFile]]:\n    order_map: Dict[Tuple[str, str], dict] = {}\n    processed: List[ProcessedFile] = []',
        'def process_order_files(aff_map: Dict[str, float], pay_map: Dict[str, float]) -> Tuple[List[dict], List[ProcessedFile]]:\n    order_map: Dict[Tuple[str, str], dict] = {}\n    processed: List[ProcessedFile] = []\n    promotion_rules = load_promotion_rules(os.getenv("SHOPEE_GIFT_RULES_FILE", "config/shopee_promotion_gift_rules_v4_7_4.csv"))',
        "promotion rule loader",
    )
    text = replace_once(
        text,
        '            main_product_mask = ~group.apply(lambda row: is_shopee_gift_row(row.get(c_name, ""), row.get(c_price, 0)), axis=1)',
        '            gift_decisions = classify_shopee_group(\n                group.to_dict("records"),\n                sku_key=c_sku,\n                name_key=c_name,\n                original_price_key=c_price,\n                qty_key=c_qty,\n                order_date_key=c_dt,\n                order_id=oid_clean,\n                shop_label=shop_label,\n                promotion_rules=promotion_rules,\n            )\n            main_product_mask = pd.Series([not bool(d.get("is_gift")) for d in gift_decisions], index=group.index)',
        "main product gift mask",
    )
    text = replace_once(
        text,
        '            items = []\n            for _, item in group.iterrows():',
        '            items = []\n            for item_pos, (_, item) in enumerate(group.iterrows()):\n                gift_decision = gift_decisions[item_pos]',
        "item loop decision",
    )
    text = replace_once(
        text,
        '                    "qty": qty,\n                    "is_gift": is_shopee_gift_row(item.get(c_name, ""), item.get(c_price, 0)),\n                    "raw": row_to_json(item),',
        '                    "qty": qty,\n                    "original_price": safe_num(item.get(c_price, 0)),\n                    "effective_price": gift_decision.get("effective_price"),\n                    "is_gift": bool(gift_decision.get("is_gift", False)),\n                    "gift_classification_rule": gift_decision.get("rule"),\n                    "gift_review_required": bool(gift_decision.get("review_required", False)),\n                    "raw": enrich_gift_raw_payload(row_to_json(item), gift_decision),',
        "item classification payload",
    )
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="scripts/shopee_etl.py")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-sha", action="store_true", help="Allow patch when Git blob SHA differs, but snippet guards still apply")
    args = ap.parse_args()
    path = Path(args.file)
    data = path.read_bytes()
    sha = git_blob_sha1(data)
    if sha != EXPECTED_GIT_BLOB_SHA and not args.force_sha:
        raise RuntimeError(f"Unexpected shopee_etl.py Git blob SHA {sha}; expected {EXPECTED_GIT_BLOB_SHA}. Re-audit before patching or use --force-sha only after code review.")
    text = data.decode("utf-8")
    out = patched_text(text)
    compile(out, str(path), "exec")
    print(f"Input Git blob SHA: {sha}")
    print("Patch structure: PASS")
    if args.dry_run:
        print("DRY RUN: no file changed")
        return 0
    backup = path.with_name(path.name + ".pre_v4_7_4")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(out, encoding="utf-8")
    print(f"Patched: {path}")
    print(f"Backup:  {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
