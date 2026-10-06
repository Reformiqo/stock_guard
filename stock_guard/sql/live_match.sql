-- =====================================================================================
-- SEPL / SEPLU  —  LIVE MATCH SCRIPT
-- Makes Stock Ledger (closing) = Bin = Stock Balance report = GL Stock In Hand = Batch
--
-- LIVE: this script reads the data AT THE MOMENT IT RUNS. It does not depend on any
-- earlier snapshot, so nothing posted before it runs is missed.
--
-- Basis (unchanged by this script): Stock Ledger Entry.actual_qty and
-- Stock Ledger Entry.stock_value_difference. These already equal the Stock Balance report
-- and GL. Everything else (running qty/value, Bin, batch rows, batch master, GL paise) is
-- aligned to them.
--
-- Temporary-table indexes are declared inside CREATE TEMPORARY TABLE (no ALTER TABLE),
-- because ALTER TABLE on a temporary table makes MariaDB commit the open transaction.
--
-- No Journal Entries. Tested on MariaDB 10.6+ (site runs 10.6.25).
--
-- HOW TO RUN (one interactive session; site name on the bench is seplt.frappe.cloud,
-- sepl.erpera.io is only its domain):
--   1. Ask users to stop posting, then take a backup:
--        bench --site seplt.frappe.cloud backup --with-files
--      (Part A of this script sets the stock freeze itself.)
--   2. Open the database console and run the script with "source", not with "<":
--        bench --site seplt.frappe.cloud mariadb
--        source /path/SEPL_LIVE_Match_Script.sql
--      The script ends WITHOUT committing Part C. Read the PART D results, then type
--        COMMIT;   (if every difference is 0 / within 1 rupee)
--        ROLLBACK; (otherwise)
--   3. After COMMIT, in the same console:  source /path/SEPL_LIVE_Post_Commit.sql
--      then:  bench --site seplt.frappe.cloud clear-cache
-- =====================================================================================

SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION';
SET @run_at = NOW() + INTERVAL 330 MINUTE;   -- site data is stored in IST; DB server clock is UTC
-- C6 settings. Stock Guard sets @sg_clear_residue (0/1) before running; from a console it defaults to 1.
-- @sg_closed_upto = Stock Frozen Up To as it was BEFORE Part A, so closed months are never touched.
SET @sg_clear_residue = IFNULL(@sg_clear_residue, 1);
SET @sg_closed_upto = COALESCE((SELECT `value` FROM `tabSingles` WHERE `doctype`='Stock Settings' AND `field`='stock_frozen_upto' AND IFNULL(`value`,'')<>'' LIMIT 1), '1900-01-01');

-- -------------------------------------------------------------------------------------
-- PART A — Freeze stock posting up to today (committed immediately so users are blocked)
-- Also set the same values in the UI (Stock Settings) or run clear-cache, because
-- Frappe caches Singles.
-- -------------------------------------------------------------------------------------
DELETE FROM `tabSingles` WHERE `doctype`='Stock Settings' AND `field` IN ('stock_frozen_upto','stock_auth_role');
INSERT INTO `tabSingles` (`doctype`,`field`,`value`) VALUES
  ('Stock Settings','stock_frozen_upto', DATE_FORMAT(@run_at,'%Y-%m-%d')),
  ('Stock Settings','stock_auth_role','System Manager');
COMMIT;

-- -------------------------------------------------------------------------------------
-- PART B — Backup of every column this script changes (for UNDO). DDL auto-commits,
-- so it runs before the transaction.
-- -------------------------------------------------------------------------------------
DROP TABLE IF EXISTS `zz_fix_bk_sle`, `zz_fix_bk_bin`, `zz_fix_bk_sbe`, `zz_fix_bk_sbb`, `zz_fix_bk_batch`, `zz_fix_bk_gl`, `zz_fix_bk_riv`, `zz_fix_bk_run`;
CREATE TABLE `zz_fix_bk_sle`   AS SELECT `name`,`qty_after_transaction`,`stock_value`,`valuation_rate`,`stock_value_difference` FROM `tabStock Ledger Entry` WHERE `is_cancelled`=0;
CREATE TABLE `zz_fix_bk_bin`   AS SELECT `name`,`actual_qty`,`projected_qty`,`stock_value`,`valuation_rate` FROM `tabBin`;
CREATE TABLE `zz_fix_bk_sbe`   AS SELECT `name`,`qty`,`incoming_rate`,`stock_value_difference` FROM `tabSerial and Batch Entry`;
CREATE TABLE `zz_fix_bk_sbb`   AS SELECT `name`,`total_qty`,`total_amount`,`avg_rate` FROM `tabSerial and Batch Bundle`;
CREATE TABLE `zz_fix_bk_batch` AS SELECT `name`,`batch_qty` FROM `tabBatch`;
CREATE TABLE `zz_fix_bk_gl`    AS SELECT `name`,`debit`,`credit`,`debit_in_account_currency`,`credit_in_account_currency`,`debit_in_transaction_currency`,`credit_in_transaction_currency`,`debit_in_reporting_currency`,`credit_in_reporting_currency` FROM `tabGL Entry` WHERE `is_cancelled`=0;
CREATE TABLE `zz_fix_bk_riv`   AS SELECT `name`,`status` FROM `tabRepost Item Valuation` WHERE `docstatus`=1 AND `status` IN ('Queued','In Progress','Failed');
CREATE TABLE `zz_fix_bk_run`   AS SELECT @run_at AS `run_at`;
ALTER TABLE `zz_fix_bk_sle` ADD PRIMARY KEY (`name`);
ALTER TABLE `zz_fix_bk_gl`  ADD PRIMARY KEY (`name`);

