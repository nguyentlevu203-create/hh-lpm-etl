#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check repo readiness for CEO Daily P&L v4.7 without changing files."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GITHUB_BASE_COMMIT = "8c977bb9e4b6a52e473ef32edbf4b6d5eb103889"


def has(path: Path, token: str) -> bool:
    return path.exists() and token in path.read_text(encoding="utf-8", errors="replace")


def inspect(root: Path):
    files = {
        "v45_builder": root / "scripts" / "build_ceo_daily_pnl_package_v4_5.py",
        "v46_builder": root / "scripts" / "build_ceo_daily_pnl_package_v4_6.py",
        "v461_builder": root / "scripts" / "build_ceo_daily_pnl_package_v4_6_1.py",
        "v47_builder": root / "scripts" / "build_ceo_daily_pnl_package_v4_7.py",
        "nhanh": root / "scripts" / "nhanh_etl.py",
        "shopee": root / "scripts" / "shopee_etl.py",
        "tiktok": root / "scripts" / "tiktok_etl.py",
    }
    state = {
        "repo_root": str(root),
        "upgrade_basis_github_commit": GITHUB_BASE_COMMIT,
        "v45_base_present": files["v45_builder"].exists(),
        "v46_overlay_present": files["v46_builder"].exists(),
        "v461_overlay_present": files["v461_builder"].exists(),
        "v47_overlay_present": files["v47_builder"].exists(),
        "shopee_v46_exact_cancel": has(files["shopee"], "SHOPEE_CANCELLED_STATUS_EXACT_V46") and has(files["shopee"], "is_shopee_cancelled_status_v46"),
        "tiktok_v46_exact_cancel": has(files["tiktok"], "TIKTOK_CANCELLED_STATUS_EXACT_V46") and has(files["tiktok"], "is_tiktok_cancelled_status_v46"),
        "nhanh_v461_normalization": has(files["nhanh"], "normalize_nhanh_status_v461") and has(files["nhanh"], "is_nhanh_cancelled_status_v461"),
    }
    state["v46_source_ready"] = state["shopee_v46_exact_cancel"] and state["tiktok_v46_exact_cancel"]
    state["v461_source_ready"] = state["v46_source_ready"] and state["nhanh_v461_normalization"]
    state["v47_runtime_ready"] = state["v45_base_present"] and state["v46_overlay_present"] and state["v461_overlay_present"] and state["v47_overlay_present"] and state["v461_source_ready"]
    if not state["v45_base_present"]:
        state["action"] = "UNSUPPORTED: missing v4.5 base builder"
    elif state["v461_source_ready"]:
        state["action"] = "PREREQUISITES_ALREADY_APPLIED: do not rerun v4.6 patcher; install/use v4.7 only"
    elif state["v46_source_ready"]:
        state["action"] = "APPLY_V4_6_1_PREREQUISITE_THEN_V4_7"
    else:
        state["action"] = "APPLY_V4_6_THEN_V4_6_1_PREREQUISITES_THEN_V4_7"
    return state


def self_test() -> int:
    # Token-level self-test only; no repo writes.
    if not GITHUB_BASE_COMMIT.startswith("8c977bb"):
        print("SELF-TEST FAILED", file=sys.stderr); return 1
    print("SELF-TEST PASSED"); return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    state = inspect(Path(args.repo_root).resolve())
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if not state["v45_base_present"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
