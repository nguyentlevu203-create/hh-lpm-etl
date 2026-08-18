-- CEO Daily P&L v4.7 optional target schema.
-- Safe/idempotent: adds storage only; DOES NOT populate or invent target values.
-- Run only after review. The v4.7 builder works without these optional fields,
-- but target_cm2 / daily target fields will remain NULL and the renderer should hide them.

BEGIN;

ALTER TABLE IF EXISTS ops.monthly_sales_targets
    ADD COLUMN IF NOT EXISTS target_cm2 numeric,
    ADD COLUMN IF NOT EXISTS target_cm2_margin numeric;

CREATE TABLE IF NOT EXISTS ops.daily_sales_targets (
    target_date date NOT NULL,
    channel_code text NOT NULL,
    target_net_sales numeric,
    target_cm2 numeric,
    source text,
    note text,
    is_enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target_date, channel_code)
);

COMMENT ON TABLE ops.daily_sales_targets IS
'Optional CEO daily target allocation. Do not auto-fill by equal-day allocation unless business explicitly approves that policy.';
COMMENT ON COLUMN ops.monthly_sales_targets.target_cm2 IS
'Optional monthly CM2 target. NULL means not supplied; never infer from Profit.';
COMMENT ON COLUMN ops.monthly_sales_targets.target_cm2_margin IS
'Optional CM2 target margin as decimal ratio, e.g. 0.15 = 15%. NULL means not supplied.';

COMMIT;
