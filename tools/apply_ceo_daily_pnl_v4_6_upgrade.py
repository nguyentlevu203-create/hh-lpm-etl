#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply the v4.6 ETL status-contract upgrade to an existing hh-lpm-etl repo.

Default is CHECK-ONLY.  Use --apply to write changes.
Backups are created once with suffix .bak_v4_6_status_contract.

This script intentionally changes only the channel ETL classifiers required by
v4.6.  The v4.6 builder/validator are separate files and leave v4.5 available
for rollback.
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, List, Tuple

BACKUP_SUFFIX = ".bak_v4_6_status_contract"
MARKER = "CEO_DAILY_PNL_V4_6_STATUS_CONTRACT"


def syntax_check(text: str, path: Path) -> None:
    try:
        ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(f"Patched {path} has syntax error: {exc}") from exc


def ensure_import_unicodedata(text: str) -> str:
    if re.search(r"^import unicodedata\s*$", text, flags=re.M):
        return text
    m = re.search(r"^import re\s*$", text, flags=re.M)
    if not m:
        raise RuntimeError("Cannot find 'import re' for unicodedata insertion")
    return text[:m.end()] + "\nimport unicodedata" + text[m.end():]


def patch_nhanh(text: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    if "NHANH_CANCELLED_STATUSES_V46" not in text:
        old = 'NHANH_NO_BO_STATUSES = {"Đã hủy", "Hệ Thống hủy", "Đã hoàn", "Khách hủy"}\nNHANH_RETURN_STATUSES = {"Đã hoàn"}\nNHANH_RETURN_FEE_VND = 25000.0'
        new = '''# v4.6 exact Nhanh status semantics. Outer trim only; no fuzzy matching.\nNHANH_CANCELLED_STATUSES_V46 = {"Đã hủy", "Hệ Thống hủy", "Khách hủy", "HVC hủy"}\nNHANH_RETURN_STATUSES_V46 = {"Đang hoàn", "Xác nhận hoàn", "Đã hoàn"}\nNHANH_FAILED_STATUSES_V46 = {"Thất bại"}\n# Preserve deployed BO policy; Đã hoàn remains no-BO, while Đang hoàn/Xác nhận hoàn/Thất bại\n# are not assigned a new BO treatment without a separate owner decision.\nNHANH_NO_BO_STATUSES = set(NHANH_CANCELLED_STATUSES_V46) | {"Đã hoàn"}\nNHANH_RETURN_STATUSES = {"Đã hoàn"}\nNHANH_RETURN_FEE_VND = 25000.0'''
        if old not in text:
            raise RuntimeError("Nhanh status constants layout is unknown; refusing unsafe patch")
        text = text.replace(old, new, 1)
        notes.append("Nhanh exact cancel/return/failure status sets added")
    else:
        notes.append("Nhanh v4.6 status constants already present")

    old_assign = 'out["is_cancelled"] = out["status_exact"].isin(NHANH_NO_BO_STATUSES)'
    new_assign = 'out["is_cancelled"] = out["status_exact"].isin(NHANH_CANCELLED_STATUSES_V46)'
    if old_assign in text:
        text = text.replace(old_assign, new_assign, 1)
        notes.append("Nhanh is_cancelled now uses only exact cancellation statuses")
    elif new_assign in text:
        notes.append("Nhanh is_cancelled assignment already v4.6")
    else:
        raise RuntimeError("Nhanh is_cancelled assignment layout is unknown; refusing unsafe patch")

    text = text.replace(
        "# Only these statuses are treated as cancellation/return for Nhanh operational reporting:",
        "# v4.6: cancellation is separate from return/failure statuses. No fuzzy status matching:",
    )
    return text, notes


def _insert_helper(text: str, anchor: str, helper: str, marker: str) -> str:
    if marker in text:
        return text
    idx = text.find(anchor)
    if idx < 0:
        raise RuntimeError(f"Cannot find helper insertion anchor {anchor!r}")
    return text[:idx] + helper + "\n\n" + text[idx:]


def patch_shopee(text: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    text = ensure_import_unicodedata(text)
    helper = '''SHOPEE_CANCELLED_STATUS_EXACT_V46 = "đã hủy"\n\ndef normalize_shopee_status_v46(value: object) -> str:\n    """Harmless normalization only; exact Shopee cancellation match."""\n    raw = safe_text(value, 200)\n    return re.sub(r"\\s+", " ", unicodedata.normalize("NFC", raw)).strip().casefold()\n\ndef is_shopee_cancelled_status_v46(value: object) -> bool:\n    return normalize_shopee_status_v46(value) == SHOPEE_CANCELLED_STATUS_EXACT_V46'''
    before = text
    text = _insert_helper(text, "def split_shopee_combo_sku(", helper, "is_shopee_cancelled_status_v46")
    if text != before:
        notes.append("Shopee exact Đã hủy helper added")
    else:
        notes.append("Shopee v4.6 helper already present")

    candidates = [
        r'df\["is_cancelled_row"\]\s*=\s*df\[c_st\]\.astype\(str\)\.str\.contains\([^\n]+\)',
        r'df\["is_cancelled_row"\]\s*=\s*df\[c_st\]\.map\(is_exact_cancelled_status\)',
    ]
    exact = 'df["is_cancelled_row"] = df[c_st].map(is_shopee_cancelled_status_v46)'
    if exact not in text:
        replaced = 0
        for pattern in candidates:
            text, n = re.subn(pattern, exact, text, count=1)
            replaced += n
            if n:
                break
        if replaced != 1:
            raise RuntimeError("Shopee classifier layout is unknown; refusing unsafe patch")
        notes.append("Shopee classifier changed to exact normalized Đã hủy")
    else:
        notes.append("Shopee classifier already v4.6")

    old_gross = "gross_sales = 0.0 if is_cancelled else total_gross_main_products"
    new_gross = "gross_sales = total_gross_main_products"
    if old_gross in text:
        text = text.replace(old_gross, new_gross, 1)
        notes.append("Shopee gross_sales changed to all-status main-product Gross")
    elif new_gross in text:
        notes.append("Shopee gross_sales already all-status")
    else:
        raise RuntimeError("Shopee gross_sales layout is unknown; refusing unsafe patch")
    return text, notes


def patch_tiktok(text: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    text = ensure_import_unicodedata(text)
    helper = '''TIKTOK_CANCELLED_STATUS_EXACT_V46 = "canceled"\n\ndef normalize_tiktok_status_v46(value: object) -> str:\n    """Harmless normalization only; exact TikTok cancellation match."""\n    raw = safe_text(value, 200)\n    return re.sub(r"\\s+", " ", unicodedata.normalize("NFC", raw)).strip().casefold()\n\ndef is_tiktok_cancelled_status_v46(value: object) -> bool:\n    return normalize_tiktok_status_v46(value) == TIKTOK_CANCELLED_STATUS_EXACT_V46'''
    before = text
    text = _insert_helper(text, "def get_tiktok_platform_fee_rate(", helper, "is_tiktok_cancelled_status_v46")
    if text != before:
        notes.append("TikTok exact Canceled helper added")
    else:
        notes.append("TikTok v4.6 helper already present")

    candidates = [
        r'df_o\["is_cancelled"\]\s*=\s*df_o\["order_status"\]\.str\.contains\([^\n]+\)',
        r'df_o\["is_cancelled"\]\s*=\s*df_o\["order_status"\]\.map\(is_exact_cancelled_status\)',
    ]
    exact = 'df_o["is_cancelled"] = df_o["order_status"].map(is_tiktok_cancelled_status_v46)'
    if exact not in text:
        replaced = 0
        for pattern in candidates:
            text, n = re.subn(pattern, exact, text, count=1)
            replaced += n
            if n:
                break
        if replaced != 1:
            raise RuntimeError("TikTok classifier layout is unknown; refusing unsafe patch")
        notes.append("TikTok classifier changed to exact normalized Canceled")
    else:
        notes.append("TikTok classifier already v4.6")
    return text, notes


def append_claude_contract(text: str) -> Tuple[str, List[str]]:
    if MARKER in text:
        return text, ["CLAUDE.md already contains v4.6 status contract"]
    block = f'''\n\n<!-- {MARKER} -->\n## CEO Daily P&L v4.6 status contract\n\n- Shopee cancelled: only exact normalized `Đã hủy`.\n- TikTok cancelled: only exact normalized `Canceled`.\n- Nhanh cancelled: exact trimmed `Đã hủy`, `Hệ Thống hủy`, `Khách hủy`, `HVC hủy`.\n- Nhanh return statuses `Đang hoàn`, `Xác nhận hoàn`, `Đã hoàn` are NOT cancellation; `Thất bại` is also NOT cancellation.\n- Keep Nhanh +25,000 shipping rule only for exact `Đã hoàn`.\n- Nhanh parent daily P&L and the seven exact leaf labels are both required in the v4.6 CEO package.\n- Do not rerun the v4.5 single-literal exact-cancel patcher against v4.6 files.\n<!-- /{MARKER} -->\n'''
    return text.rstrip() + block, ["CLAUDE.md v4.6 contract appended"]


def patch_file(path: Path, fn: Callable[[str], Tuple[str, List[str]]], apply: bool, python_file: bool = True) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(path)
    original = path.read_text(encoding="utf-8")
    patched, notes = fn(original)
    if python_file:
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
    notes.extend([f"Written: {path}", f"Backup: {backup}"])
    return notes


def self_test() -> int:
    failures: List[str] = []
    nhanh = '''NHANH_NO_BO_STATUSES = {"Đã hủy", "Hệ Thống hủy", "Đã hoàn", "Khách hủy"}\nNHANH_RETURN_STATUSES = {"Đã hoàn"}\nNHANH_RETURN_FEE_VND = 25000.0\nout["is_cancelled"] = out["status_exact"].isin(NHANH_NO_BO_STATUSES)\n'''
    shopee = '''import re\ndef safe_text(v, n=200): return str(v)\ndef split_shopee_combo_sku(x): return []\ndf["is_cancelled_row"] = df[c_st].astype(str).str.contains("đã hủy|da huy|cancel|cancelled", case=False, na=False)\ngross_sales = 0.0 if is_cancelled else total_gross_main_products\n'''
    tiktok = '''import re\ndef safe_text(v, n=200): return str(v)\ndef get_tiktok_platform_fee_rate(x): return 0\ndf_o["is_cancelled"] = df_o["order_status"].str.contains("hủy|huỷ|cancel", case=False, na=False)\n'''
    try:
        n, _ = patch_nhanh(nhanh)
        s, _ = patch_shopee(shopee)
        t, _ = patch_tiktok(tiktok)
        if 'isin(NHANH_CANCELLED_STATUSES_V46)' not in n or '"HVC hủy"' not in n:
            failures.append("Nhanh patch failed")
        if 'map(is_shopee_cancelled_status_v46)' not in s or 'gross_sales = total_gross_main_products' not in s:
            failures.append("Shopee patch failed")
        if 'TIKTOK_CANCELLED_STATUS_EXACT_V46 = "canceled"' not in t or 'map(is_tiktok_cancelled_status_v46)' not in t:
            failures.append("TikTok patch failed")
    except Exception as exc:
        failures.append(str(exc))
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for x in failures: print("-", x, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Shopee=exact Đã hủy; TikTok=exact Canceled; Nhanh=exact cancel whitelist; returns/failure separate.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    root = Path(args.repo_root).resolve()
    targets = [
        (root / "scripts" / "nhanh_etl.py", patch_nhanh, True),
        (root / "scripts" / "shopee_etl.py", patch_shopee, True),
        (root / "scripts" / "tiktok_etl.py", patch_tiktok, True),
    ]
    for path, fn, is_py in targets:
        print(f"\n[{path.name}]")
        for note in patch_file(path, fn, args.apply, is_py):
            print("-", note)

    claude = root / "CLAUDE.md"
    if claude.exists():
        print("\n[CLAUDE.md]")
        for note in patch_file(claude, append_claude_contract, args.apply, False):
            print("-", note)

    required = [
        root / "scripts" / "build_ceo_daily_pnl_package_v4_6.py",
        root / "scripts" / "validate_ceo_daily_pnl_package_v4_6.py",
        root / "sql" / "audit_ceo_daily_pnl_v4_6_status_contract.sql",
        root / "run_daily_pnl_v4_6.ps1",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        print("\nERROR: v4.6 bundle was not overlaid completely. Missing:", file=sys.stderr)
        for p in missing: print("-", p, file=sys.stderr)
        return 2

    if not args.apply:
        print("\nCHECK ONLY completed. Re-run with --apply after reviewing the output.")
    else:
        print("\nV4.6 source upgrade applied. Next: re-import marketplace history, run SQL audit, then build/validate v4.6 package.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
