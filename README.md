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

Click **Run Check Now** on the settings form to run the check straight away.

## Tests

```bash
python -m unittest stock_guard.tests.test_rate_rules
```

## After a `bench update`

Check that `BatchNoValuation.set_stock_value_difference` in `erpnext/stock/serial_batch_bundle.py` still matches the loop in `stock_guard/overrides/batch_valuation.py`. If core has changed that method, the guard must be updated, or disabled in the settings.
