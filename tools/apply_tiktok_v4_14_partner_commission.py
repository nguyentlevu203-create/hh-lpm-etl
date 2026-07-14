#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch TikTok partner affiliate commission parsing.

This patch only replaces add_partner_commission() in scripts/tiktok_etl.py.
It preserves all other existing logic such as combo SKU COGS, platform fee rules,
ads/live parsing, settlement, and packaging.
"""
from __future__ import annotations

import re
from pathlib import Path

TARGET = Path("scripts/tiktok_etl.py")

NEW_FUNCTION = r'''def add_partner_commission(fact_df: pd.DataFrame) -> Tuple[pd.DataFrame, List[Tuple[Path, str]]]:
    """Load TikTok Partner affiliate commission from folder 3_affiliate_partner.

    Owner-approved production rule:
    - Order id column must be exactly one of:
        "Order ID", "Mã đơn hàng", "Mã đơn", "ID đơn hàng"
      after harmless whitespace/BOM cleanup.
    - Commission amount is the SUM of all existing approved amount columns:
        "Thanh toán hoa hồng ước tính"
        "Thanh toán hoa hồng Quảng cáo cửa hàng ước tính"
        "Hoa hồng ước tính cho Đối tác liên kết"
        "commission"
    - Do not use fuzzy columns such as "hoa hồng thực tế" unless the header is
      exactly one of the approved names above.
    """
    processed = []
    fact_df["comm_partner"] = 0.0

    order_id_headers = [
        "Order ID",
        "Mã đơn hàng",
        "Mã đơn",
        "ID đơn hàng",
    ]
    commission_headers = [
        "Thanh toán hoa hồng ước tính",
        "Thanh toán hoa hồng Quảng cáo cửa hàng ước tính",
        "Hoa hồng ước tính cho Đối tác liên kết",
        "commission",
    ]

    def _norm_header(value: object) -> str:
        return " ".join(str(value).replace("\ufeff", " ").replace("\n", " ").split()).strip().casefold()

    def _find_exact_one(columns, allowed_names: List[str], label: str, path: Path):
        mapping = {_norm_header(c): c for c in columns}
        for name in allowed_names:
            key = _norm_header(name)
            if key in mapping:
                return mapping[key]
        available = ", ".join(str(c) for c in columns)
        raise ValueError(
            f"File TikTok partner '{path.name}' thiếu cột {label}. "
            f"Chỉ chấp nhận đúng một trong {allowed_names}. Các cột hiện có: {available}"
        )

    def _find_exact_many(columns, allowed_names: List[str]) -> List[object]:
        wanted = {_norm_header(x) for x in allowed_names}
        found = []
        seen = set()
        for col in columns:
            key = _norm_header(col)
            if key in wanted and key not in seen:
                found.append(col)
                seen.add(key)
        return found

    for path in list_files("partner"):
        df = read_smart_table(
            path,
            [
                "Order ID",
                "Mã đơn hàng",
                "Mã đơn",
                "Thanh toán hoa hồng ước tính",
                "Thanh toán hoa hồng Quảng cáo cửa hàng ước tính",
                "Hoa hồng ước tính cho Đối tác liên kết",
                "commission",
            ],
        )
        if df.empty:
            continue

        pid = _find_exact_one(df.columns, order_id_headers, "mã đơn", path)
        commission_cols = _find_exact_many(df.columns, commission_headers)
        if not commission_cols:
            available = ", ".join(str(c) for c in df.columns)
            raise ValueError(
                f"File TikTok partner '{path.name}' thiếu cột hoa hồng partner hợp lệ. "
                f"Chỉ chấp nhận đúng các cột {commission_headers}. Các cột hiện có: {available}"
            )

        df["order_id"] = df[pid].map(clean_code)
        df = df[df["order_id"] != ""].copy()
        if df.empty:
            processed.append((path, "partner"))
            print(f"⚠️ TikTok partner {path.name}: không có mã đơn hợp lệ")
            continue

        total_commission = pd.Series(0.0, index=df.index)
        for col in commission_cols:
            total_commission = total_commission.add(money_series(df[col]).astype(float), fill_value=0)

        df["partner_commission"] = total_commission
        grouped = df.groupby("order_id", as_index=False)["partner_commission"].sum()

        fact_df = fact_df.merge(grouped, on="order_id", how="left")
        fact_df["comm_partner"] = fact_df["comm_partner"] + fact_df["partner_commission"].fillna(0)
        fact_df.drop(columns=["partner_commission"], inplace=True)

        processed.append((path, "partner"))
        print(
            f"✅ TikTok partner {path.name}: {len(grouped)} đơn, "
            f"columns={'; '.join(str(c) for c in commission_cols)}"
        )
    return fact_df, processed
'''


def main() -> int:
    if not TARGET.exists():
        raise FileNotFoundError(f"Không tìm thấy {TARGET}. Hãy chạy script này tại thư mục gốc project ETL.")

    text = TARGET.read_text(encoding="utf-8")
    backup = TARGET.with_suffix(".py.bak_v4_14_partner_commission")
    backup.write_text(text, encoding="utf-8")

    pattern = r"def add_partner_commission\(fact_df: pd\.DataFrame\) -> Tuple\[pd\.DataFrame, List\[Tuple\[Path, str\]\]\]:.*?\n\ndef add_settlement"
    replacement = NEW_FUNCTION + "\n\ndef add_settlement"
    new_text, count = re.subn(pattern, lambda _m: replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError("Không tìm thấy đúng hàm add_partner_commission() để vá. File có thể đã khác cấu trúc dự kiến.")

    TARGET.write_text(new_text, encoding="utf-8")
    print("OK: patched scripts/tiktok_etl.py partner commission parser")
    print(f"Backup saved to: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
