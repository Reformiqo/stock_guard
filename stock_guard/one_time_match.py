"""One-time match of Stock Ledger, Bin, Stock Balance, GL Stock In Hand and batch records.

Runs the tested SQL in stock_guard/sql/live_match.sql from inside the site, as a
background job, so nobody needs a database console.

Modes
  preview : runs the corrections and the checks inside one transaction, then rolls
            everything back. Nothing is changed. Use it to see what would change.
  apply   : freezes stock posting, takes backup tables (zz_fix_bk_*), runs the
            corrections and the checks. It commits only if every check passes;
            otherwise it rolls back and restores the previous Stock Settings.
  undo    : restores every changed value from the zz_fix_bk_* backup tables.

The result is written to Stock Guard Settings (One-time Match section) and emailed.
"""

import os
import re

import frappe
from frappe.utils import cint, flt, now_datetime
from frappe.utils.user import get_users_with_role

SQL_DIR = os.path.join(os.path.dirname(__file__), "sql")
MATCH_FILE = "live_match.sql"
UNDO_FILE = "live_undo.sql"

REPOST_JOBS = (
	"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.run_parallel_reposting",
	"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.repost_entries",
)
TOLERANCE = 1.0
PART_RE = re.compile(r"^-- PART ([A-D]) ", re.M)


# ---------------------------------------------------------------- public entry points


@frappe.whitelist()
def start(mode: str, confirm: str | None = None, enable_reposting: int = 1, apply_settings: int = 1):
	"""Called from the Stock Guard Settings form. Queues the job."""
	frappe.only_for("System Manager")
	mode = (mode or "").lower()
	if mode not in ("preview", "apply", "undo"):
		frappe.throw("Mode must be preview, apply or undo.")
	if mode == "apply" and confirm != "APPLY":
		frappe.throw("Type APPLY to confirm.")
	if mode == "undo" and confirm != "UNDO":
		frappe.throw("Type UNDO to confirm.")
	if mode == "undo" and not frappe.db.sql("SHOW TABLES LIKE 'zz_fix_bk_sle'"):
		frappe.throw("No backup tables found. Undo is only possible after an Apply run.")

	_save(mode, "Queued", f"Queued by {frappe.session.user} at {now_datetime()}")
	frappe.enqueue(
		"stock_guard.one_time_match.run",
		queue="long",
		timeout=5400,
		job_id="stock_guard_one_time_match",
		deduplicate=True,
		mode=mode,
		enable_reposting=cint(enable_reposting),
		apply_settings=cint(apply_settings),
		requested_by=frappe.session.user,
	)
	return "Queued"


def run(mode, enable_reposting=1, apply_settings=1, requested_by=None):
	_save(mode, "Running", f"Started at {now_datetime()}")
	log = Log(mode)
	try:
		if mode == "undo":
			status = _run_undo(log)
		else:
			status = _run_match(log, apply=(mode == "apply"), enable_reposting=enable_reposting,
				apply_settings=apply_settings)
	except Exception:
		frappe.db.rollback()
		log.add("ERROR", frappe.get_traceback())
		status = "Failed"
		if mode == "apply":
			_restore_settings(log)
	_save(mode, status, log.text())
	_notify(mode, status, log.text(), requested_by)
	return status


# ---------------------------------------------------------------- match / undo


def _drop_temp_tables(text):
	"""Drop the script's temporary tables so a re-run on the same connection starts clean."""
	for name in sorted(set(re.findall(r"CREATE TEMPORARY TABLE `(tmp_\w+)`", text))):
		saved = frappe.db.transaction_writes
		frappe.db.transaction_writes = 0
		try:
			frappe.db.sql(f"DROP TEMPORARY TABLE IF EXISTS `{name}`")
		finally:
			frappe.db.transaction_writes = saved


def _run_match(log, apply, enable_reposting, apply_settings):
	text = _read(MATCH_FILE)
	parts = _parts(text)
	executor = Executor(log)
	_drop_temp_tables(text)
	try:
		return _run_match_inner(log, apply, enable_reposting, apply_settings, parts, executor)
	finally:
		_drop_temp_tables(text)


PRE_CHECK_SQL = """
	select count(*) from `tabStock Ledger Entry` s
	join `tabItem` i on i.name = s.item_code
	where s.voucher_type = 'Stock Reconciliation' and s.is_cancelled = 0
		and i.has_batch_no = 0 and s.actual_qty = 0
		and abs(s.qty_after_transaction - (
			select coalesce(sum(p.actual_qty), 0) from `tabStock Ledger Entry` p
			where p.item_code = s.item_code and p.warehouse = s.warehouse and p.is_cancelled = 0
				and (p.posting_datetime < s.posting_datetime
					or (p.posting_datetime = s.posting_datetime and p.creation < s.creation))
		)) > 0.001
"""


