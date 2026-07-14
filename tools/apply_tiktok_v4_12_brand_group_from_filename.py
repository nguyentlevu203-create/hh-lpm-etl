#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply TikTok v4.12 patch: brand_group follows filename-derived shop_label.

This patch is intentionally surgical. It does not rewrite the whole ETL file,
so previous fixes for combo SKU, platform fee from 2026-05-08, ads, live cost,
settlement, and packaging remain intact.
"""
from __future__ import annotations

import re
from pathlib import Path

TARGET = Path("scripts/tiktok_etl.py")


def patch_file() -> None:
    if not TARGET.exists():
        raise FileNotFoundError(
            f"Không tìm thấy {TARGET}. Hãy chạy script ở thư mục gốc ETL_production_v3_exact_codes."
        )

    s = TARGET.read_text(encoding="utf-8")
    backup = TARGET.with_suffix(".py.bak_v4_12_brand_group_from_filename")
    backup.write_text(s, encoding="utf-8")

    # 1) Ensure filename/shop fallback is LPM, not TIKTOK.
    s = s.replace(
        '    return "TIKTOK"\n\n\ndef normalize_brand',
        '    # Owner-approved fallback: unknown/unmapped TikTok filenames belong to LPM.\n    return "LPM"\n\n\ndef normalize_brand',
    )

    # 2) Replace normalize_brand block and add explicit brand_group_from_shop_label helper.
    normalize_pattern = re.compile(
        r'def normalize_brand\(value: object\) -> str:\n'
        r'(?:    .*\n)+?'
        r'\n\ndef date_from_filename',
        re.MULTILINE,
    )
    normalize_replacement = '''def normalize_brand(value: object) -> str:
    """Normalize free-text shop/brand value with owner-approved fallback.

    Unknown or blank values default to LPM.
    """
    return brand_group_from_shop_label(value)


def brand_group_from_shop_label(value: object) -> str:
    """Map filename-derived shop_label/text to TikTok brand_group.

    Rules:
    - Any Sữa chua / Bledina marker -> SUA_CHUA.
    - Any LPM / Petit / Marseillais marker -> LPM.
    - Blank or unknown value -> LPM.
    """
    s = safe_text(value).upper()
    if "SUA" in s or "CHUA" in s or "BLÉDINA" in s or "BLEDINA" in s:
        return "SUA_CHUA"
    if "LPM" in s or "PETIT" in s or "MARSEILLAIS" in s:
        return "LPM"
    return "LPM"


def date_from_filename'''
    if 'def brand_group_from_shop_label(value: object) -> str:' not in s:
        s, n = normalize_pattern.subn(normalize_replacement, s, count=1)
        if n != 1:
            raise RuntimeError("Không vá được hàm normalize_brand / brand_group_from_shop_label.")

    # 3) Make get_tiktok_platform_fee_rate use the same brand mapping, with LPM fallback.
    s = re.sub(
        r'brand = safe_text\(brand_group\)\.upper\(\)\.strip\(\)\n\s*if brand != "LPM":\n\s*brand = "SUA_CHUA"',
        'brand = brand_group_from_shop_label(brand_group)',
        s,
        count=1,
    )

    # 4) Brand group must come from filename-derived shop_label, not category/master data.
    old_assignment = 'df_o["brand_group"] = df_o["cat_clean"].map(cat_map).fillna("").map(normalize_brand)'
    new_assignment = 'df_o["brand_group"] = df_o["__shop_label"].map(brand_group_from_shop_label)'
    if old_assignment in s:
        s = s.replace(old_assignment, new_assignment, 1)
    elif new_assignment not in s:
        raise RuntimeError("Không tìm thấy dòng gán brand_group cần vá.")

    comment = (
        '    # Owner-approved rule: brand_group follows shop_label, and shop_label is derived from filename.\n'
        '    # Unknown/unmapped filenames default to LPM via extract_shop_label().\n'
    )
    if comment not in s:
        s = s.replace('    ' + new_assignment + '\n', comment + '    ' + new_assignment + '\n', 1)

    TARGET.write_text(s, encoding="utf-8")
    print("OK: applied TikTok v4.12 brand_group-from-filename patch")
    print(f"Backup saved to: {backup}")


if __name__ == "__main__":
    patch_file()