-- PRE-CHECK: position before the fix (for the record)
SELECT 'BEFORE' AS stage, x.company,
       ROUND(SUM(x.lv),2) AS ledger_closing, ROUND(SUM(x.sv),2) AS stock_balance
FROM (SELECT s.company, s.item_code, s.warehouse, SUM(s.stock_value_difference) sv,
             (SELECT l.stock_value FROM `tabStock Ledger Entry` l WHERE l.is_cancelled=0 AND l.item_code=s.item_code AND l.warehouse=s.warehouse ORDER BY l.posting_datetime DESC, l.creation DESC LIMIT 1) lv
      FROM `tabStock Ledger Entry` s WHERE s.is_cancelled=0 GROUP BY s.company, s.item_code, s.warehouse) x
GROUP BY x.company;

-- =====================================================================================
-- PART C — Corrections (one transaction)
-- =====================================================================================
SET autocommit = 0;
START TRANSACTION;

-- C6. Value residue on batches with zero qty (batch-wise valuation). When a batch is fully
--     issued, its value should be 0 too. Rounding and back-dated rate changes can leave a value
--     (often negative) on a batch whose qty is 0. That residue sits inside Stock Balance and GL.
--     It is cleared on the LAST outward movement of that batch in that warehouse:
--       batch row value  := value - residue      ledger line value := value - residue
--     and a Stock In Hand / Stock Adjustment GL pair is added on the same voucher and date.
--     C1 then rebuilds the running value, C3b the bundle header. Movements dated on or before
--     the original Stock Frozen Up To date are not changed; they are only listed.
--     Runs only when @sg_clear_residue = 1.
CREATE TEMPORARY TABLE `tmp_res` (INDEX (batch_no, warehouse)) AS
SELECT sle.company, sbe.batch_no, sle.item_code, sle.warehouse,
       ROUND(SUM(sbe.qty), 6) bq, ROUND(SUM(sbe.stock_value_difference), 6) residue
FROM `tabStock Ledger Entry` sle
JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sle.serial_and_batch_bundle
JOIN `tabBatch` b ON b.name = sbe.batch_no AND b.use_batchwise_valuation = 1
WHERE sle.is_cancelled = 0 AND IFNULL(sbe.batch_no,'') <> '' AND @sg_clear_residue = 1
GROUP BY sle.company, sbe.batch_no, sle.item_code, sle.warehouse
HAVING ABS(SUM(sbe.qty)) <= 0.0001 AND ABS(SUM(sbe.stock_value_difference)) > 0.005;

CREATE TEMPORARY TABLE `tmp_rest` (INDEX (sbe_name), INDEX (sle_name)) AS
SELECT * FROM (
  SELECT r.company, r.batch_no, r.item_code, r.warehouse, r.residue,
         sbe.name sbe_name, sbe.qty sbe_qty, sbe.stock_value_difference sbe_svd,
         sle.name sle_name, sle.voucher_type, sle.voucher_no, sle.posting_date pd,
         ROW_NUMBER() OVER (PARTITION BY r.batch_no, r.warehouse
                            ORDER BY sle.posting_datetime DESC, sle.creation DESC, sle.name DESC) rn
  FROM `tmp_res` r
  JOIN `tabStock Ledger Entry` sle ON sle.item_code = r.item_code AND sle.warehouse = r.warehouse AND sle.is_cancelled = 0
  JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sle.serial_and_batch_bundle AND sbe.batch_no = r.batch_no
  WHERE sbe.qty < 0) t
WHERE rn = 1;

SELECT 'C6 zero-qty batch residue NOT cleared (last outward movement is in a closed period)' AS info,
       company, item_code, batch_no, warehouse, voucher_no, pd AS posting_date, ROUND(residue,2) AS residue
