#!/usr/bin/env python3
"""Patch Shopee/TikTok ETL to persist CEO seller-funded deductions.

Run from the project root after applying the DB migration. The patch is narrow:
- Shopee: persist exact subsidy/voucher columns and prevent order voucher duplication.
- TikTok: persist exact SKU Seller Discount at item and order levels.

The script validates every source anchor, compiles both patched files before
replacement, and creates timestamped backups.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path


SHOPEE = Path("scripts/shopee_etl.py")
TIKTOK = Path("scripts/tiktok_etl.py")


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"Cannot patch {label}: expected exactly 1 anchor, found {count}. "
            "No production file was changed."
        )
    return source.replace(old, new, 1)


def patch_shopee(source: str) -> str:
    if "total_subsidy_amount" in source and "shop_voucher_amount" in source:
        return source

    source = replace_once(
        source,
        '''        c_voucher = find_col(df.columns, ["mã giảm giá của shop", "shop voucher", "voucher shop"]) or "Mã giảm giá của Shop"
        c_price = find_col(df.columns, ["giá gốc", "gia goc", "original price"]) or "Giá gốc"
        c_qty = find_col(df.columns, ["số lượng", "so luong", "quantity"]) or "Số lượng"
        c_subsidy = find_col(df.columns, ["người bán trợ giá", "nguoi ban tro gia", "seller subsidy"]) or "Tổng số tiền được người bán trợ giá"
''',
        '''        # CEO Net Sales bridge: use exact Shopee headers. Do not fuzzy-match
        # these fields because seller-funded and platform-funded deductions have
        # different accounting treatment.
        c_voucher = find_exact_header(df.columns, "Mã giảm giá của Shop")
        c_price = find_col(df.columns, ["giá gốc", "gia goc", "original price"]) or "Giá gốc"
        c_qty = find_col(df.columns, ["số lượng", "so luong", "quantity"]) or "Số lượng"
        c_subsidy = find_exact_header(df.columns, "Người bán trợ giá")
        c_platform_subsidy = find_exact_header(df.columns, "Được Shopee trợ giá")
        c_total_subsidy = find_exact_header(df.columns, "Tổng số tiền được người bán trợ giá")
''',
        "Shopee exact financial headers",
    )
    source = replace_once(
        source,
        '''        require_columns(df, [c_id, c_st, c_dt, c_sku, c_fix, c_svc, c_pay, c_paid, c_voucher, c_price, c_qty, c_subsidy, c_name])
''',
        '''        require_columns(df, [
            c_id, c_st, c_dt, c_sku, c_fix, c_svc, c_pay, c_paid,
            c_voucher, c_price, c_qty, c_subsidy, c_platform_subsidy,
            c_total_subsidy, c_name,
        ])
''',
        "Shopee required columns",
    )
    source = replace_once(
        source,
        '''            seller_subsidy = safe_num(money_series(group[c_subsidy]).sum())
            shop_voucher = safe_num(money_series(group[c_voucher]).sum())
            rev_before_cancel = safe_num(total_gross_all_rows - seller_subsidy - shop_voucher)
''',
        '''            seller_subsidy = safe_num(money_series(group[c_subsidy]).sum())
            platform_subsidy = safe_num(money_series(group[c_platform_subsidy]).sum())
            total_subsidy = safe_num(money_series(group[c_total_subsidy]).sum())

            # "Mã giảm giá của Shop" is an order-level amount repeated on every
            # SKU line. MAX records it once; SUM would double count multi-SKU orders.
            shop_voucher = safe_num(money_series(group[c_voucher]).max())

            # Source invariant confirmed in Shopee exports:
            # total subsidy = seller-funded subsidy + Shopee-funded subsidy.
            if abs(total_subsidy - seller_subsidy - platform_subsidy) > 1.0:
                raise ValueError(
                    "Sai đối soát trợ giá Shopee. "
                    f"order={oid_clean}, seller={seller_subsidy}, "
                    f"platform={platform_subsidy}, total={total_subsidy}"
                )

            # Keep the legacy revenue formula otherwise unchanged. Platform-funded
            # subsidy is stored for audit and is not deducted from seller revenue.
            rev_before_cancel = safe_num(total_gross_all_rows - seller_subsidy - shop_voucher)
''',
        "Shopee subsidy aggregation",
    )
    source = replace_once(
        source,
        '''                "seller_discount": seller_subsidy,
                "platform_voucher": shop_voucher,
                "seller_revenue": seller_revenue,
''',
        '''                # Legacy keys remain for compatibility with existing reports.
                "seller_discount": seller_subsidy,
                "platform_voucher": shop_voucher,
                # Explicit CEO bridge fields.
                "seller_subsidy_amount": seller_subsidy,
                "platform_subsidy_amount": platform_subsidy,
                "total_subsidy_amount": total_subsidy,
                "shop_voucher_amount": shop_voucher,
                "seller_revenue": seller_revenue,
''',
        "Shopee order payload",
    )
    source = replace_once(
        source,
        '''                    safe_num(r.get("seller_discount", 0.0)),
                    safe_num(r.get("platform_voucher", 0.0)),
                    r.get("source_file"),
''',
        '''                    safe_num(r.get("seller_discount", 0.0)),
                    safe_num(r.get("platform_voucher", 0.0)),
                    safe_num(r.get("seller_subsidy_amount", 0.0)),
                    safe_num(r.get("platform_subsidy_amount", 0.0)),
                    safe_num(r.get("total_subsidy_amount", 0.0)),
                    safe_num(r.get("shop_voucher_amount", 0.0)),
                    r.get("source_file"),
''',
        "Shopee financial extra values",
    )
    source = replace_once(
        source,
        '''                        seller_discount, platform_voucher, source_file
                    ) VALUES %s
''',
        '''                        seller_discount, platform_voucher,
                        seller_subsidy_amount, platform_subsidy_amount,
                        total_subsidy_amount, shop_voucher_amount, source_file
                    ) VALUES %s
''',
        "Shopee financial extra insert columns",
    )
    source = replace_once(
        source,
        '''                        platform_voucher = EXCLUDED.platform_voucher,
                        source_file = EXCLUDED.source_file,
''',
        '''                        platform_voucher = EXCLUDED.platform_voucher,
                        seller_subsidy_amount = EXCLUDED.seller_subsidy_amount,
                        platform_subsidy_amount = EXCLUDED.platform_subsidy_amount,
                        total_subsidy_amount = EXCLUDED.total_subsidy_amount,
                        shop_voucher_amount = EXCLUDED.shop_voucher_amount,
                        source_file = EXCLUDED.source_file,
''',
        "Shopee financial extra update columns",
    )
    return source


def patch_tiktok(source: str) -> str:
    if (
        '"seller_discount_amount": "sum"' in source
        and "seller_discount_amount = EXCLUDED.seller_discount_amount" in source
    ):
        return source

    source = replace_once(
        source,
        '''        "discount": find_col(df_o.columns, ["seller discount", "chiết khấu từ người bán", "chiết khấu", "giảm giá"]),
''',
        '''        # CEO Net Sales bridge: exact TikTok seller-funded SKU discount.
        "discount": find_one_exact_header(df_o.columns, ["SKU Seller Discount"]),
''',
        "TikTok exact seller discount header",
    )
    source = replace_once(
        source,
        '''    df_o["disc_num"] = money_series(df_o[co["discount"]]).abs() if co["discount"] else 0.0
    df_o["gmv_item"] = money_series(df_o[co["gmv"]]) if co["gmv"] else (df_o["price_num"] * df_o["qty_num"] - df_o["disc_num"])
''',
        '''    df_o["disc_num"] = money_series(df_o[co["discount"]]).abs()
    df_o["seller_discount_amount"] = df_o["disc_num"]
    df_o["gmv_item"] = money_series(df_o[co["gmv"]]) if co["gmv"] else (df_o["price_num"] * df_o["qty_num"] - df_o["disc_num"])
''',
        "TikTok item seller discount",
    )
    source = replace_once(
        source,
        '''        "gmv_item": "sum",
        "line_cogs": "sum",
''',
        '''        "gmv_item": "sum",
        "seller_discount_amount": "sum",
        "line_cogs": "sum",
''',
        "TikTok order aggregation",
    )
    source = replace_once(
        source,
        '''                    int(safe_num(r["qty_num"])), safe_num(r["fee_base_item"]), safe_num(r["gmv_item"]),
                    safe_num(r["fixed_fee"]), safe_num(r["payment_fee"]), safe_num(r["vxp_fee"]), safe_num(r["infra_fee"]),
''',
        '''                    int(safe_num(r["qty_num"])), safe_num(r["fee_base_item"]), safe_num(r["gmv_item"]),
                    safe_num(r.get("seller_discount_amount", 0)),
                    safe_num(r["fixed_fee"]), safe_num(r["payment_fee"]), safe_num(r["vxp_fee"]), safe_num(r["infra_fee"]),
''',
        "TikTok order values",
    )
    source = replace_once(
        source,
        '''                    total_sku_qty, fee_base_revenue, gmv_before_cancel,
                    fixed_fee, payment_fee, vxp_fee, infrastructure_fee, commission_amount, booking_fee,
''',
        '''                    total_sku_qty, fee_base_revenue, gmv_before_cancel, seller_discount_amount,
                    fixed_fee, payment_fee, vxp_fee, infrastructure_fee, commission_amount, booking_fee,
''',
        "TikTok order insert columns",
    )
    source = replace_once(
        source,
        '''                    gmv_before_cancel = EXCLUDED.gmv_before_cancel,
                    fixed_fee = EXCLUDED.fixed_fee,
''',
        '''                    gmv_before_cancel = EXCLUDED.gmv_before_cancel,
                    seller_discount_amount = EXCLUDED.seller_discount_amount,
                    fixed_fee = EXCLUDED.fixed_fee,
''',
        "TikTok order update columns",
    )
    source = replace_once(
        source,
        '''                        int(safe_num(row.get("qty_num", 0))), safe_num(row.get("price_num", 0)), max(safe_num(row.get("line_unit_cogs", 0)), 0),
                        batch_id, row.get("__source_file"), Json(json_safe(row_to_json(row))),
''',
        '''                        int(safe_num(row.get("qty_num", 0))), safe_num(row.get("price_num", 0)),
                        safe_num(row.get("seller_discount_amount", 0)), max(safe_num(row.get("line_unit_cogs", 0)), 0),
                        batch_id, row.get("__source_file"), Json(json_safe(row_to_json(row))),
''',
        "TikTok item values",
    )
    source = replace_once(
        source,
        '''                        quantity, price, unit_cogs, batch_id, source_file, raw_payload
''',
        '''                        quantity, price, seller_discount_amount, unit_cogs, batch_id, source_file, raw_payload
''',
        "TikTok item insert columns",
    )
    return source


def main() -> None:
    for path in (SHOPEE, TIKTOK):
        if not path.is_file():
            raise FileNotFoundError(f"Missing {path}; run from the ETL project root.")

    shopee_source = SHOPEE.read_text(encoding="utf-8-sig")
    tiktok_source = TIKTOK.read_text(encoding="utf-8-sig")
    shopee_patched = patch_shopee(shopee_source)
    tiktok_patched = patch_tiktok(tiktok_source)

    if shopee_patched == shopee_source and tiktok_patched == tiktok_source:
        print("CEO discount upgrade is already installed; no files changed.")
        return

    # Validate both complete outputs before changing either production file.
    compile(shopee_patched, str(SHOPEE), "exec")
    compile(tiktok_patched, str(TIKTOK), "exec")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backups = []
    for path in (SHOPEE, TIKTOK):
        backup = path.with_name(f"{path.stem}.bak_before_ceo_discount_{stamp}.py")
        shutil.copy2(path, backup)
        backups.append(backup)

    for path, content in ((SHOPEE, shopee_patched), (TIKTOK, tiktok_patched)):
        temp = path.with_suffix(".py.tmp")
        temp.write_text(content, encoding="utf-8", newline="")
        os.replace(temp, path)

    # Re-read and compile the actual replaced files.
    compile(SHOPEE.read_text(encoding="utf-8"), str(SHOPEE), "exec")
    compile(TIKTOK.read_text(encoding="utf-8"), str(TIKTOK), "exec")

    print(f"Patched: {SHOPEE}")
    print(f"Patched: {TIKTOK}")
    for backup in backups:
        print(f"Backup: {backup}")
    print("Next: run Shopee and TikTok ETL, then execute the verification SQL.")


if __name__ == "__main__":
    main()
