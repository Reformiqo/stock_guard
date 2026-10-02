"""Guard for batch-wise valuation (ERPNext v16, erpnext/stock/serial_batch_bundle.py).

Core computes the outgoing rate of a batch as
    rate = stock value of the batch before this entry / qty of the batch before this entry

When the batch qty before the entry is close to zero, for example a Stock
Reconciliation that removes and re-adds the whole batch at the same timestamp,
this division returns an extreme rate (seen on this site: about 11.9 crore per
unit for batch SLZ-0318 F). The value then overflows the Currency column, the
UPDATE fails and the whole reposting job fails. Stopping the scheduler to avoid
these failures is what left Bin and the Stock Ledger closing out of date.

This guard keeps the core calculation unchanged for normal rows. Only when a
rate is clearly invalid does it use the last inward rate of the same batch in
the same warehouse (or the warehouse valuation rate if the batch has no inward
row). Every replacement is written to the Error Log.
"""

import frappe
from frappe.utils import flt

from stock_guard.overrides.rate_rules import is_rate_valid, is_suspicious

_PATCH_FLAG = "_stock_guard_patched"
DEFAULT_CAP_RATIO = 20.0


def apply(*args, **kwargs):
	"""Patch BatchNoValuation once per process. Safe to call many times."""
	try:
		from erpnext.stock.serial_batch_bundle import BatchNoValuation
	except ImportError:
		return

	if getattr(BatchNoValuation, _PATCH_FLAG, False):
		return

	if not hasattr(BatchNoValuation, "set_stock_value_difference"):
		# Core changed: do not patch blindly.
		frappe.logger("stock_guard").warning(
			"BatchNoValuation.set_stock_value_difference not found; guard not applied"
		)
		return

	BatchNoValuation._stock_guard_original = BatchNoValuation.set_stock_value_difference
	BatchNoValuation.set_stock_value_difference = guarded_set_stock_value_difference
	setattr(BatchNoValuation, _PATCH_FLAG, True)


def _guard_settings():
	"""Return (enabled, cap_ratio). Falls back to defaults if the settings are not migrated yet."""
	try:
		settings = frappe.get_cached_doc("Stock Guard Settings")
		enabled = bool(settings.enabled) and bool(settings.enable_batch_rate_guard)
		cap_ratio = flt(settings.batch_rate_cap_ratio) or DEFAULT_CAP_RATIO
		return enabled, cap_ratio
	except Exception:
		return True, DEFAULT_CAP_RATIO


def guarded_set_stock_value_difference(self):
	"""Same loop as core, with a rate check before the value is used."""
	enabled, cap_ratio = _guard_settings()

	for batch_no, ledger in self.batch_nos.items():
		if batch_no in self.non_batchwise_valuation_batches:
			continue

		available_qty = flt(self.available_qty[batch_no])
		if not available_qty:
			continue

		# Note: "stock_value_differece" is the attribute name used by core.
		rate = flt(self.stock_value_differece[batch_no]) / available_qty
		outgoing_qty = flt(ledger.qty)

		if enabled and is_suspicious(rate, available_qty, outgoing_qty):
			reference_rate = _reference_rate(self, batch_no)
			if not is_rate_valid(rate, available_qty, outgoing_qty, reference_rate, cap_ratio):
				_log_replacement(self, batch_no, rate, reference_rate, available_qty, outgoing_qty)
				rate = reference_rate

		self.batch_avg_rate[batch_no] = rate
		self.stock_value_change += rate * outgoing_qty


def _reference_rate(self, batch_no) -> float:
	"""Last inward rate of this batch in this warehouse up to this entry, else the warehouse rate."""
	sle = self.sle
	conditions = ""
	values = {
		"batch_no": batch_no,
		"item_code": sle.item_code,
		"warehouse": sle.warehouse,
	}
	if sle.get("posting_datetime"):
		conditions = "and posting_datetime <= %(posting_datetime)s"
		values["posting_datetime"] = sle.posting_datetime

	row = frappe.db.sql(
		f"""
		select stock_value_difference, qty
		from `tabSerial and Batch Entry`
		where batch_no = %(batch_no)s
			and item_code = %(item_code)s
			and warehouse = %(warehouse)s
			and docstatus = 1
			and is_cancelled = 0
			and type_of_transaction = 'Inward'
			and qty > 0
			{conditions}
		order by posting_datetime desc, creation desc
		limit 1
		""",
		values,
		as_dict=True,
	)
	if row and flt(row[0].qty):
		return abs(flt(row[0].stock_value_difference) / flt(row[0].qty))

	wh_data = getattr(self, "wh_data", None)
	if wh_data and flt(wh_data.get("valuation_rate")) > 0:
		return flt(wh_data.valuation_rate)

	return flt(
		frappe.db.get_value(
			"Bin", {"item_code": sle.item_code, "warehouse": sle.warehouse}, "valuation_rate"
		)
	)


def _log_replacement(self, batch_no, rate, reference_rate, available_qty, outgoing_qty):
	sle = self.sle
	key = f"stock_guard:batch_rate:{sle.get('voucher_no')}:{sle.get('voucher_detail_no')}:{batch_no}"
	try:
		if frappe.cache.get_value(key):
			return
		frappe.cache.set_value(key, 1, expires_in_sec=86400)
	except Exception:
		pass

	message = (
		f"Batch outgoing rate replaced by Stock Guard.\n"
		f"Item: {sle.item_code}\nWarehouse: {sle.warehouse}\nBatch: {batch_no}\n"
		f"Voucher: {sle.get('voucher_type')} {sle.get('voucher_no')} "
		f"(row {sle.get('voucher_detail_no')})\n"
		f"Posting: {sle.get('posting_datetime')}\n"
		f"Batch qty before entry: {available_qty}\nOutgoing qty: {outgoing_qty}\n"
		f"Computed rate: {rate}\nRate used: {reference_rate}"
	)
	frappe.log_error(title="Stock Guard: batch rate replaced", message=message)