FROM `tmp_rest` WHERE pd <= @sg_closed_upto;
SELECT 'C6 zero-qty batch residue NOT cleared (batch has no outward movement)' AS info,
       r.company, r.item_code, r.batch_no, r.warehouse, ROUND(r.residue,2) AS residue
FROM `tmp_res` r WHERE NOT EXISTS (SELECT 1 FROM `tmp_rest` t WHERE t.batch_no = r.batch_no AND t.warehouse = r.warehouse);
DELETE FROM `tmp_rest` WHERE pd <= @sg_closed_upto;

-- one ledger line can carry several cleared batches: total per line first
CREATE TEMPORARY TABLE `tmp_resl` (PRIMARY KEY (sle_name)) AS
SELECT sle_name, MAX(company) company, MAX(voucher_type) voucher_type, MAX(voucher_no) voucher_no, MAX(pd) pd,
       ROUND(SUM(residue), 6) residue, COUNT(*) batches
FROM `tmp_rest` GROUP BY sle_name;

SELECT 'C6 zero-qty batch residue cleared' AS step, company, COUNT(*) AS batches, ROUND(SUM(residue),2) AS residue_cleared,
       ROUND(-SUM(residue),2) AS stock_value_change
FROM `tmp_rest` GROUP BY company;

UPDATE `tabSerial and Batch Entry` e
JOIN `tmp_rest` t ON t.sbe_name = e.name
SET e.stock_value_difference = ROUND(t.sbe_svd - t.residue, 9),
    e.incoming_rate          = ROUND(ABS((t.sbe_svd - t.residue) / t.sbe_qty), 9);
SELECT 'C6a Serial and Batch Entry rows corrected' AS step, ROW_COUNT() AS rows_changed;

UPDATE `tabStock Ledger Entry` sle
JOIN `tmp_resl` l ON l.sle_name = sle.name
SET sle.stock_value_difference = ROUND(sle.stock_value_difference - l.residue, 9);
SELECT 'C6b Stock Ledger Entry values corrected' AS step, ROW_COUNT() AS rows_changed;

-- Stock In Hand / Stock Adjustment pair per voucher: Stock In Hand moves by -residue
CREATE TEMPORARY TABLE `tmp_resv` AS
SELECT company, voucher_type, voucher_no, MAX(pd) pd, ROUND(-SUM(residue), 2) diff
FROM `tmp_resl` GROUP BY company, voucher_type, voucher_no HAVING ABS(ROUND(-SUM(residue), 2)) >= 0.01;

INSERT INTO `tabGL Entry` (`name`,`creation`,`modified`,`owner`,`modified_by`,`docstatus`,`idx`,`posting_date`,`transaction_date`,`fiscal_year`,
  `account`,`account_currency`,`voucher_type`,`voucher_no`,`transaction_currency`,`transaction_exchange_rate`,
  `debit`,`debit_in_account_currency`,`debit_in_transaction_currency`,`credit`,`credit_in_account_currency`,`credit_in_transaction_currency`,
  `cost_center`,`company`,`is_opening`,`is_advance`,`is_cancelled`,`to_rename`,`remarks`)
SELECT CONCAT('GLRES-', LEFT(MD5(CONCAT(r.voucher_no, side.s, @run_at)), 16)), @run_at, @run_at, 'Administrator', 'Administrator', 1, 0, r.pd, r.pd,
  (SELECT fy.name FROM `tabFiscal Year` fy WHERE r.pd BETWEEN fy.year_start_date AND fy.year_end_date LIMIT 1),
  IF(side.s='STK', IF(r.company LIKE '%UNIT-I', 'Stock In Hand - SEPLU', 'Stock In Hand - SEPL'),
                   IF(r.company LIKE '%UNIT-I', 'Stock Adjustment - SEPLU', 'Stock Adjustment - SEPL')),
  'INR', r.voucher_type, r.voucher_no, 'INR', 1,
  IF((side.s='STK') = (r.diff > 0), ABS(r.diff), 0), IF((side.s='STK') = (r.diff > 0), ABS(r.diff), 0), IF((side.s='STK') = (r.diff > 0), ABS(r.diff), 0),
  IF((side.s='STK') = (r.diff > 0), 0, ABS(r.diff)), IF((side.s='STK') = (r.diff > 0), 0, ABS(r.diff)), IF((side.s='STK') = (r.diff > 0), 0, ABS(r.diff)),
  (SELECT c.cost_center FROM `tabCompany` c WHERE c.name = r.company), r.company, 'No', 'No', 0, 0,
  'Stock Guard value residue clearance'
