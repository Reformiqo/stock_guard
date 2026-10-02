"""Daily consistency check for Stock Ledger, Bin, Stock Balance and GL Stock In Hand.

Runs after the nightly reposting window. It:
  1. Compares the four sources per company.
  2. Finds item-warehouses whose running balance is broken or whose Bin differs
     from the last Stock Ledger Entry.
  3. Finds vouchers whose GL Stock In Hand differs from their Stock Ledger value.
  4. Checks that the reposting scheduler is running and lists failed repost jobs.
  5. Optionally creates Repost Item Valuation entries to repair what it found.
  6. Emails the report and stores a summary in Stock Guard Settings.
"""

import json

import frappe
from frappe.utils import add_days, cint, flt, fmt_money, get_datetime, getdate, now_datetime, nowdate
from frappe.utils.user import get_users_with_role

REPOST_JOBS = (
	"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.run_parallel_reposting",
	"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.repost_entries",
)
ROW_VALUE_TOLERANCE = 0.01
QTY_TOLERANCE = 0.001


# ---------------------------------------------------------------- entry points


def enqueue_daily_check():
	"""Scheduler entry point. The check can take a few minutes, so run it on the long queue."""
	frappe.enqueue(
		"stock_guard.health.run_daily_check",
		queue="long",
		timeout=3600,
		job_id="stock_guard_daily_check",
		deduplicate=True,
	)


def run_daily_check():
	settings = frappe.get_single("Stock Guard Settings")
	if not settings.enabled:
		return

	tolerance = flt(settings.value_tolerance) or 1.0
	report = build_report(tolerance=tolerance, gl_days=cint(settings.gl_check_days) or 90)

	repairs = {"created": [], "skipped": [], "errors": []}
	if settings.auto_repair:
		repairs = create_repairs(report, max_jobs=cint(settings.max_repair_jobs) or 200)

	send_report(settings, report, repairs)
	save_summary(report, repairs)
	return report


# ---------------------------------------------------------------- report


def build_report(tolerance=1.0, gl_days=90):
	return {
		"run_at": str(now_datetime()),
		"totals": company_totals(),
		"broken_pairs": broken_pairs(),
		"bin_mismatches": bin_mismatches(tolerance),
		"gl_mismatches": gl_mismatches(tolerance, gl_days),
		"scheduler": scheduler_status(),
		"reposting": reposting_status(),
	}


def stock_accounts():
	return frappe.get_all(
		"Account", filters={"account_type": "Stock", "is_group": 0}, fields=["name", "company"]
	)


def company_totals():
	"""Ledger closing, Bin, Stock Balance and GL Stock In Hand per company."""
	totals = {}

	def row(company):
		return totals.setdefault(
			company, {"ledger_closing": 0.0, "bin": 0.0, "stock_balance": 0.0, "gl": 0.0}
		)

	for r in frappe.db.sql(
		"""
		select company, sum(stock_value_difference) as value
		FROM `tabStock Ledger Entry`
		where is_cancelled = 0
		group by company
		""",
		as_dict=True,
	):
		row(r.company)["stock_balance"] = flt(r.value, 2)

	for r in frappe.db.sql(
		"""
		select company, sum(stock_value) as value
		FROM (
			select company, stock_value,
				row_number() over (
					partition by item_code, warehouse
					order by posting_datetime desc, creation desc, name desc
				) as rn
			FROM `tabStock Ledger Entry`
			where is_cancelled = 0
		) last_rows
		where rn = 1
		group by company
		""",
		as_dict=True,
	):
		row(r.company)["ledger_closing"] = flt(r.value, 2)

	for r in frappe.db.sql(
		"""
		select w.company, sum(b.stock_value) as value
		FROM `tabBin` b
		join `tabWarehouse` w on w.name = b.warehouse
		group by w.company
		""",
		as_dict=True,
	):
		row(r.company)["bin"] = flt(r.value, 2)

	accounts = [a.name for a in stock_accounts()]
	if accounts:
		for r in frappe.db.sql(
			"""
			select company, sum(debit - credit) as value
			FROM `tabGL Entry`
			where is_cancelled = 0 and account in %(accounts)s
			group by company
			""",
			{"accounts": accounts},
			as_dict=True,
		):
			row(r.company)["gl"] = flt(r.value, 2)

	return totals


