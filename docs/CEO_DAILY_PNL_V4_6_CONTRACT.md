# CEO Daily P&L v4.6 contract

## Status classification

| Channel | Cancelled exact status | Return/failure treatment |
|---|---|---|
| Shopee | `Đã hủy` | Other statuses are non-cancelled for this report |
| TikTok | `Canceled` | Other statuses are non-cancelled for this report |
| Nhanh | `Đã hủy`, `Hệ Thống hủy`, `Khách hủy`, `HVC hủy` | `Đang hoàn`, `Xác nhận hoàn`, `Đã hoàn`, `Thất bại` are not cancellation |

No fuzzy or substring matching is allowed for cancellation classification.

## Nhanh parent and leaf reporting

The package must expose both:

- Parent daily: `mtd_daily_detail_by_channel.NHANH` and alias `nhanh_parent_daily_detail`
- Leaf: `nhanh_leaf_channel_map`, `nhanh_leaf_daily_detail`

The leaf catalog remains exactly seven ETL-approved labels. `_UNALLOCATED` is internal only.

## P&L

No EBITDA. Final line is Profit / LỢI NHUẬN.

`Net Sales - sold_cogs - gift_cogs = GM1`

Then CM1 costs, CM2 costs, backoffice, Profit.