FROM `tmp_resv` r JOIN (SELECT 'STK' s UNION ALL SELECT 'ADJ') side;
SELECT 'C6c GL rows inserted (value residue clearance)' AS step, ROW_COUNT() AS rows_changed;

-- C1. Stock Ledger Entry: running qty / value / rate = running sum of actual_qty and
--     stock_value_difference, in ERPNext posting order (posting_datetime, creation).
CREATE TEMPORARY TABLE `tmp_brk` (INDEX (item_code, warehouse)) AS
SELECT DISTINCT item_code, warehouse FROM (
  SELECT item_code, warehouse, stock_value, stock_value_difference, actual_qty, qty_after_transaction,
         LAG(stock_value) OVER w pv, LAG(qty_after_transaction) OVER w pq
  FROM `tabStock Ledger Entry` WHERE is_cancelled=0
  WINDOW w AS (PARTITION BY item_code, warehouse ORDER BY posting_datetime, creation, name)) t
WHERE ABS(stock_value-IFNULL(pv,0)-stock_value_difference) > 0.01 OR ABS(qty_after_transaction-IFNULL(pq,0)-actual_qty) > 0.0001;
SELECT 'C1 item-warehouses with a running break' AS step, COUNT(*) AS pairs FROM `tmp_brk`;

CREATE TEMPORARY TABLE `tmp_run` (PRIMARY KEY (`name`)) AS
SELECT `name`,
       SUM(`actual_qty`)             OVER w AS rq,
       SUM(`stock_value_difference`) OVER w AS rv
FROM `tabStock Ledger Entry` s
WHERE s.`is_cancelled`=0 AND EXISTS (SELECT 1 FROM `tmp_brk` k WHERE k.item_code=s.item_code AND k.warehouse=s.warehouse)
WINDOW w AS (PARTITION BY `item_code`,`warehouse` ORDER BY `posting_datetime`,`creation`,`name` ROWS UNBOUNDED PRECEDING);

UPDATE `tabStock Ledger Entry` sle
JOIN `tmp_run` r ON r.`name` = sle.`name`
SET sle.`qty_after_transaction` = ROUND(r.rq, 9),
    sle.`stock_value`           = ROUND(r.rv, 9),
    sle.`valuation_rate`        = IF(r.rq > 0.0001, ROUND(r.rv / r.rq, 9), sle.`valuation_rate`)
WHERE ABS(sle.`qty_after_transaction` - r.rq) > 0.000001
   OR ABS(sle.`stock_value` - r.rv) > 0.000001;
SELECT 'C1 Stock Ledger Entry rows corrected' AS step, ROW_COUNT() AS rows_changed;

-- C2. Bin = last Stock Ledger Entry of each item-warehouse (projected_qty moves with actual_qty)
CREATE TEMPORARY TABLE `tmp_last` (INDEX (item_code, warehouse)) AS
SELECT item_code, warehouse, qty_after_transaction lq, stock_value lv, valuation_rate lr FROM (
  SELECT item_code, warehouse, qty_after_transaction, stock_value, valuation_rate,
         ROW_NUMBER() OVER (PARTITION BY item_code, warehouse ORDER BY posting_datetime DESC, creation DESC, name DESC) rn
  FROM `tabStock Ledger Entry` WHERE is_cancelled=0) t WHERE rn=1;

UPDATE `tabBin` b
JOIN `tmp_last` l ON l.item_code=b.item_code AND l.warehouse=b.warehouse
SET b.`projected_qty`  = b.`projected_qty` + (l.lq - b.`actual_qty`),
    b.`actual_qty`     = l.lq,
    b.`stock_value`    = l.lv,
    b.`valuation_rate` = l.lr
WHERE ABS(b.`actual_qty`-l.lq) > 0.0001 OR ABS(b.`stock_value`-l.lv) > 0.01 OR ABS(b.`valuation_rate`-l.lr) > 0.000001;
SELECT 'C2 Bin rows corrected' AS step, ROW_COUNT() AS rows_changed;

-- C3a. Batch rows: value and rate follow the Stock Ledger line (only where batch qty = ledger qty)
CREATE TEMPORARY TABLE `tmp_bund` (INDEX (sbb)) AS
SELECT sle.`serial_and_batch_bundle` sbb, sle.`actual_qty` aq, sle.`stock_value_difference` svd,
       SUM(sbe.`qty`) bq, SUM(sbe.`stock_value_difference`) bv
FROM `tabStock Ledger Entry` sle
JOIN `tabSerial and Batch Entry` sbe ON sbe.`parent` = sle.`serial_and_batch_bundle`
WHERE sle.`is_cancelled`=0 AND IFNULL(sle.`serial_and_batch_bundle`,'')<>''
GROUP BY sle.`name`, sle.`serial_and_batch_bundle`, sle.`actual_qty`, sle.`stock_value_difference`;

