#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run daily HH inventory pipeline.

Folder convention:
  data/inventory/daily/YYYY-MM-DD/HN/*.xlsx
  data/inventory/daily/YYYY-MM-DD/HCM/*.xlsx
  data/inventory/rules/*.xlsx

This wrapper runs:
  1) import_reorder_alert_rules.py, optional
  2) inventory_daily_filter.py for HN with --rerun
  3) inventory_daily_filter.py for HCM with --rerun
  4) build_ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible.py, optional

Important:
- --rerun replaces the same snapshot_date + warehouse safely through staging + commit.
- Previous dates are kept for history. Reports always read the selected --date.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


def find_one_xlsx(folder: Path, label: str) -> Path:
    files = sorted([p for p in folder.glob("*.xlsx") if not p.name.startswith("~$")])
    if not files:
        raise FileNotFoundError(f"No .xlsx file found for {label}: {folder}")
    if len(files) > 1:
        names = "\n".join(f" - {p.name}" for p in files)
        raise RuntimeError(
            f"More than one .xlsx file found for {label}: {folder}\n{names}\n"
            "Keep exactly one file in this folder or pass explicit paths."
        )
    return files[0]


def newest_xlsx(folder: Path) -> Optional[Path]:
    files = sorted(
        [p for p in folder.glob("*.xlsx") if not p.name.startswith("~$")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def run(cmd: list[str], dry_run: bool = False) -> None:
    print("\n>>> " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="Snapshot/report date, e.g. 2026-05-20")
    ap.add_argument("--base-dir", default="data/inventory", help="Inventory base folder")
    ap.add_argument("--hn-file", default=None, help="Explicit HN inventory xlsx")
    ap.add_argument("--hcm-file", default=None, help="Explicit HCM inventory xlsx")
    ap.add_argument("--rules-file", default=None, help="Explicit reorder rules xlsx")
    ap.add_argument("--skip-rules", action="store_true", help="Skip importing reorder rules")
    ap.add_argument("--build-package", action="store_true", help="Build CEO package after import")
    ap.add_argument("--store-db", action="store_true", help="Store CEO package in DB")
    ap.add_argument("--out-dir", default="outbox/packages", help="Package output folder")
    ap.add_argument("--dry-run", action="store_true", help="Print commands only")
    args = ap.parse_args()

    root = Path.cwd()
    base_dir = Path(args.base_dir)
    day_dir = base_dir / "daily" / args.date
    hn_file = Path(args.hn_file) if args.hn_file else find_one_xlsx(day_dir / "HN", "HN")
    hcm_file = Path(args.hcm_file) if args.hcm_file else find_one_xlsx(day_dir / "HCM", "HCM")
    rules_file = Path(args.rules_file) if args.rules_file else newest_xlsx(base_dir / "rules")

    summary = {
        "date": args.date,
        "hn_file": str(hn_file),
        "hcm_file": str(hcm_file),
        "rules_file": str(rules_file) if rules_file else None,
        "build_package": args.build_package,
        "store_db": args.store_db,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    py = sys.executable

    if not args.skip_rules:
        if not rules_file:
            raise FileNotFoundError("No reorder rules file found. Put it in data/inventory/rules or pass --rules-file.")
        run([py, "scripts/import_reorder_alert_rules.py", "--file", str(rules_file), "--replace-active"], args.dry_run)

    run([
        py,
        "scripts/inventory_daily_filter.py",
        "--snapshot-date",
        args.date,
        "--warehouse",
        "HN",
        "--file",
        str(hn_file),
        "--rerun",
    ], args.dry_run)

    run([
        py,
        "scripts/inventory_daily_filter.py",
        "--snapshot-date",
        args.date,
        "--file",
        str(hcm_file),
        "--rerun",
    ], args.dry_run)

    if args.build_package:
        cmd = [
            py,
            "scripts/build_ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible.py",
            "--date",
            args.date,
            "--out-dir",
            args.out_dir,
        ]
        if args.store_db:
            cmd.append("--store-db")
        run(cmd, args.dry_run)

    print("\nDONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
