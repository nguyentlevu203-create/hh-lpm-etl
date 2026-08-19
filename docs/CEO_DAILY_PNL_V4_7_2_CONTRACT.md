# CEO Daily P&L v4.7.2 Contract

## Inherited unchanged

- P&L waterfall and final line LỢI NHUẬN/Profit.
- No EBITDA.
- Marketplace exact cancellation rules.
- Nhanh v4.6.1 normalized-exact status contract.
- Monthly target and achievement logic.
- Inventory snapshot rule: latest snapshot date <= report date; never future inventory.

## New blocks

### `mtgt_customer_mtd_all`
Grain: channel + customer key. Scope: all GT/MT customers with current-MTD transactions.

### `top_20_sku_by_channel`
Grain: channel + ranked SKU. Top 20 by MTD sold quantity, gift excluded from rank. Stock mapping is SKU-first.

### `inventory_expiry_alert_skus`
Grain: one existing inventory date/expiry alert row. Only rows with `earliest_expiry_date` are included.

## Renderer hard rules

1. GT/MT customer tables may show every row in the all-customer block; never substitute the legacy Top 5 block.
2. Top20 tables must use MTD sold quantity and the stock total across all lots/HSD for the mapped SKU.
3. Sheet 06 must not render the full inventory master. It shows only existing date/expiry alerts.
4. No invented near-expiry threshold and no inferred near-date pushed quantity.