def _run_match_inner(log, apply, enable_reposting, apply_settings, parts, executor):
	# The script rebuilds running qty from actual_qty. A non-batch Stock Reconciliation that
	# changed qty (actual_qty = 0, new qty in qty_after_transaction) is the one case it cannot
	# rebuild, so stop before touching anything if one exists.
	blocking = cint(frappe.db.sql(PRE_CHECK_SQL)[0][0])
	log.add("PRE-CHECK", f"Non-batch Stock Reconciliations that change qty: {blocking}")
	if blocking:
		log.add("RESULT", "Stopped before any change. Send the reconciliation list for review.")
		return "Stopped: pre-check"
	clear_residue = cint(frappe.db.get_single_value("Stock Guard Settings", "clear_value_residue"))
	frappe.db.sql("SET @sg_clear_residue = %s", (clear_residue,))
	log.add("INFO", "Zero-qty batch value residue (C6): " + ("cleared" if clear_residue else "not cleared (setting off)"))
	executor.clear_residue = clear_residue
	if apply:
		_remember_settings()
		executor.run_block(parts["pre"])
		executor.run_block(parts["A"])  # freeze, committed
		frappe.clear_document_cache("Stock Settings", "Stock Settings")
		executor.run_block(parts["B"])  # backups (DDL, committed)
	else:
		executor.run_block(parts["pre"])
		log.add("INFO", "Preview: freeze and backups skipped; all changes will be rolled back.")

	executor.run_block(parts["C"])
	executor.run_block(parts["D"])

	ok, problems = evaluate(executor.results, clear_residue=clear_residue)
	for p in problems:
		log.add("CHECK FAILED", p)

	if not apply:
		frappe.db.rollback()
		log.add("RESULT", "Preview finished. Everything was rolled back. "
			+ ("All checks would pass." if ok else "Some checks would fail (see above)."))
		return "Preview OK" if ok else "Preview: checks fail"

	if not ok:
		frappe.db.rollback()
		_restore_settings(log)
		log.add("RESULT", "Checks failed. All corrections were rolled back. Stock Settings restored.")
		return "Rolled back"

	frappe.db.commit()
	log.add("RESULT", "All checks passed. Corrections committed.")

	if enable_reposting:
		for method in REPOST_JOBS:
			frappe.db.set_value("Scheduled Job Type", {"method": method}, "stopped", 0)
		log.add("INFO", "Reposting scheduler switched on: run_parallel_reposting, repost_entries.")
	if apply_settings:
		_apply_late_entry_settings(log)
	else:
		# Part A froze posting for the run; put the previous Stock Settings back.
		_restore_settings(log)
	frappe.db.commit()
	frappe.clear_cache()
	return "Applied"


def _run_undo(log):
	executor = Executor(log)
	statements = split_statements(_read(UNDO_FILE))
	if not frappe.db.sql("SHOW TABLES LIKE 'zz_fix_bk_run'"):
		# Backup taken by Stock Guard 1.2.0 or earlier: it has no C6 data to restore.
		# Only the C6 statements are skipped: the GLRES delete and the Stock Ledger Entry
		# value restore (the 1.2.0 SLE backup has no stock_value_difference column).
		statements = [s for s in statements if not _is_c6_undo(s)]
	for stmt in statements:
		executor.execute(stmt)
	frappe.db.commit()
	frappe.clear_cache()
	log.add("RESULT", "Undo committed. Values restored from zz_fix_bk_* tables; reposting scheduler stopped again.")
	return "Undone"


def _is_c6_undo(stmt):
	if "zz_fix_bk_run" in stmt:
		return True
	return "`tabStock Ledger Entry`" in stmt and "JOIN `zz_fix_bk_sle`" in stmt and "s.stock_value_difference=b.stock_value_difference" in stmt


# ---------------------------------------------------------------- checks


