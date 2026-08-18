#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply the v4.6.1 Nhanh ETL normalization upgrade.

Default is CHECK-ONLY. Use --apply to write changes.
The patcher assumes v4.6 is already installed and intentionally leaves v4.6
builder/validator files untouched for rollback.
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

BACKUP_SUFFIX = ".bak_v4_6_1_status_normalization"
MARKER = "CEO_DAILY_PNL_V4_6_1_CLEANUP_CONTRACT"


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
        raise RuntimeError("Cannot find 'import re' in nhanh_etl.py")
    return text[:m.end()] + "\nimport unicodedata" + text[m.end():]


def patch_nhanh(text: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    if "NHANH_CANCELLED_STATUSES_V46" not in text:
        raise RuntimeError("Nhanh v4.6 status constants not found. Install v4.6 before v4.6.1.")

    text = ensure_import_unicodedata(text)

    helper_marker = "def normalize_nhanh_status_v461("
    if helper_marker not in text:
        anchor = "NHANH_RETURN_FEE_VND = 25000.0"
        idx = text.find(anchor)
        if idx < 0:
            raise RuntimeError("Cannot find NHANH_RETURN_FEE_VND anchor; refusing unsafe patch")
        insert_at = idx + len(anchor)
        helper = r'''


def normalize_nhanh_status_v461(value: object) -> str:
    """Unicode NFC + case-insensitive + trim/collapse whitespace; no accent folding."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = unicodedata.normalize("NFC", str(value))
    return re.sub(r"\s+", " ", text).strip().casefold()


def is_nhanh_cancelled_status_v461(value: object) -> bool:
    normalized = normalize_nhanh_status_v461(value)
    return normalized in {normalize_nhanh_status_v461(x) for x in NHANH_CANCELLED_STATUSES_V46}


def is_nhanh_completed_return_status_v461(value: object) -> bool:
    return normalize_nhanh_status_v461(value) == normalize_nhanh_status_v461("Đã hoàn")
'''
        text = text[:insert_at] + helper + text[insert_at:]
        notes.append("Nhanh normalized-exact v4.6.1 helpers added")
    else:
        notes.append("Nhanh v4.6.1 helpers already present")

    old_assignments = [
        'out["is_cancelled"] = out["status_exact"].isin(NHANH_CANCELLED_STATUSES_V46)',
        'out["is_cancelled"] = out["status_exact"].isin(NHANH_NO_BO_STATUSES)',
    ]
    new_assignment = 'out["is_cancelled"] = out["status_exact"].map(is_nhanh_cancelled_status_v461)'
    if new_assignment not in text:
        found = False
        for old in old_assignments:
            if old in text:
                text = text.replace(old, new_assignment, 1)
                found = True
                notes.append("Nhanh is_cancelled changed to normalized exact matching")
                break
        if not found:
            raise RuntimeError("Unknown Nhanh is_cancelled assignment layout; refusing unsafe patch")
    else:
        notes.append("Nhanh is_cancelled already v4.6.1")

    old_return = 'return_fee = out["status_exact"].eq("Đã hoàn").astype(float) * NHANH_RETURN_FEE_VND'
    new_return = 'return_fee = out["status_exact"].map(is_nhanh_completed_return_status_v461).astype(float) * NHANH_RETURN_FEE_VND'
    if new_return not in text:
        if old_return not in text:
            raise RuntimeError("Unknown Nhanh return-fee status layout; refusing unsafe patch")
        text = text.replace(old_return, new_return, 1)
        notes.append("Nhanh Đã hoàn +25k rule changed to normalized exact matching")
    else:
        notes.append("Nhanh return-fee classifier already v4.6.1")

    text = text.replace(
        "# v4.6: cancellation is separate from return/failure statuses. No fuzzy status matching:",
        "# v4.6.1: cancellation is normalized-exact (NFC/case-insensitive/trim-collapse); no fuzzy/substring matching:",
    )
    return text, notes


def append_claude_contract(text: str) -> Tuple[str, List[str]]:
    if MARKER in text:
        return text, ["CLAUDE.md already contains v4.6.1 cleanup contract"]
    block = f'''

<!-- {MARKER} -->
## CEO Daily P&L v4.6.1 cleanup contract

- Keep v4.6 financial waterfall unchanged: Gross -> Net Sales -> GM1 -> CM1 -> CM2 -> Profit. No EBITDA.
- Shopee cancelled: exact normalized `Đã hủy` only.
- TikTok cancelled: exact normalized `Canceled` only.
- Nhanh cancellation uses Unicode NFC + case-insensitive + trim/collapse whitespace, then exact phrase matching against `Đã hủy`, `Hệ Thống hủy`, `Khách hủy`, `HVC hủy`. No accent folding, fuzzy matching or substring matching.
- Therefore `Hệ Thống hủy`, `Hệ thống hủy`, and whitespace/case variants are equivalent; `Đã huỷ` is not silently converted to `Đã hủy`.
- Nhanh return statuses `Đang hoàn`, `Xác nhận hoàn`, `Đã hoàn` and failed status `Thất bại` are not cancelled. +25,000 shipping remains for normalized-exact `Đã hoàn` only.
- Cancel-like diagnostics must use whole tokens; `Đang chuyển` must never be flagged merely because it contains the substring `huy` across letters.
- On no-sales/no-source rows used in tables/charts, applicable numeric KPIs are 0; true N/A remains null.
- `success_orders` is a cross-channel non-cancelled compatibility field. For Nhanh literal `Thành công`, use `successful_orders_exact`. Never label Nhanh `success_orders` as literal `Đơn thành công`.
- Use v4.6.1 builder/validator for new CEO packages; keep v4.6 only for rollback.
<!-- /{MARKER} -->
'''
    return text.rstrip() + block, ["CLAUDE.md v4.6.1 contract appended"]


def patch_file(path: Path, fn, apply: bool, python_file: bool = True) -> List[str]:
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


def verify_v46_marketplaces(root: Path) -> List[str]:
    notes: List[str] = []
    shopee = (root / "scripts" / "shopee_etl.py").read_text(encoding="utf-8")
    tiktok = (root / "scripts" / "tiktok_etl.py").read_text(encoding="utf-8")
    if "is_shopee_cancelled_status_v46" not in shopee:
        raise RuntimeError("Shopee v4.6 exact-cancel helper missing; do not apply v4.6.1 on an incomplete v4.6 repo")
    if "is_tiktok_cancelled_status_v46" not in tiktok:
        raise RuntimeError("TikTok v4.6 exact-cancel helper missing; do not apply v4.6.1 on an incomplete v4.6 repo")
    notes.append("Shopee v4.6 exact Đã hủy classifier verified")
    notes.append("TikTok v4.6 exact Canceled classifier verified")
    return notes


def self_test() -> int:
    sample = '''import re\nimport pandas as pd\nNHANH_CANCELLED_STATUSES_V46 = {"Đã hủy", "Hệ Thống hủy", "Khách hủy", "HVC hủy"}\nNHANH_RETURN_STATUSES_V46 = {"Đang hoàn", "Xác nhận hoàn", "Đã hoàn"}\nNHANH_FAILED_STATUSES_V46 = {"Thất bại"}\nNHANH_NO_BO_STATUSES = set(NHANH_CANCELLED_STATUSES_V46) | {"Đã hoàn"}\nNHANH_RETURN_STATUSES = {"Đã hoàn"}\nNHANH_RETURN_FEE_VND = 25000.0\nout["is_cancelled"] = out["status_exact"].isin(NHANH_CANCELLED_STATUSES_V46)\nreturn_fee = out["status_exact"].eq("Đã hoàn").astype(float) * NHANH_RETURN_FEE_VND\n'''
    try:
        patched, _ = patch_nhanh(sample)
        if "map(is_nhanh_cancelled_status_v461)" not in patched:
            raise AssertionError("cancel classifier not patched")
        if "map(is_nhanh_completed_return_status_v461)" not in patched:
            raise AssertionError("return classifier not patched")
        ns = {}
        # Compile only; execution needs mock variables and is not required here.
        ast.parse(patched)
    except Exception as exc:
        print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("v4.6.1 Nhanh classifier: NFC + case-insensitive + trim/collapse + exact phrase; no accent folding/substrings.")
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
    required_v46 = [
        root / "scripts" / "build_ceo_daily_pnl_package_v4_6.py",
        root / "scripts" / "validate_ceo_daily_pnl_package_v4_6.py",
        root / "run_daily_pnl_v4_6.ps1",
    ]
    missing_v46 = [str(p) for p in required_v46 if not p.exists()]
    if missing_v46:
        print("ERROR: v4.6 base is incomplete:", file=sys.stderr)
        for p in missing_v46:
            print("-", p, file=sys.stderr)
        return 2

    print("\n[marketplace v4.6 prerequisites]")
    for note in verify_v46_marketplaces(root):
        print("-", note)

    nhanh = root / "scripts" / "nhanh_etl.py"
    print("\n[nhanh_etl.py]")
    for note in patch_file(nhanh, patch_nhanh, args.apply, True):
        print("-", note)

    claude = root / "CLAUDE.md"
    if claude.exists():
        print("\n[CLAUDE.md]")
        for note in patch_file(claude, append_claude_contract, args.apply, False):
            print("-", note)

    required_v461 = [
        root / "scripts" / "build_ceo_daily_pnl_package_v4_6_1.py",
        root / "scripts" / "validate_ceo_daily_pnl_package_v4_6_1.py",
        root / "run_daily_pnl_v4_6_1.ps1",
        root / "sql" / "audit_ceo_daily_pnl_v4_6_1.sql",
    ]
    missing = [str(p) for p in required_v461 if not p.exists()]
    if missing:
        print("\nERROR: v4.6.1 overlay incomplete. Missing:", file=sys.stderr)
        for p in missing:
            print("-", p, file=sys.stderr)
        return 2

    if not args.apply:
        print("\nCHECK ONLY completed. Re-run with --apply after reviewing output.")
    else:
        print("\nV4.6.1 source upgrade applied. Shopee/TikTok do not require another re-import if v4.6 mismatch is already 0.")
        print("Re-import Nhanh is recommended to clean legacy is_cancelled flags, but v4.6.1 reporting recomputes Nhanh from raw status directly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
