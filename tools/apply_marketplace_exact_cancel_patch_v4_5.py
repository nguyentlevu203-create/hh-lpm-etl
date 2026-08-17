#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch marketplace ETLs for CEO Daily P&L v4.5.

Owner-approved contract
-----------------------
1. An order is cancelled ONLY when its normalized status is exactly "Đã hủy".
   Normalization is limited to Unicode NFC, whitespace trim/collapse and
   case-insensitive comparison. No substring/fuzzy English/Vietnamese matching.
2. Every other status is treated as normal/non-cancelled. Return/refund order
   classification is intentionally disabled.
3. Shopee Gross Sales stores main-product Gross for every status, including
   exact-cancelled orders. Gift rows remain excluded from Shopee Gross.
4. This patch DOES NOT change voucher storage. The existing Shopee DB column
   order_financials_extra.platform_voucher is a legacy/misleading name whose
   raw source is "Mã giảm giá của Shop"; the v4.5 package builder maps it to
   seller_voucher. True platform-funded vouchers are ignored by the package.

The patcher is fail-fast, idempotent, creates backups, and validates Python
syntax before writing.
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
from pathlib import Path

CANCELLED_STATUS_EXACT = "đã hủy"
BACKUP_SUFFIX = ".bak_v4_5_exact_cancel"

CANCEL_HELPER = '''\nCANCELLED_STATUS_EXACT = "đã hủy"\n\ndef normalize_marketplace_status(value: object) -> str:\n    """Normalize harmless presentation noise for exact status comparison."""\n    text = unicodedata.normalize("NFC", safe_text(value, 200))\n    return re.sub(r"\\s+", " ", text).strip().casefold()\n\ndef is_exact_cancelled_status(value: object) -> bool:\n    return normalize_marketplace_status(value) == CANCELLED_STATUS_EXACT\n\n'''


def ensure_import(text: str) -> str:
    if re.search(r"^import unicodedata$", text, flags=re.M):
        return text
    m = re.search(r"^import re\s*$", text, flags=re.M)
    if not m:
        raise RuntimeError("Cannot find 'import re' to insert unicodedata import")
    return text[: m.end()] + "\nimport unicodedata" + text[m.end() :]


def ensure_helper(text: str, anchor: str) -> str:
    if "def is_exact_cancelled_status(" in text:
        return text
    idx = text.find(anchor)
    if idx < 0:
        raise RuntimeError(f"Cannot find helper insertion anchor: {anchor!r}")
    return text[:idx] + CANCEL_HELPER + text[idx:]