UPDATE `tabSerial and Batch Entry` e
JOIN `tmp_bund` t ON t.sbb = e.`parent`
SET e.`stock_value_difference` = ROUND(t.svd * e.`qty` / t.aq, 9),
    e.`incoming_rate`          = ROUND(ABS(t.svd / t.aq), 9)
WHERE t.aq <> 0 AND ABS(t.aq - t.bq) <= 0.001 AND ABS(t.svd - t.bv) > 0.01;
SELECT 'C3a Serial and Batch Entry rows corrected' AS step, ROW_COUNT() AS rows_changed;

-- C3b. Bundle header totals follow the ledger value (stored sign kept)
UPDATE `tabSerial and Batch Bundle` h
JOIN `tmp_bund` t ON t.sbb = h.`name`
SET h.`total_amount` = IF(h.`total_amount` < 0, -1, 1) * ROUND(ABS(t.svd), 9),
    h.`avg_rate`     = ROUND(ABS(t.svd / t.aq), 9)
WHERE t.aq <> 0 AND ABS(t.aq - t.bq) <= 0.001
  AND (ABS(ABS(h.`total_amount`) - ABS(t.svd)) > 0.01 OR ABS(h.`avg_rate` - ABS(t.svd / t.aq)) > 0.000001);
SELECT 'C3b Serial and Batch Bundle headers corrected' AS step, ROW_COUNT() AS rows_changed;

-- C3c. Batch master batch_qty = batch balance in the ledger
UPDATE `tabBatch` b
LEFT JOIN (SELECT sbe.`batch_no`, SUM(sbe.`qty`) led
           FROM `tabStock Ledger Entry` sle JOIN `tabSerial and Batch Entry` sbe ON sbe.`parent`=sle.`serial_and_batch_bundle`
           WHERE sle.`is_cancelled`=0 GROUP BY sbe.`batch_no`) x ON x.`batch_no` = b.`name`
SET b.`batch_qty` = ROUND(IFNULL(x.led,0), 9)
WHERE ABS(IFNULL(x.led,0) - IFNULL(b.`batch_qty`,0)) > 0.001;
SELECT 'C3c Batch master rows corrected' AS step, ROW_COUNT() AS rows_changed;

-- C4. GL Stock In Hand = Stock Ledger value on every voucher (paise). The largest
--     Stock In Hand row absorbs the difference; the largest other row balances it.
CREATE TEMPORARY TABLE `tmp_gld` AS
SELECT c company, vt voucher_type, vn voucher_no, ROUND(SUM(sv) - SUM(gv), 2) diff, MAX(pd) pd FROM (
  SELECT company c, voucher_type vt, voucher_no vn, SUM(stock_value_difference) sv, 0 gv, MAX(posting_date) pd
  FROM `tabStock Ledger Entry` WHERE is_cancelled=0 GROUP BY company, voucher_type, voucher_no
  UNION ALL
  SELECT company, voucher_type, voucher_no, 0, SUM(debit-credit), MAX(posting_date)
  FROM `tabGL Entry` WHERE is_cancelled=0 AND account IN ('Stock In Hand - SEPL','Stock In Hand - SEPLU')
  GROUP BY company, voucher_type, voucher_no) u
GROUP BY c, vt, vn HAVING ABS(SUM(sv) - SUM(gv)) >= 0.005;
SELECT 'C4 vouchers with GL <> ledger by MORE than 1 rupee (NOT auto-fixed - send this list)' AS info, company, voucher_type, voucher_no, diff FROM `tmp_gld` WHERE ABS(diff) > 1;
DELETE FROM `tmp_gld` WHERE ABS(diff) > 1;

CREATE TEMPORARY TABLE `tmp_glr` AS
SELECT d.*,
  (SELECT g.name FROM `tabGL Entry` g WHERE g.voucher_no=d.voucher_no AND g.is_cancelled=0
     AND g.account IN ('Stock In Hand - SEPL','Stock In Hand - SEPLU') ORDER BY ABS(g.debit-g.credit) DESC, g.name LIMIT 1) stock_row,
  (SELECT g.name FROM `tabGL Entry` g WHERE g.voucher_no=d.voucher_no AND g.is_cancelled=0
     AND g.account NOT IN ('Stock In Hand - SEPL','Stock In Hand - SEPLU') ORDER BY ABS(g.debit-g.credit) DESC, g.name LIMIT 1) bal_row
FROM `tmp_gld` d;