def broken_pairs():
	"""Item-warehouses where stock_value or qty_after_transaction does not follow the previous row.

	A Stock Reconciliation row with actual_qty = 0 (non-batch item) resets the qty,
	so only its value is checked.
	"""
	return frappe.db.sql(
		"""
		select company, item_code, warehouse, count(*) as breaks,
			min(posting_datetime) as first_break_at,
			max(abs(coalesce(prev_value, 0) + stock_value_difference - stock_value)) as max_value_gap
		FROM (
			select company, item_code, warehouse, posting_datetime, voucher_type, actual_qty,
				qty_after_transaction, stock_value, stock_value_difference,
				lag(qty_after_transaction) over w as prev_qty,
				lag(stock_value) over w as prev_value
			FROM `tabStock Ledger Entry`
			where is_cancelled = 0
			window w as (partition by item_code, warehouse order by posting_datetime, creation, name)
		) chain
		where abs(coalesce(prev_value, 0) + stock_value_difference - stock_value) > %(row_tol)s
			or (
				not (voucher_type = 'Stock Reconciliation' and actual_qty = 0)
				and abs(coalesce(prev_qty, 0) + actual_qty - qty_after_transaction) > %(qty_tol)s
			)
		group by company, item_code, warehouse
		order by max_value_gap desc
		""",
		{"row_tol": ROW_VALUE_TOLERANCE, "qty_tol": QTY_TOLERANCE},
		as_dict=True,
	)


def bin_mismatches(tolerance):
	"""Bins whose qty or value differ from the last Stock Ledger Entry of the item-warehouse."""
	return frappe.db.sql(
		"""
		select w.company, b.item_code, b.warehouse,
			b.actual_qty as bin_qty, b.stock_value as bin_value,
			coalesce(l.qty_after_transaction, 0) as ledger_qty,
			coalesce(l.stock_value, 0) as ledger_value,
			l.posting_datetime as last_posting
		FROM `tabBin` b
		join `tabWarehouse` w on w.name = b.warehouse
		left join (
			select item_code, warehouse, qty_after_transaction, stock_value, posting_datetime
			FROM (
				select item_code, warehouse, qty_after_transaction, stock_value, posting_datetime,
					row_number() over (
						partition by item_code, warehouse
						order by posting_datetime desc, creation desc, name desc
					) as rn
				FROM `tabStock Ledger Entry`
				where is_cancelled = 0
			) z
			where rn = 1
		) l on l.item_code = b.item_code and l.warehouse = b.warehouse
		where abs(b.actual_qty - coalesce(l.qty_after_transaction, 0)) > %(qty_tol)s
			or abs(b.stock_value - coalesce(l.stock_value, 0)) > %(tol)s
		""",
		{"tol": tolerance, "qty_tol": QTY_TOLERANCE},
		as_dict=True,
	)


def gl_mismatches(tolerance, days):
	"""Vouchers of the last `days` days whose GL Stock In Hand differs from their Stock Ledger value."""
	accounts = [a.name for a in stock_accounts()]
	if not accounts:
		return []

	from_date = add_days(nowdate(), -days)
	ledger = {}
	for r in frappe.db.sql(
		"""
		select company, voucher_type, voucher_no, min(posting_date) as posting_date,
			sum(stock_value_difference) as value
		FROM `tabStock Ledger Entry`
		where is_cancelled = 0 and posting_date >= %(from_date)s
		group by company, voucher_type, voucher_no
		""",
		{"from_date": from_date},
		as_dict=True,
	):
		ledger[(r.company, r.voucher_type, r.voucher_no)] = r

	gl = {}
	for r in frappe.db.sql(
		"""
		select company, voucher_type, voucher_no, min(posting_date) as posting_date,
			sum(debit - credit) as value
		FROM `tabGL Entry`
		where is_cancelled = 0 and account in %(accounts)s and posting_date >= %(from_date)s
		group by company, voucher_type, voucher_no
		""",
		{"accounts": accounts, "from_date": from_date},
		as_dict=True,
	):
		gl[(r.company, r.voucher_type, r.voucher_no)] = r

	out = []
	for key in set(ledger) | set(gl):
		ledger_value = flt(ledger[key].value) if key in ledger else 0.0
		gl_value = flt(gl[key].value) if key in gl else 0.0
		if abs(ledger_value - gl_value) > tolerance:
			src = ledger.get(key) or gl.get(key)
			out.append(
				{
					"company": key[0],
					"voucher_type": key[1],
					"voucher_no": key[2],
					"posting_date": str(src.posting_date),
					"ledger_value": flt(ledger_value, 2),
					"gl_value": flt(gl_value, 2),
					"difference": flt(gl_value - ledger_value, 2),
				}
			)
	out.sort(key=lambda d: abs(d["difference"]), reverse=True)
	return out


def scheduler_status():
	return frappe.get_all(
		"Scheduled Job Type",
		filters={"method": ["in", list(REPOST_JOBS)]},
		fields=["method", "stopped", "last_execution"],
	)


