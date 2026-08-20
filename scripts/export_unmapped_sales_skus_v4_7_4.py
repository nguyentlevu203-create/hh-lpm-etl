#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export unresolved v4.7.4 sales SKU mappings from a built package to CSV."""
from __future__ import annotations
import argparse,csv,json
from pathlib import Path

FIELDS=("channel","sku","source_skus","product_name","sold_qty_mtd","gift_qty_mtd","total_push_qty_mtd","mapping_status")

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--package",required=True)
    ap.add_argument("--out",default=None)
    args=ap.parse_args()
    p=Path(args.package); d=json.loads(p.read_text(encoding="utf-8"))
    rows=d.get("unmapped_sales_skus_v4_7_4") or []
    out=Path(args.out) if args.out else p.with_name(p.stem+"_unmapped_skus.csv")
    with out.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS); w.writeheader()
        for r in rows:
            w.writerow({
                "channel":r.get("channel"),"sku":r.get("sku"),
                "source_skus":" | ".join(str(x) for x in (r.get("source_skus") or [])),
                "product_name":r.get("product_name"),"sold_qty_mtd":r.get("sold_qty_mtd"),
                "gift_qty_mtd":r.get("gift_qty_mtd"),"total_push_qty_mtd":r.get("total_push_qty_mtd"),
                "mapping_status":r.get("mapping_status"),
            })
    print(f"Exported {len(rows)} unresolved rows: {out}")
    return 0
if __name__=="__main__": raise SystemExit(main())