def evaluate(results, clear_residue=0):
	"""Decide whether the corrected data is consistent. Returns (ok, [problems])."""
	problems = []
	by_label = {}
	for rows in results:
		if rows:
			label = str(list(rows[0].values())[0])
			by_label.setdefault(label, []).extend(rows)

	def one(label, field="cnt"):
		rows = by_label.get(label)
		if not rows:
			problems.append(f"Check '{label}' did not return a result.")
			return None
		return flt(rows[0].get(field))

	stock_balance = {}
	d1 = by_label.get("D1 value") or []
	if not d1:
		problems.append("Check 'D1 value' did not return a result.")
	for r in d1:
		stock_balance[r["company"]] = flt(r["stock_balance"])
		if abs(flt(r["ledger_minus_stock_balance"])) > TOLERANCE:
			problems.append(f"{r['company']}: ledger closing differs from Stock Balance by {r['ledger_minus_stock_balance']}")
		if abs(flt(r["bin_minus_ledger"])) > TOLERANCE:
			problems.append(f"{r['company']}: Bin differs from ledger closing by {r['bin_minus_ledger']}")

	# Vouchers whose GL differs from the ledger by more than 1 rupee are reported by the
	# script (C4) and not changed; they need a source-document fix. Allow for them here so
	# they do not block the stock corrections, and report them instead.
	known_gl_gap = {}
	for rows in results:
		for r in rows:
			first = str(list(r.values())[0]) if r else ""
			if first.startswith("C4 vouchers with GL"):
				known_gl_gap[r.get("company")] = known_gl_gap.get(r.get("company"), 0.0) + flt(r.get("diff"))

	for r in by_label.get("D2 GL Stock In Hand") or []:
		company = frappe.db.get_value("Account", r["account"], "company")
		if company not in stock_balance:
			continue
		gap = flt(r["gl_value"]) - stock_balance[company]
		reported = known_gl_gap.get(company, 0.0)
		if abs(gap + reported) > TOLERANCE and abs(gap) > TOLERANCE:
			problems.append(f"{company}: GL Stock In Hand {r['gl_value']} differs from Stock Balance {stock_balance[company]}")

	for label in (
		"D2b GL vouchers out of balance (debit <> credit)",
		"D4 running breaks (value > 0.01 or qty)",
		"D5 Bin <> last ledger",
		"D7 batch-tracked ledger lines without bundle",
		"D8 batch master <> ledger",
	):
		value = one(label)
		if value:
			problems.append(f"{label}: {cint(value)}")

	# D9: after C6, only the residues C6 listed as not cleared (closed period / no outward
	# movement) may remain.
	if clear_residue:
		not_cleared = sum(len(v) for k, v in by_label.items() if k.startswith("C6 zero-qty batch residue NOT cleared"))
		d9 = one("D9 zero-qty batches with value left")
		if d9 is not None and cint(d9) > not_cleared:
			problems.append(f"D9 zero-qty batches with value left: {cint(d9)} (only {not_cleared} expected)")

	d6 = by_label.get("D6 ledger lines where batch qty or value differ")
	if not d6:
		problems.append("Check 'D6' did not return a result.")
	elif flt(d6[0].get("qty_lines")) or flt(d6[0].get("value_lines")):
		problems.append(f"D6 batch lines differ: qty {d6[0].get('qty_lines')}, value {d6[0].get('value_lines')}")

	return (not problems), problems


# ---------------------------------------------------------------- SQL execution


class Executor:
	"""Runs SQL statements on the site connection, keeping Frappe's transaction rules intact."""

	def __init__(self, log):
		self.log = log
		self.results = []
		self.clear_residue = 0

	def run_block(self, text):
		for stmt in split_statements(text):
			self.execute(stmt)

	def execute(self, stmt):
		head = re.sub(r"\s+", " ", stmt.strip()[:60]).upper()

		if head.startswith("COMMIT"):
			frappe.db.commit()
			return
		if head.startswith("ROLLBACK"):
			frappe.db.rollback()
			return
		if head.startswith("START TRANSACTION") or head.startswith("SET AUTOCOMMIT"):
			# Frappe already keeps the connection inside a transaction.
			return
		if re.match(r"(CREATE|DROP) TEMPORARY TABLE", head):
			# MariaDB does not commit for CREATE/DROP TEMPORARY TABLE; Frappe's guard
			# would still refuse it after writes, so bypass the guard for these only.
			saved = frappe.db.transaction_writes
			frappe.db.transaction_writes = 0
			try:
				frappe.db.sql(stmt)
			finally:
				frappe.db.transaction_writes = saved
			return
		if re.match(r"(CREATE|DROP|ALTER|TRUNCATE|RENAME) ", head):
			# Real DDL (backup tables only): commits first, by design.
			frappe.db.sql_ddl(stmt)
			return

		rows = frappe.db.sql(stmt, as_dict=True)
		if head.startswith("SELECT"):
			rows = [dict(r) for r in (rows or [])]
			self.results.append(rows)
			for r in rows[:200]:
				self.log.add("ROW", " | ".join(f"{k}={v}" for k, v in r.items()))


