-- HH Control Tower Product Master Seed v1
-- Purpose:
--   Seed dim.product_master and dim.product_identifier_map from existing ETL tables.
--   Run after hh_control_tower_db_upgrade_v2.sql.
-- Safe to re-run: uses ON CONFLICT where applicable.

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Seed from dim.product_costs
-- -----------------------------------------------------------------------------
INSERT INTO dim.product_master (
    canonical_sku_code,
    product_name,
    is_active,
    created_at,
    updated_at
)
SELECT DISTINCT
    TRIM(ma)::text AS canonical_sku_code,
    COALESCE(NULLIF(TRIM(ma)::text, ''), 'Unknown Product') AS product_name,
    TRUE,
    now(),
    now()
FROM dim.product_costs
WHERE ma IS NOT NULL
  AND TRIM(ma)::text <> ''
ON CONFLICT ON CONSTRAINT uq_product_master_canonical_sku
DO UPDATE SET
    product_name = COALESCE(NULLIF(dim.product_master.product_name, ''), EXCLUDED.product_name),
    updated_at = now();

INSERT INTO dim.product_identifier_map (
    product_id,
    source_system,
    identifier_type,
    identifier_value,
    priority,
    is_active
)
SELECT
    pm.product_id,
    'GLOBAL',
    'SKU',
    TRIM(pc.ma)::text,
    10,
    TRUE
FROM dim.product_costs pc
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(TRIM(pc.ma)::text)
WHERE pc.ma IS NOT NULL
  AND TRIM(pc.ma)::text <> ''
ON CONFLICT DO NOTHING;

-- -----------------------------------------------------------------------------
-- 2. Seed MTGT
-- -----------------------------------------------------------------------------
INSERT INTO dim.product_master (
    canonical_sku_code,
    primary_ean,
    product_name,
    is_active,
    created_at,
    updated_at
)
SELECT DISTINCT
    TRIM(product_code)::text AS canonical_sku_code,
    NULLIF(TRIM(barcode)::text, '') AS primary_ean,
    COALESCE(NULLIF(TRIM(product_name)::text, ''), TRIM(product_code)::text) AS product_name,
    TRUE,
    now(),
    now()
FROM mtgt.sales_lines
WHERE product_code IS NOT NULL
  AND TRIM(product_code)::text <> ''
ON CONFLICT ON CONSTRAINT uq_product_master_canonical_sku
DO UPDATE SET
    primary_ean = COALESCE(dim.product_master.primary_ean, EXCLUDED.primary_ean),
    product_name = COALESCE(NULLIF(dim.product_master.product_name, ''), EXCLUDED.product_name),
    updated_at = now();

INSERT INTO dim.product_identifier_map (
    product_id,
    source_system,
    identifier_type,
    identifier_value,
    priority,
    is_active
)
SELECT DISTINCT
    pm.product_id,
    'MTGT',
    'PRODUCT_CODE',
    TRIM(s.product_code)::text,
    10,
    TRUE
FROM mtgt.sales_lines s
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(TRIM(s.product_code)::text)
WHERE s.product_code IS NOT NULL
  AND TRIM(s.product_code)::text <> ''
ON CONFLICT DO NOTHING;

INSERT INTO dim.product_identifier_map (
    product_id,
    source_system,
    identifier_type,
    identifier_value,
    priority,
    is_active
)
SELECT DISTINCT
    pm.product_id,
    'MTGT',
    'EAN',
    TRIM(s.barcode)::text,
    10,
    TRUE
FROM mtgt.sales_lines s
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(TRIM(s.product_code)::text)
WHERE s.product_code IS NOT NULL
  AND TRIM(s.product_code)::text <> ''
  AND s.barcode IS NOT NULL
  AND TRIM(s.barcode)::text <> ''
ON CONFLICT DO NOTHING;

-- -----------------------------------------------------------------------------
-- 3. Seed Nhanh / Digital
-- -----------------------------------------------------------------------------
WITH src AS (
    SELECT
        COALESCE(
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(product_code)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
        ) AS canonical_sku_code,
        NULLIF(TRIM(barcode)::text, '') AS primary_ean,
        COALESCE(
            NULLIF(TRIM(product_name)::text, ''),
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(product_code)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
        ) AS product_name
    FROM nhanh.order_items
    WHERE COALESCE(
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(product_code)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
          ) IS NOT NULL
),
dedup AS (
    SELECT DISTINCT ON (dim.norm_identifier(canonical_sku_code))
        canonical_sku_code,
        primary_ean,
        product_name
    FROM src
    ORDER BY
        dim.norm_identifier(canonical_sku_code),
        CASE WHEN primary_ean IS NOT NULL THEN 0 ELSE 1 END,
        LENGTH(COALESCE(product_name, '')) DESC
)
INSERT INTO dim.product_master (
    canonical_sku_code,
    primary_ean,
    product_name,
    is_active,
    created_at,
    updated_at
)
SELECT canonical_sku_code, primary_ean, product_name, TRUE, now(), now()
FROM dedup
ON CONFLICT ON CONSTRAINT uq_product_master_canonical_sku
DO UPDATE SET
    primary_ean = COALESCE(dim.product_master.primary_ean, EXCLUDED.primary_ean),
    product_name = COALESCE(NULLIF(dim.product_master.product_name, ''), EXCLUDED.product_name),
    updated_at = now();

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'NHANH', 'SKU', TRIM(i.ma_lookup)::text, 10, TRUE
FROM nhanh.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.product_code)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.ma_lookup IS NOT NULL AND TRIM(i.ma_lookup)::text <> ''
ON CONFLICT DO NOTHING;

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'NHANH', 'PRODUCT_CODE', TRIM(i.product_code)::text, 20, TRUE
FROM nhanh.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.product_code)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.product_code IS NOT NULL AND TRIM(i.product_code)::text <> ''
ON CONFLICT DO NOTHING;

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'NHANH', 'EAN', TRIM(i.barcode)::text, 30, TRUE
FROM nhanh.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.product_code)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.barcode IS NOT NULL AND TRIM(i.barcode)::text <> ''
ON CONFLICT DO NOTHING;