def patch_shopee(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    text = ensure_import(text)
    text = ensure_helper(text, "def split_shopee_combo_sku(")

    old_regex = re.compile(
        r'df\["is_cancelled_row"\]\s*=\s*df\[c_st\]\.astype\(str\)\.str\.contains\([^\n]+\)'
    )
    exact_line = 'df["is_cancelled_row"] = df[c_st].map(is_exact_cancelled_status)'
    if old_regex.search(text):
        text, n = old_regex.subn(exact_line, text, count=1)
        notes.append(f"Shopee cancellation classifier changed to exact 'Đã hủy' ({n})")
    elif exact_line in text:
        notes.append("Shopee cancellation classifier already exact")
    else:
        raise RuntimeError("Shopee cancellation classifier layout is unknown; refusing unsafe patch")

    old_gross = "gross_sales = 0.0 if is_cancelled else total_gross_main_products"
    new_gross = "gross_sales = total_gross_main_products"
    if old_gross in text:
        text = text.replace(old_gross, new_gross, 1)
        notes.append("Shopee Gross changed to all-status main-product Gross")
    elif new_gross in text:
        notes.append("Shopee Gross already all-status")
    else:
        raise RuntimeError("Shopee gross_sales assignment layout is unknown; refusing unsafe patch")

    text = text.replace(
        "gross_sales_main_products is stored separately in shopee.orders.gross_sales.",
        "gross_sales_main_products is stored for every status in shopee.orders.gross_sales.",
    )
    if re.search(r"^SHOPEE_ETL_VERSION\s*=", text, flags=re.M):
        text = re.sub(
            r"^SHOPEE_ETL_VERSION\s*=.*$",
            'SHOPEE_ETL_VERSION = "v4.50_exact_cancel_gross_all_status"',
            text,
            count=1,
            flags=re.M,
        )
    return text, notes


def patch_tiktok(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    text = ensure_import(text)
    text = ensure_helper(text, "def get_tiktok_platform_fee_rate(")

    old_regex = re.compile(
        r'df_o\["is_cancelled"\]\s*=\s*df_o\["order_status"\]\.str\.contains\([^\n]+\)'
    )
    exact_line = 'df_o["is_cancelled"] = df_o["order_status"].map(is_exact_cancelled_status)'
    if old_regex.search(text):
        text, n = old_regex.subn(exact_line, text, count=1)
        notes.append(f"TikTok cancellation classifier changed to exact 'Đã hủy' ({n})")
    elif exact_line in text:
        notes.append("TikTok cancellation classifier already exact")
    else:
        raise RuntimeError("TikTok cancellation classifier layout is unknown; refusing unsafe patch")

    # Existing fee/COGS/packaging/cancel-shipping rules remain unchanged.
    # Only the boolean feeding those rules changes to the exact owner-approved status.
    return text, notes


def syntax_check(text: str, path: Path) -> None:
    try:
        ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(f"Patched {path} has syntax error: {exc}") from exc


def write_patch(path: Path, patch_fn, apply: bool) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(path)
    original = path.read_text(encoding="utf-8")
    patched, notes = patch_fn(original)
    syntax_check(patched, path)
    if patched == original:
        notes.append("No file changes required")
        return notes
    if not apply:
        notes.append("CHECK ONLY: changes required but not written")
        return notes
    backup = Path(str(path) + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(patched, encoding="utf-8")
    notes += [f"Written: {path}", f"Backup:  {backup}"]
    return notes


def self_test() -> int:
    sample_shopee = '''import re\n\ndef safe_text(value, max_len=200): return str(value)\ndef split_shopee_combo_sku(x): return []\ndf["is_cancelled_row"] = df[c_st].astype(str).str.contains("đã hủy|da huy|cancel|cancelled", case=False, na=False)\ngross_sales = 0.0 if is_cancelled else total_gross_main_products\n'''
    sample_tiktok = '''import re\n\ndef safe_text(value, max_len=200): return str(value)\ndef get_tiktok_platform_fee_rate(x): return 0\ndf_o["is_cancelled"] = df_o["order_status"].str.contains("hủy|huỷ|cancel", case=False, na=False)\n'''
    sh, _ = patch_shopee(sample_shopee)
    tt, _ = patch_tiktok(sample_tiktok)
    failures = []
    if 'df["is_cancelled_row"] = df[c_st].map(is_exact_cancelled_status)' not in sh:
        failures.append("Shopee exact classifier missing")
    if "gross_sales = total_gross_main_products" not in sh:
        failures.append("Shopee all-status Gross missing")
    if 'df_o["is_cancelled"] = df_o["order_status"].map(is_exact_cancelled_status)' not in tt:
        failures.append("TikTok exact classifier missing")

    import unicodedata
    def norm(v: object) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(v or ""))).strip().casefold()
    cases = {
        "Đã hủy": True,
        " đã hủy ": True,
        "ĐÃ HỦY": True,
        "Đã hủy bởi Người mua": False,
        "Cancelled": False,
        "Trả hàng/Hoàn tiền": False,
        "Hoàn thành": False,
    }
    for raw, expected in cases.items():
        got = norm(raw) == CANCELLED_STATUS_EXACT
        if got != expected:
            failures.append(f"status {raw!r}: expected {expected}, got {got}")
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for x in failures:
            print("-", x, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Only exact normalized 'Đã hủy' is cancelled; all other statuses are normal.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--apply", action="store_true", help="Write changes. Default is check-only.")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    root = Path(args.repo_root).resolve()
    targets = [
        (root / "scripts" / "shopee_etl.py", patch_shopee),
        (root / "scripts" / "tiktok_etl.py", patch_tiktok),
    ]
    print("Rule: ONLY exact normalized status 'Đã hủy' is cancelled")
    print("Returns/refunds: disabled; every other status is normal")
    print("Platform-funded voucher: package excludes it; ETL compatibility column is not renamed here")
    for path, fn in targets:
        print(f"\n[{path.name}]")
        for note in write_patch(path, fn, args.apply):
            print("-", note)
    if not args.apply:
        print("\nNo files changed. Re-run with --apply after reviewing the check result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
