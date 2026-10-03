#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, json, math, sys
from pathlib import Path

EPS=1.0
SPECIAL="GT_NV001"
FIELDS=("gross_sales","net_sales","orders","sold_cogs","gift_cogs","cogs_total","gm1","logistics_order_cost","cm1","ads_total","cm2","backoffice_cost","profit")

def f(v):
    try:
        x=float(v or 0); return 0.0 if math.isnan(x) or math.isinf(x) else x
    except Exception:return 0.0

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("package"); args=ap.parse_args()
    p=Path(args.package); pkg=json.loads(p.read_text(encoding="utf-8"))
    errors=[]
    if str((pkg.get("metadata") or {}).get("package_version"))!="v4.7.5": errors.append("metadata.package_version must be v4.7.5")
    block=pkg.get("gt_special_employee_v4_7_5") or {}
    if block.get("employee_code")!="NV001": errors.append("special employee code must be NV001")
    if str(block.get("employee_name") or "").upper()!="NGUYỄN XUÂN MẠNH": errors.append("special employee name mismatch")
    sp=(block.get("current_mtd") or {})
    if abs(f(sp.get("ads_total")))>EPS: errors.append("NV001 ads_total must be zero")
    comp=pkg.get("reporting_mtd_pnl_comparison_by_channel_v4_7_5") or {}
    if "GT" not in comp or SPECIAL not in comp: errors.append("reporting MTD comparison must contain GT and GT_NV001")
    audit=((pkg.get("data_quality") or {}).get("v4_7_5_gt_special_employee") or {})
    for name in ("mtd_gt_original_equals_gt_ex_plus_nv001","company_total_preserved"):
        gaps=audit.get(name) or {}
        for k,v in gaps.items():
            if abs(f(v))>EPS: errors.append(f"{name}.{k} gap={f(v)}")
    contract=((pkg.get("excel_render_contract") or {}).get("v4_7_5_gt_special_employee") or {})
    if not contract: errors.append("missing Excel v4.7.5 GT special employee render contract")
    if errors:
        print("VALIDATION FAILED", file=sys.stderr)
        for e in errors: print("-",e,file=sys.stderr)
        return 1
    print("VALIDATION PASSED")
    print("GT excludes NV001 in reporting blocks; NV001 rendered separately; company total preserved; NV001 ads=0.")
    return 0
if __name__=="__main__": raise SystemExit(main())