def reposting_status():
	counts = {
		r.status: r.n
		for r in frappe.db.sql(
			"""
			select status, count(*) as n
			FROM `tabRepost Item Valuation`
			where docstatus = 1
			group by status
			""",
			as_dict=True,
		)
	}
	stuck = frappe.db.count(
		"Repost Item Valuation",
		{"docstatus": 1, "status": "Queued", "creation": ["<", add_days(now_datetime(), -1)]},
	)
	recent_failed = frappe.get_all(
		"Repost Item Valuation",
		filters={"docstatus": 1, "status": "Failed", "modified": [">=", add_days(now_datetime(), -2)]},
		fields=["name", "based_on", "voucher_type", "voucher_no", "item_code", "warehouse", "posting_date"],
		order_by="modified desc",
		limit=50,
	)
	return {"counts": counts, "queued_over_24h": stuck, "recent_failed": recent_failed}


# ---------------------------------------------------------------- repair


def create_repairs(report, max_jobs=200):
	"""Create Repost Item Valuation entries for the problems in the report.

	Uses the standard ERPNext reposting, so Stock Ledger, Bin, batch rows and GL
	are all rebuilt by core code.
	"""
	created, skipped, errors = [], [], []

	if any(cint(j.stopped) for j in report["scheduler"]):
		skipped.append("Reposting scheduler is stopped: no repost entries created.")
		return {"created": created, "skipped": skipped, "errors": errors}

	pairs = {}
	for p in report["broken_pairs"]:
		pairs[(p.item_code, p.warehouse)] = {"company": p.company, "from": get_datetime(p.first_break_at)}
	for b in report["bin_mismatches"]:
		key = (b.item_code, b.warehouse)
		if key not in pairs and b.last_posting:
			pairs[key] = {"company": b.company, "from": get_datetime(b.last_posting)}

	for (item_code, warehouse), info in pairs.items():
		if len(created) >= max_jobs:
			skipped.append(f"Limit of {max_jobs} reached; remaining items will be handled next run.")
			break
		if _open_or_recently_failed_pair(item_code, warehouse):
			skipped.append(f"{item_code} / {warehouse}: repost already open or failed in the last 2 days")
			continue
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Repost Item Valuation",
					"based_on": "Item and Warehouse",
					"company": info["company"],
					"item_code": item_code,
					"warehouse": warehouse,
					"posting_date": info["from"].date(),
					"posting_time": info["from"].strftime("%H:%M:%S"),
					"allow_negative_stock": 1,
					"allow_zero_rate": 1,
				}
			)
			doc.flags.ignore_permissions = True
			doc.submit()
			frappe.db.commit()
			created.append(doc.name)
		except Exception as e:
			frappe.db.rollback()
			errors.append(f"{item_code} / {warehouse}: {e}")

	for g in report["gl_mismatches"]:
		if len(created) >= max_jobs:
			break
		if g["voucher_type"] in ("Sales Invoice", "Purchase Invoice") and not frappe.db.get_value(
			g["voucher_type"], g["voucher_no"], "update_stock"
		):
			skipped.append(f"{g['voucher_type']} {g['voucher_no']}: no stock update, check manually")
			continue
		if frappe.db.exists(
			"Repost Item Valuation",
			{
				"docstatus": 1,
				"voucher_type": g["voucher_type"],
				"voucher_no": g["voucher_no"],
				"status": ["in", ["Queued", "In Progress"]],
			},
		):
			continue
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Repost Item Valuation",
					"based_on": "Transaction",
					"company": g["company"],
					"voucher_type": g["voucher_type"],
					"voucher_no": g["voucher_no"],
					"posting_date": getdate(g["posting_date"]),
					"posting_time": "00:00:00",
					"repost_only_accounting_ledgers": 1,
				}
			)
			doc.flags.ignore_permissions = True
			doc.submit()
			frappe.db.commit()
			created.append(doc.name)
		except Exception as e:
			frappe.db.rollback()
			errors.append(f"{g['voucher_type']} {g['voucher_no']}: {e}")

	return {"created": created, "skipped": skipped, "errors": errors}


def _open_or_recently_failed_pair(item_code, warehouse):
	base = {
		"docstatus": 1,
		"based_on": "Item and Warehouse",
		"item_code": item_code,
		"warehouse": warehouse,
	}
	if frappe.db.exists("Repost Item Valuation", {**base, "status": ["in", ["Queued", "In Progress"]]}):
		return True
	return bool(
		frappe.db.exists(
			"Repost Item Valuation",
			{**base, "status": "Failed", "modified": [">=", add_days(now_datetime(), -2)]},
		)
	)