def split_statements(text):
	"""Split SQL text into statements on ';', ignoring comments and quoted text."""
	statements, buf = [], []
	i, n = 0, len(text)
	quote = None
	while i < n:
		ch = text[i]
		nxt = text[i + 1] if i + 1 < n else ""
		if quote:
			buf.append(ch)
			if ch == "\\" and quote in ("'", '"'):
				buf.append(nxt)
				i += 2
				continue
			if ch == quote:
				if nxt == quote:  # doubled quote inside a literal
					buf.append(nxt)
					i += 2
					continue
				quote = None
			i += 1
			continue
		if ch == "-" and nxt == "-":
			j = text.find("\n", i)
			i = n if j == -1 else j + 1
			buf.append("\n")
			continue
		if ch == "#":
			j = text.find("\n", i)
			i = n if j == -1 else j + 1
			buf.append("\n")
			continue
		if ch == "/" and nxt == "*":
			j = text.find("*/", i + 2)
			i = n if j == -1 else j + 2
			continue
		if ch in ("'", '"', "`"):
			quote = ch
			buf.append(ch)
			i += 1
			continue
		if ch == ";":
			stmt = "".join(buf).strip()
			if stmt:
				statements.append(stmt)
			buf = []
			i += 1
			continue
		buf.append(ch)
		i += 1
	tail = "".join(buf).strip()
	if tail:
		statements.append(tail)
	return statements


def _parts(text):
	"""Split the match script into its preamble and parts A-D."""
	marks = [(m.group(1), m.start()) for m in PART_RE.finditer(text)]
	if [m[0] for m in marks] != ["A", "B", "C", "D"]:
		frappe.throw("live_match.sql does not contain parts A, B, C and D in order.")
	parts = {"pre": text[: marks[0][1]]}
	for idx, (name, start) in enumerate(marks):
		end = marks[idx + 1][1] if idx + 1 < len(marks) else len(text)
		parts[name] = text[start:end]
	return parts


def _read(name):
	with open(os.path.join(SQL_DIR, name), encoding="utf-8") as f:
		return f.read()


# ---------------------------------------------------------------- settings


_SETTINGS_FIELDS = ("stock_frozen_upto", "stock_auth_role", "stock_frozen_upto_days")


def _remember_settings():
	values = {f: frappe.db.get_single_value("Stock Settings", f) for f in _SETTINGS_FIELDS}
	frappe.cache.set_value("stock_guard:stock_settings_before", values, expires_in_sec=86400)


def _restore_settings(log):
	values = frappe.cache.get_value("stock_guard:stock_settings_before")
	if values is None:
		return
	for field, value in values.items():
		frappe.db.set_single_value("Stock Settings", field, value)
	frappe.db.commit()
	frappe.clear_document_cache("Stock Settings", "Stock Settings")
	log.add("INFO", f"Stock Settings restored: {values}")


def _apply_late_entry_settings(log):
	"""Late-entry rules: 31 days is the hard outer limit in core; Stock Guard applies the
	per-document windows (Purchase Receipt 30 days, others 3 days) inside it. Older entries
	need the approver role. Part A froze posting up to today, so the previous
	Stock Frozen Up To (the last closed month end) is put back."""
	before = frappe.cache.get_value("stock_guard:stock_settings_before") or {}
	frappe.db.set_single_value(
		"Stock Settings",
		{
			"stock_frozen_upto": before.get("stock_frozen_upto"),
			"stock_frozen_upto_days": 31,
			"stock_auth_role": "Stock Backdate Approver",
		},
	)
	frappe.db.set_single_value("Stock Reposting Settings", "notify_reposting_error_to_role", "Stock Manager")
	frappe.clear_document_cache("Stock Settings", "Stock Settings")
	log.add(
		"INFO",
		f"Stock Settings: Stock Frozen Up To = {before.get('stock_frozen_upto') or 'blank'}, "
		"Stock Frozen Up To Days = 31, Role Allowed to Edit Frozen Stock = Stock Backdate Approver. "
		"Stock Reposting Settings: Notify Reposting Error to Role = Stock Manager.",
	)


# ---------------------------------------------------------------- logging / output


class Log:
	def __init__(self, mode):
		self.lines = [f"One-time match ({mode}) on {frappe.local.site} at {now_datetime()}"]

	def add(self, kind, text):
		self.lines.append(f"[{kind}] {text}")

	def text(self):
		return "\n".join(self.lines)


def _save(mode, status, text):
	frappe.db.set_single_value(
		"Stock Guard Settings",
		{
			"match_last_mode": mode,
			"match_last_status": status,
			"match_last_run_on": now_datetime(),
			"match_last_log": text[-140000:],
		},
	)
	frappe.db.commit()


def _notify(mode, status, text, requested_by):
	role = frappe.db.get_single_value("Stock Guard Settings", "alert_role") or "Stock Manager"
	recipients = set(get_users_with_role(role))
	if requested_by and requested_by not in ("Administrator", "Guest"):
		recipients.add(requested_by)
	if not recipients:
		return
	body = "<pre style='white-space:pre-wrap'>" + frappe.utils.escape_html(text[-60000:]) + "</pre>"
	try:
		frappe.sendmail(
			recipients=sorted(recipients),
			subject=f"Stock Guard one-time match ({mode}): {status}",
			message=body,
			now=True,
		)
	except Exception:
		frappe.log_error(title="Stock Guard: one-time match email not sent", message=frappe.get_traceback())
