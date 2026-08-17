#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply the Shopee Gross-All-Status patch to scripts/shopee_etl.py.

The patch preserves the existing Net Sales / payout / fee logic and changes only:
1. gross_sales is kept for cancelled orders instead of being forced to zero;
2. the original item price is carried in the in-memory item payload;
3. shopee.order_items.price stores that original item price instead of 0.

Run once from the repository root:
    python tools/apply_shopee_gross_all_status_patch_v4_1.py

Then copy/reload the relevant Shopee order export files and rerun Shopee ETL.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scripts" / "shopee_etl.py"
BACKUP = ROOT / "scripts" / "shopee_etl.py.bak_v4_1_gross_all_status"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly 1 match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    if not TARGET.exists():
        print(f"ERROR: file not found: {TARGET}", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")
    if "SHOPEE_GROSS_ALL_STATUS_V4_1" in text:
        print("Patch already applied; no changes made.")
        return 0

    original = text
    text = replace_once(
        text,
        '            gross_sales = 0.0 if is_cancelled else total_gross_main_products\n',
        '            # SHOPEE_GROSS_ALL_STATUS_V4_1\n'
        '            # Gross Sales is the total main-product order value across every status,\n'
        '            # including cancelled and returned/refunded orders. Net Sales remains\n'
        '            # seller_revenue and is still zero for cancelled orders.\n'
        '            gross_sales = total_gross_main_products\n',
        "gross_sales assignment",
    )

    text = replace_once(
        text,
        '                    "qty": qty,\n                    "is_gift": is_shopee_gift_row(item.get(c_name, ""), item.get(c_price, 0)),\n',
        '                    "qty": qty,\n'
        '                    "price": safe_num(item.get(c_price, 0)),\n'
        '                    "is_gift": is_shopee_gift_row(item.get(c_name, ""), item.get(c_price, 0)),\n',
        "item price payload",
    )

    text = replace_once(
        text,
        '                        int(item.get("qty", 0)), 0.0, bool(item.get("is_gift", False)),\n',
        '                        int(item.get("qty", 0)), safe_num(item.get("price", 0.0)), bool(item.get("is_gift", False)),\n',
        "order_items.price load",
    )

    if 'SHOPEE_ETL_VERSION = "' in text:
        old_line = next(line for line in text.splitlines() if line.startswith('SHOPEE_ETL_VERSION = "'))
        version = old_line.split('"', 2)[1]
        new_line = f'SHOPEE_ETL_VERSION = "{version}_gross_all_status_v4_1"'
        text = text.replace(old_line, new_line, 1)

    if text == original:
        raise RuntimeError("No changes were produced")

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
    TARGET.write_text(text, encoding="utf-8")

    print(f"Patched: {TARGET}")
    print(f"Backup:  {BACKUP}")
    print("Next: reload Shopee order files and run python scripts\\shopee_etl.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
