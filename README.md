# Stock Guard

A Frappe app for ERPNext v16 that keeps these four sources in line when late (back-dated) stock entries are posted:

- Stock Ledger (closing)
- Bin
- Stock Balance
- GL Stock In Hand

## What it does

1. **Batch rate guard.**
   - During posting and reposting, it replaces a batch outgoing rate that is clearly invalid: negative, large enough to overflow the column, or far above the batch's last inward rate.
   - An invalid rate like this makes the Repost Item Valuation job fail. Failing jobs are why reposting got switched off.
   - Every replacement is recorded in the Error Log with the title "Stock Guard: batch rate replaced".
2. **Daily consistency check** (06:30). It compares Stock Ledger closing, Bin, Stock Balance and GL Stock In Hand for each company, and lists:
   - item-warehouses with a broken running balance
   - Bins that differ from the last ledger entry
   - vouchers whose GL differs from the Stock Ledger
   - whether the reposting scheduler is stopped
   - repost jobs that failed recently

   The report is emailed and saved in Stock Guard Settings.
3. **Automatic repair** (optional, off by default). It creates standard Repost Item Valuation entries for whatever the check finds, and lets ERPNext core rebuild the data.

4. **One-time Match** (buttons on Stock Guard Settings, System Manager only). Runs the tested one-time clean-up (`sql/live_match.sql`) inside the site as a background job, so no database console is needed:
   - **Preview** changes nothing.
   - **Apply** freezes posting, takes backup tables and corrects the data. It commits only if every check passes, then optionally switches reposting on and applies the late-entry settings.
   - **Undo** restores the backup.
   - Vouchers whose GL differs from the ledger by more than ₹1 are reported and left unchanged (they need a source-document fix). They no longer block Apply.
5. **Per-document back-date window** (1.2.0). Purchase Receipts, and Purchase Invoices that update stock, can be dated up to 30 days back, because they are submitted after QC. Other stock documents (Delivery Note, Sales Invoice with Update Stock, Stock Entry, Stock Reconciliation, Subcontracting Receipt) can be dated up to 3 days back. Users with Stock Settings > Role Allowed to Edit Frozen Stock are not limited.
6. **GL repost fix** (1.2.0). ERPNext's `get_voucherwise_gl_entries` (in `erpnext/accounts/utils.py`) also counts cancelled GL rows when it decides whether a voucher's GL is already correct. As a result, a voucher can be left without GL. Stock Guard replaces it with a version that only reads active rows, so missing GL is always recreated.
7. **Nightly self-heal** (1.2.0). If the 06:30 check finds a broken running balance or a Bin difference, it runs the One-time Match (Apply) automatically. The run commits only if every check passes, and it restores the previous Stock Settings afterwards. It waits for another night when repost entries are still queued or in progress, because the match would set them to Skipped. Each run replaces the `zz_fix_bk_*` backup tables, so **Undo** only reverts the latest run.
8. **Zero-qty batch residue clearance** (1.2.1, step C6 of the One-time Match, off by default). A batch-wise valued batch that has been fully issued can still carry a value, often a negative one. C6 clears it on the batch's last outward Serial and Batch Entry and its Stock Ledger line. On the same voucher and date it posts a `GLRES-` pair: Stock In Hand against Stock Adjustment. Movements on or before the original Stock Frozen Up To date are only listed, never changed. Check D9 then confirms that only those listed residues remain. D10 lists item-warehouses with a negative value, for information. **Undo** restores the ledger values and deletes the `GLRES-` rows; it still works with backups taken by 1.2.0. Switch on **Clear Value Left on Zero-qty Batches** only after Accounts approval. Remember that the nightly self-heal also applies C6 while the setting is on.

## Install

Self-hosted bench:

```bash
cd ~/frappe-bench
bench get-app https://github.com/<your-org>/stock_guard   # or a local git path
bench --site seplt.frappe.cloud install-app stock_guard
bench --site seplt.frappe.cloud migrate
bench restart
```

Frappe Cloud:

1. Push this folder to a GitHub repository.
2. Add the app to the bench group and deploy.
3. Install it on the site.

## Settings

Go to **Stock Guard Settings**:

| Field | Default | Meaning |
|---|---|---|
| Enabled | On | Master switch |
| Enable Batch Rate Guard | On | Turns the rate guard on or off |
| Max Rate vs Last Inward Rate (x) | 20 | A suspicious rate above this multiple of the last inward rate is replaced |
| Send Report To Role | Stock Manager | Who receives the daily email |
| Extra Recipients | | More email addresses |
| Value Tolerance | 1 | ₹ difference that still counts as a match |
| Check GL vs Stock Ledger for Last (Days) | 90 | Window for the voucher-level GL check |
| Create Repost Entries Automatically | Off | Turns on automatic repair |
| Max Repost Entries Per Run | 200 | Safety limit |
| Enable Back-date Window | On | Turns the per-document back-date window on or off |
| Purchase Receipt: Max Days Back | 30 | Also used for Purchase Invoices that update stock |
| Other Stock Documents: Max Days Back | 3 | Every other stock document |
| Run One-time Match Automatically When a Gap Is Found | On | Nightly self-heal |
| Clear Value Left on Zero-qty Batches | Off | Step C6: clears the value left on fully issued batches (needs Accounts approval) |

Recommended Stock Settings with the back-date window:

| Field | Value |
|---|---|
| Stock Frozen Up To | Last closed month end |
| Stock Frozen Up To Days | 31 (hard outer limit; Stock Guard applies the 30 / 3-day windows inside it) |
| Role Allowed to Edit Frozen Stock | Stock Backdate Approver |
| Role Allowed to Create/Edit Back-dated Transactions | blank |

Click **Run Check Now** on the settings form to run the check straight away.

## Tests

```bash
python -m unittest stock_guard.tests.test_rate_rules
```

## After a `bench update`

Check that `BatchNoValuation.set_stock_value_difference` in `erpnext/stock/serial_batch_bundle.py` still matches the loop in `stock_guard/overrides/batch_valuation.py`. If core has changed that method, the guard must be updated, or disabled in the settings.

Also check that `get_voucherwise_gl_entries` in `erpnext/accounts/utils.py` still has the same signature and return shape as `stock_guard/overrides/gl_repost.py`.
