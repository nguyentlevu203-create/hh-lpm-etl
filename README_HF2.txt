v4.7.4 HF2 - stale marketplace alias rebind
============================================

Purpose
-------
Resolve the systematic conflict where approved EAN/product_id is canonical but an older
SHOPEE/TIKTOK short-SKU alias points to a separate product_id created before the EAN
master was available.

HF2 never deletes or merges product_master rows. It only re-points exact active SKU alias
rows when the stale product has no contradictory EAN evidence.

1) Dry-run (required)
---------------------
python .\scripts\rebind_stale_product_aliases_v4_7_4_HF2.py `
  --file .\config\product_sku_alias_master_v4_7_4.csv `
  > .\outbox\audit\sku_alias_rebind_HF2_dryrun.json

$r = Get-Content .\outbox\audit\sku_alias_rebind_HF2_dryrun.json -Raw | ConvertFrom-Json
$r | Select-Object mode,mapping_rows,safe_rebind_count,blocked_conflict_count,unresolved_ean_count,can_apply
$r.blocked_conflicts | Format-List
$r.unresolved_eans | Format-List

Only continue if:
blocked_conflict_count = 0
unresolved_ean_count   = 0
can_apply              = True

2) Apply rebind
---------------
python .\scripts\rebind_stale_product_aliases_v4_7_4_HF2.py `
  --file .\config\product_sku_alias_master_v4_7_4.csv `
  --apply `
  > .\outbox\audit\sku_alias_rebind_HF2_apply.json

3) Re-run original v4.7.4 alias importer dry-run
------------------------------------------------
python .\scripts\import_product_sku_aliases_v4_7_4.py `
  --file .\config\product_sku_alias_master_v4_7_4.csv `
  > .\outbox\audit\sku_alias_dryrun_after_HF2.json

$r = Get-Content .\outbox\audit\sku_alias_dryrun_after_HF2.json -Raw | ConvertFrom-Json
$r | Select-Object mode,mapping_rows,new_aliases,existing_aliases,can_apply
$r.conflicts | Format-Table -AutoSize
$r.unresolved_eans | Format-Table -AutoSize

Only if can_apply=True, run the original importer with --apply.

Do NOT delete duplicate product_master rows in this HF. They can be audited and cleaned
separately after report reconciliation.