-- new debit/credit worked out first (multi-table UPDATE does not guarantee column order)
CREATE TEMPORARY TABLE `tmp_glnew` (PRIMARY KEY (`name`)) AS
SELECT g.name,
       IF(g.debit > 0 OR g.credit = 0, g.debit + r.diff, g.debit)   AS nd,
       IF(g.debit > 0 OR g.credit = 0, g.credit, g.credit - r.diff) AS nc
FROM `tabGL Entry` g JOIN `tmp_glr` r ON r.stock_row = g.name
UNION ALL
SELECT g.name,
       IF(g.credit > 0 OR g.debit = 0, g.debit, g.debit - r.diff)   AS nd,
       IF(g.credit > 0 OR g.debit = 0, g.credit + r.diff, g.credit) AS nc
FROM `tabGL Entry` g JOIN `tmp_glr` r ON r.bal_row = g.name AND r.stock_row IS NOT NULL;

UPDATE `tabGL Entry` g JOIN `tmp_glnew` n ON n.name = g.name
SET g.debit_in_reporting_currency   = IF(g.debit_in_reporting_currency  = 0, 0, n.nd * IF(g.reporting_currency_exchange_rate = 0, 1, g.reporting_currency_exchange_rate)),
    g.credit_in_reporting_currency  = IF(g.credit_in_reporting_currency = 0, 0, n.nc * IF(g.reporting_currency_exchange_rate = 0, 1, g.reporting_currency_exchange_rate)),
    g.debit  = n.nd, g.debit_in_account_currency  = n.nd, g.debit_in_transaction_currency  = n.nd,
    g.credit = n.nc, g.credit_in_account_currency = n.nc, g.credit_in_transaction_currency = n.nc;
SELECT 'C4a GL rows corrected (Stock In Hand row + balancing row)' AS step, ROW_COUNT() AS rows_changed;

-- vouchers whose GL has only Stock In Hand rows: add one Stock Adjustment row to keep the voucher balanced
INSERT INTO `tabGL Entry` (`name`,`creation`,`modified`,`owner`,`modified_by`,`docstatus`,`idx`,`posting_date`,`transaction_date`,`fiscal_year`,
  `account`,`account_currency`,`voucher_type`,`voucher_no`,`transaction_currency`,`transaction_exchange_rate`,
  `debit`,`debit_in_account_currency`,`debit_in_transaction_currency`,`credit`,`credit_in_account_currency`,`credit_in_transaction_currency`,
  `cost_center`,`company`,`is_opening`,`is_advance`,`is_cancelled`,`to_rename`,`remarks`)
SELECT CONCAT('GLFIX-', LEFT(MD5(CONCAT(r.voucher_no,'BAL')), 16)), @run_at, @run_at, 'Administrator', 'Administrator', 1, 0, r.pd, r.pd,
  (SELECT fy.name FROM `tabFiscal Year` fy WHERE r.pd BETWEEN fy.year_start_date AND fy.year_end_date LIMIT 1),
  IF(r.company LIKE '%UNIT-I', 'Stock Adjustment - SEPLU', 'Stock Adjustment - SEPL'), 'INR', r.voucher_type, r.voucher_no, 'INR', 1,
  IF(r.diff < 0, ABS(r.diff), 0), IF(r.diff < 0, ABS(r.diff), 0), IF(r.diff < 0, ABS(r.diff), 0),
  IF(r.diff > 0, ABS(r.diff), 0), IF(r.diff > 0, ABS(r.diff), 0), IF(r.diff > 0, ABS(r.diff), 0),
  (SELECT c.cost_center FROM `tabCompany` c WHERE c.name = r.company), r.company, 'No', 'No', 0, 0,
  'Balancing row for Stock In Hand alignment'
FROM `tmp_glr` r WHERE r.stock_row IS NOT NULL AND r.bal_row IS NULL;
SELECT 'C4b2 GL balancing rows inserted (stock-only vouchers)' AS step, ROW_COUNT() AS rows_changed;

-- vouchers with ledger value but no Stock In Hand GL row: add a Stock In Hand / Stock Adjustment pair
INSERT INTO `tabGL Entry` (`name`,`creation`,`modified`,`owner`,`modified_by`,`docstatus`,`idx`,`posting_date`,`transaction_date`,`fiscal_year`,
  `account`,`account_currency`,`voucher_type`,`voucher_no`,`transaction_currency`,`transaction_exchange_rate`,
  `debit`,`debit_in_account_currency`,`debit_in_transaction_currency`,`credit`,`credit_in_account_currency`,`credit_in_transaction_currency`,
  `cost_center`,`company`,`is_opening`,`is_advance`,`is_cancelled`,`to_rename`,`remarks`)
