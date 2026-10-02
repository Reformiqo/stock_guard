-- UNDO for SEPL_LIVE_Match_Script.sql — restores every changed value from the zz_fix_bk_* backup
-- tables created by that script (PART B). Run only if the correction must be reversed.
SET autocommit=0;
START TRANSACTION;
UPDATE `tabStock Ledger Entry` s JOIN `zz_fix_bk_sle` b ON b.name=s.name
  SET s.qty_after_transaction=b.qty_after_transaction, s.stock_value=b.stock_value, s.valuation_rate=b.valuation_rate
  WHERE s.qty_after_transaction<>b.qty_after_transaction OR s.stock_value<>b.stock_value OR s.valuation_rate<>b.valuation_rate;
UPDATE `tabBin` s JOIN `zz_fix_bk_bin` b ON b.name=s.name
  SET s.actual_qty=b.actual_qty, s.projected_qty=b.projected_qty, s.stock_value=b.stock_value, s.valuation_rate=b.valuation_rate;
UPDATE `tabSerial and Batch Entry` s JOIN `zz_fix_bk_sbe` b ON b.name=s.name
  SET s.qty=b.qty, s.incoming_rate=b.incoming_rate, s.stock_value_difference=b.stock_value_difference;
UPDATE `tabSerial and Batch Bundle` s JOIN `zz_fix_bk_sbb` b ON b.name=s.name
  SET s.total_qty=b.total_qty, s.total_amount=b.total_amount, s.avg_rate=b.avg_rate;
UPDATE `tabBatch` s JOIN `zz_fix_bk_batch` b ON b.name=s.name SET s.batch_qty=b.batch_qty;
UPDATE `tabGL Entry` s JOIN `zz_fix_bk_gl` b ON b.name=s.name
  SET s.debit=b.debit, s.credit=b.credit, s.debit_in_account_currency=b.debit_in_account_currency, s.credit_in_account_currency=b.credit_in_account_currency,
      s.debit_in_transaction_currency=b.debit_in_transaction_currency, s.credit_in_transaction_currency=b.credit_in_transaction_currency,
      s.debit_in_reporting_currency=b.debit_in_reporting_currency, s.credit_in_reporting_currency=b.credit_in_reporting_currency;
DELETE FROM `tabGL Entry` WHERE `name` LIKE 'GLFIX-%' AND `remarks` IN ('Stock In Hand aligned to Stock Ledger value','Balancing row for Stock In Hand alignment');
UPDATE `tabRepost Item Valuation` s JOIN `zz_fix_bk_riv` b ON b.name=s.name SET s.status=b.status;
DELETE FROM `tabSingles` WHERE `doctype`='Stock Settings' AND `field` IN ('stock_frozen_upto','stock_auth_role');
UPDATE `tabScheduled Job Type` SET `stopped`=1 WHERE `method` LIKE '%repost_item_valuation.repost_item_valuation.%';
-- check, then COMMIT;  (or ROLLBACK;)   then: bench --site seplt.frappe.cloud clear-cache
