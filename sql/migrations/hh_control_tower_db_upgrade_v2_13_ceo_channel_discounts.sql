BEGIN;

-- Shopee CEO Net Sales bridge.
ALTER TABLE shopee.order_financials_extra
    ADD COLUMN IF NOT EXISTS seller_subsidy_amount numeric NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS platform_subsidy_amount numeric NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_subsidy_amount numeric NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS shop_voucher_amount numeric NOT NULL DEFAULT 0;

-- TikTok seller-funded SKU discount at order and item levels.
ALTER TABLE tiktok.orders_pnl
    ADD COLUMN IF NOT EXISTS seller_discount_amount numeric NOT NULL DEFAULT 0;

ALTER TABLE tiktok.order_items
    ADD COLUMN IF NOT EXISTS seller_discount_amount numeric NOT NULL DEFAULT 0;

COMMIT;