-- -----------------------------------------------------------------------------
-- 4. Seed Shopee
-- -----------------------------------------------------------------------------
WITH src AS (
    SELECT
        COALESCE(
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(seller_sku)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
        ) AS canonical_sku_code,
        NULLIF(TRIM(barcode)::text, '') AS primary_ean,
        COALESCE(
            NULLIF(TRIM(product_name)::text, ''),
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(seller_sku)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
        ) AS product_name
    FROM shopee.order_items
    WHERE COALESCE(
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(seller_sku)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
          ) IS NOT NULL
),
dedup AS (
    SELECT DISTINCT ON (dim.norm_identifier(canonical_sku_code))
        canonical_sku_code,
        primary_ean,
        product_name
    FROM src
    ORDER BY
        dim.norm_identifier(canonical_sku_code),
        CASE WHEN primary_ean IS NOT NULL THEN 0 ELSE 1 END,
        LENGTH(COALESCE(product_name, '')) DESC
)
INSERT INTO dim.product_master (canonical_sku_code, primary_ean, product_name, is_active, created_at, updated_at)
SELECT canonical_sku_code, primary_ean, product_name, TRUE, now(), now()
FROM dedup
ON CONFLICT ON CONSTRAINT uq_product_master_canonical_sku
DO UPDATE SET
    primary_ean = COALESCE(dim.product_master.primary_ean, EXCLUDED.primary_ean),
    product_name = COALESCE(NULLIF(dim.product_master.product_name, ''), EXCLUDED.product_name),
    updated_at = now();

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'SHOPEE', 'SKU', TRIM(i.ma_lookup)::text, 10, TRUE
FROM shopee.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.seller_sku)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.ma_lookup IS NOT NULL AND TRIM(i.ma_lookup)::text <> ''
ON CONFLICT DO NOTHING;

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'SHOPEE', 'SELLER_SKU', TRIM(i.seller_sku)::text, 20, TRUE
FROM shopee.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.seller_sku)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.seller_sku IS NOT NULL AND TRIM(i.seller_sku)::text <> ''
ON CONFLICT DO NOTHING;

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'SHOPEE', 'EAN', TRIM(i.barcode)::text, 30, TRUE
FROM shopee.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.seller_sku)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.barcode IS NOT NULL AND TRIM(i.barcode)::text <> ''
ON CONFLICT DO NOTHING;

-- -----------------------------------------------------------------------------
-- 5. Seed TikTok
-- -----------------------------------------------------------------------------
WITH src AS (
    SELECT
        COALESCE(
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(seller_sku)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
        ) AS canonical_sku_code,
        NULLIF(TRIM(barcode)::text, '') AS primary_ean,
        COALESCE(
            NULLIF(TRIM(product_name)::text, ''),
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(seller_sku)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
        ) AS product_name
    FROM tiktok.order_items
    WHERE COALESCE(
            NULLIF(TRIM(ma_lookup)::text, ''),
            NULLIF(TRIM(seller_sku)::text, ''),
            NULLIF(TRIM(barcode)::text, '')
          ) IS NOT NULL
),
dedup AS (
    SELECT DISTINCT ON (dim.norm_identifier(canonical_sku_code))
        canonical_sku_code,
        primary_ean,
        product_name
    FROM src
    ORDER BY
        dim.norm_identifier(canonical_sku_code),
        CASE WHEN primary_ean IS NOT NULL THEN 0 ELSE 1 END,
        LENGTH(COALESCE(product_name, '')) DESC
)
INSERT INTO dim.product_master (canonical_sku_code, primary_ean, product_name, is_active, created_at, updated_at)
SELECT canonical_sku_code, primary_ean, product_name, TRUE, now(), now()
FROM dedup
ON CONFLICT ON CONSTRAINT uq_product_master_canonical_sku
DO UPDATE SET
    primary_ean = COALESCE(dim.product_master.primary_ean, EXCLUDED.primary_ean),
    product_name = COALESCE(NULLIF(dim.product_master.product_name, ''), EXCLUDED.product_name),
    updated_at = now();

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'TIKTOK', 'SKU', TRIM(i.ma_lookup)::text, 10, TRUE
FROM tiktok.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.seller_sku)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.ma_lookup IS NOT NULL AND TRIM(i.ma_lookup)::text <> ''
ON CONFLICT DO NOTHING;

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'TIKTOK', 'SELLER_SKU', TRIM(i.seller_sku)::text, 20, TRUE
FROM tiktok.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.seller_sku)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.seller_sku IS NOT NULL AND TRIM(i.seller_sku)::text <> ''
ON CONFLICT DO NOTHING;

INSERT INTO dim.product_identifier_map (product_id, source_system, identifier_type, identifier_value, priority, is_active)
SELECT DISTINCT pm.product_id, 'TIKTOK', 'EAN', TRIM(i.barcode)::text, 30, TRUE
FROM tiktok.order_items i
JOIN dim.product_master pm
  ON pm.canonical_sku_norm = dim.norm_identifier(COALESCE(NULLIF(TRIM(i.ma_lookup)::text, ''), NULLIF(TRIM(i.seller_sku)::text, ''), NULLIF(TRIM(i.barcode)::text, '')))
WHERE i.barcode IS NOT NULL AND TRIM(i.barcode)::text <> ''
ON CONFLICT DO NOTHING;

COMMIT;