SELECT CONCAT('GLFIX-', LEFT(MD5(CONCAT(r.voucher_no, side.s)), 16)), @run_at, @run_at, 'Administrator', 'Administrator', 1, 0, r.pd, r.pd,
  (SELECT fy.name FROM `tabFiscal Year` fy WHERE r.pd BETWEEN fy.year_start_date AND fy.year_end_date LIMIT 1),
  IF(side.s='STK', IF(r.company LIKE '%UNIT-I', 'Stock In Hand - SEPLU', 'Stock In Hand - SEPL'),
                   IF(r.company LIKE '%UNIT-I', 'Stock Adjustment - SEPLU', 'Stock Adjustment - SEPL')),
  'INR', r.voucher_type, r.voucher_no, 'INR', 1,
  IF((side.s='STK') = (r.diff > 0), ABS(r.diff), 0), IF((side.s='STK') = (r.diff > 0), ABS(r.diff), 0), IF((side.s='STK') = (r.diff > 0), ABS(r.diff), 0),
  IF((side.s='STK') = (r.diff > 0), 0, ABS(r.diff)), IF((side.s='STK') = (r.diff > 0), 0, ABS(r.diff)), IF((side.s='STK') = (r.diff > 0), 0, ABS(r.diff)),
  (SELECT c.cost_center FROM `tabCompany` c WHERE c.name = r.company), r.company, 'No', 'No', 0, 0,
  'Stock In Hand aligned to Stock Ledger value'
FROM `tmp_glr` r JOIN (SELECT 'STK' s UNION ALL SELECT 'ADJ') side
WHERE r.stock_row IS NULL;
SELECT 'C4c GL rows inserted (vouchers without Stock In Hand row)' AS step, ROW_COUNT() AS rows_changed;

-- C5. Close the old recalculation queue (superseded by C1-C4)
UPDATE `tabRepost Item Valuation` SET `status`='Skipped'
WHERE `docstatus`=1 AND `status` IN ('Queued','In Progress','Failed') AND `creation` <= @run_at;
SELECT 'C5 Repost Item Valuation set to Skipped' AS step, ROW_COUNT() AS rows_changed;

-- =====================================================================================
-- PART D — VERIFY (inside the transaction). Expected: every difference 0 (value ±1 rupee).
-- =====================================================================================
DROP TEMPORARY TABLE IF EXISTS `tmp_last2`;
CREATE TEMPORARY TABLE `tmp_last2` (INDEX (item_code, warehouse)) AS
SELECT item_code, warehouse, stock_value lv FROM (
  SELECT item_code, warehouse, stock_value, ROW_NUMBER() OVER (PARTITION BY item_code, warehouse ORDER BY posting_datetime DESC, creation DESC, name DESC) rn
  FROM `tabStock Ledger Entry` WHERE is_cancelled=0) z WHERE rn=1;
SELECT 'D1 value' AS chk, x.company,
       ROUND(SUM(x.lv),2) ledger_closing, ROUND(SUM(x.bv),2) bin_value, ROUND(SUM(x.sv),2) stock_balance,
       ROUND(SUM(x.lv)-SUM(x.sv),2) ledger_minus_stock_balance, ROUND(SUM(x.bv)-SUM(x.lv),2) bin_minus_ledger
FROM (SELECT s.company, s.item_code, s.warehouse, SUM(s.stock_value_difference) sv, MAX(l.lv) lv, MAX(IFNULL(b.stock_value,0)) bv
      FROM `tabStock Ledger Entry` s
      JOIN `tmp_last2` l ON l.item_code=s.item_code AND l.warehouse=s.warehouse
      LEFT JOIN `tabBin` b ON b.item_code=s.item_code AND b.warehouse=s.warehouse
      WHERE s.is_cancelled=0 GROUP BY s.company, s.item_code, s.warehouse) x
GROUP BY x.company;

SELECT 'D2b GL vouchers out of balance (debit <> credit)' AS chk, COUNT(*) AS cnt FROM (SELECT voucher_no FROM `tabGL Entry` WHERE is_cancelled=0 AND voucher_no IN (SELECT voucher_no FROM `tmp_glr`) GROUP BY voucher_no HAVING ABS(SUM(debit)-SUM(credit)) > 0.005) q;

SELECT 'D2 GL Stock In Hand' AS chk, account, ROUND(SUM(debit-credit),2) gl_value
FROM `tabGL Entry` WHERE is_cancelled=0 AND account IN ('Stock In Hand - SEPL','Stock In Hand - SEPLU') GROUP BY account;