# ---------------------------------------------------------------- output


def _status(ok):
	return "Match" if ok else "Mismatch"


def summary_lines(report, repairs, tolerance=1.0):
	lines = [f"Stock Guard check at {report['run_at']}", ""]
	for company, t in sorted(report["totals"].items()):
		ok = (
			abs(t["ledger_closing"] - t["stock_balance"]) <= tolerance
			and abs(t["bin"] - t["stock_balance"]) <= tolerance
			and abs(t["gl"] - t["stock_balance"]) <= tolerance
		)
		lines.append(
			f"{company}: Ledger closing {fmt_money(t['ledger_closing'])} | Bin {fmt_money(t['bin'])} | "
			f"Stock Balance {fmt_money(t['stock_balance'])} | GL {fmt_money(t['gl'])} -> {_status(ok)}"
		)
	stopped = [j.method.rsplit(".", 1)[-1] for j in report["scheduler"] if cint(j.stopped)]
	rp = report["reposting"]
	lines += [
		"",
		f"Item-warehouses with broken running balance: {len(report['broken_pairs'])}",
		f"Bins different from last ledger entry: {len(report['bin_mismatches'])}",
		f"Vouchers where GL differs from Stock Ledger: {len(report['gl_mismatches'])}",
		f"Reposting scheduler stopped: {', '.join(stopped) if stopped else 'none'}",
		f"Repost jobs: {json.dumps(rp['counts'])}; queued over 24h: {rp['queued_over_24h']}; "
		f"failed in last 2 days: {len(rp['recent_failed'])}",
		f"Repost entries created now: {len(repairs['created'])}; skipped: {len(repairs['skipped'])}; "
		f"errors: {len(repairs['errors'])}",
	]
	return lines


def _table(title, rows, columns, limit=50):
	if not rows:
		return ""
	head = "".join(f"<th style='text-align:left;padding:4px'>{c}</th>" for c in columns)
	body = ""
	for r in rows[:limit]:
		body += "<tr>" + "".join(
			f"<td style='padding:4px'>{frappe.utils.escape_html(str(r.get(c, '')))}</td>" for c in columns
		) + "</tr>"
	more = f"<p>Showing {limit} of {len(rows)}.</p>" if len(rows) > limit else ""
	return f"<h4>{title}</h4><table border='1' cellspacing='0'>{'<tr>' + head + '</tr>'}{body}</table>{more}"


def send_report(settings, report, repairs):
	recipients = set(get_users_with_role(settings.alert_role or "Stock Manager"))
	for email in (settings.extra_recipients or "").replace("\n", ",").split(","):
		if email.strip():
			recipients.add(email.strip())
	if not recipients:
		return

	lines = summary_lines(report, repairs, flt(settings.value_tolerance) or 1.0)
	all_ok = (
		not report["broken_pairs"]
		and not report["bin_mismatches"]
		and not report["gl_mismatches"]
		and not report["reposting"]["recent_failed"]
		and not any(cint(j.stopped) for j in report["scheduler"])
	)
	subject = f"Stock Guard: {'all matched' if all_ok else 'action needed'} ({nowdate()})"

	html = "<p>" + "<br>".join(frappe.utils.escape_html(l) for l in lines) + "</p>"
	html += _table(
		"Broken running balance",
		report["broken_pairs"],
		["company", "item_code", "warehouse", "breaks", "first_break_at", "max_value_gap"],
	)
	html += _table(
		"Bin vs last ledger entry",
		report["bin_mismatches"],
		["company", "item_code", "warehouse", "bin_qty", "ledger_qty", "bin_value", "ledger_value"],
	)
	html += _table(
		"GL vs Stock Ledger by voucher",
		report["gl_mismatches"],
		["company", "voucher_type", "voucher_no", "posting_date", "ledger_value", "gl_value", "difference"],
	)
	html += _table(
		"Failed repost jobs (last 2 days)",
		report["reposting"]["recent_failed"],
		["name", "based_on", "voucher_type", "voucher_no", "item_code", "warehouse", "posting_date"],
	)
	if repairs["errors"]:
		html += "<h4>Repair errors</h4><p>" + "<br>".join(
			frappe.utils.escape_html(e) for e in repairs["errors"][:50]
		) + "</p>"

	frappe.sendmail(recipients=sorted(recipients), subject=subject, message=html, now=True)


def save_summary(report, repairs):
	lines = summary_lines(report, repairs)
	frappe.db.set_single_value(
		"Stock Guard Settings",
		{"last_run_on": now_datetime(), "last_run_summary": "\n".join(lines)},
	)
	frappe.db.commit()