SELECT 'D3 vouchers GL <> ledger (> 0.01)' AS chk, COUNT(*) AS cnt FROM (
  SELECT vn FROM (
    SELECT voucher_no vn, SUM(stock_value_difference) sv, 0 gv FROM `tabStock Ledger Entry` WHERE is_cancelled=0 GROUP BY voucher_no
    UNION ALL SELECT voucher_no, 0, SUM(debit-credit) FROM `tabGL Entry` WHERE is_cancelled=0 AND account IN ('Stock In Hand - SEPL','Stock In Hand - SEPLU') GROUP BY voucher_no) u
  GROUP BY vn HAVING ABS(SUM(sv)-SUM(gv)) > 0.01) q;

SELECT 'D4 running breaks (value > 0.01 or qty)' AS chk, COUNT(*) AS cnt FROM (
  SELECT stock_value, stock_value_difference, actual_qty, qty_after_transaction,
         LAG(stock_value) OVER w pv, LAG(qty_after_transaction) OVER w pq
  FROM `tabStock Ledger Entry` WHERE is_cancelled=0
  WINDOW w AS (PARTITION BY item_code, warehouse ORDER BY posting_datetime, creation, name)) t
WHERE ABS(stock_value-IFNULL(pv,0)-stock_value_difference) > 0.01 OR ABS(qty_after_transaction-IFNULL(pq,0)-actual_qty) > 0.0001;

SELECT 'D5 Bin <> last ledger' AS chk, COUNT(*) AS cnt FROM `tabBin` b JOIN `tmp_last2` l ON l.item_code=b.item_code AND l.warehouse=b.warehouse
WHERE ABS(b.stock_value-l.lv) > 0.01;

SELECT 'D6 ledger lines where batch qty or value differ' AS chk,
       SUM(ABS(t.aq-t.bq) > 0.001) AS qty_lines, SUM(ABS(t.svd-t.bv) > 0.01) AS value_lines
FROM (SELECT sle.actual_qty aq, sle.stock_value_difference svd, SUM(sbe.qty) bq, SUM(sbe.stock_value_difference) bv
      FROM `tabStock Ledger Entry` sle JOIN `tabSerial and Batch Entry` sbe ON sbe.parent=sle.serial_and_batch_bundle
      WHERE sle.is_cancelled=0 GROUP BY sle.name, sle.actual_qty, sle.stock_value_difference) t;

SELECT 'D7 batch-tracked ledger lines without bundle' AS chk, COUNT(*) AS cnt
FROM `tabStock Ledger Entry` sle JOIN `tabItem` i ON i.name=sle.item_code AND i.has_batch_no=1
WHERE sle.is_cancelled=0 AND IFNULL(sle.serial_and_batch_bundle,'')='';

SELECT 'D8 batch master <> ledger' AS chk, COUNT(*) AS cnt FROM `tabBatch` b
LEFT JOIN (SELECT sbe.batch_no, SUM(sbe.qty) led FROM `tabStock Ledger Entry` sle JOIN `tabSerial and Batch Entry` sbe ON sbe.parent=sle.serial_and_batch_bundle WHERE sle.is_cancelled=0 GROUP BY sbe.batch_no) x ON x.batch_no=b.name
WHERE ABS(IFNULL(x.led,0)-IFNULL(b.batch_qty,0)) > 0.001;

SELECT 'D9 zero-qty batches with value left' AS chk, COUNT(*) AS cnt FROM (
  SELECT sbe.batch_no, sle.warehouse FROM `tabStock Ledger Entry` sle
  JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sle.serial_and_batch_bundle
  JOIN `tabBatch` b ON b.name = sbe.batch_no AND b.use_batchwise_valuation = 1
  WHERE sle.is_cancelled = 0
  GROUP BY sbe.batch_no, sle.warehouse
  HAVING ABS(SUM(sbe.qty)) <= 0.0001 AND ABS(SUM(sbe.stock_value_difference)) > 0.005) q;

SELECT 'D10 item-warehouses with negative value' AS chk, company, COUNT(*) AS cnt, ROUND(SUM(v),2) AS value FROM (
  SELECT company, item_code, warehouse, SUM(stock_value_difference) v FROM `tabStock Ledger Entry` WHERE is_cancelled=0
  GROUP BY company, item_code, warehouse HAVING SUM(stock_value_difference) < -0.01) q GROUP BY company;

-- EXPECTED: D1 ledger_minus_stock_balance = 0 and bin_minus_ledger = 0 (±1);
--           D3 counts only differences > 0.01; vouchers listed under C4 (> 1 rupee) need GL regeneration from the voucher;
--           D2 = D1 stock_balance (±1); D3..D8 = 0. D9 = only the residues C6 listed as not cleared.
--           D10 is information: negative value with positive qty needs a dated-today revaluation.
-- If D6 qty_lines or D7 is not 0: those lines need a batch decision (not auto-fixed); send the list.
-- Now type   COMMIT;   or   ROLLBACK;
