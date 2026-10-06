"""Fix for ERPNext GL reposting of stock vouchers (erpnext/accounts/utils.py).

get_voucherwise_gl_entries() compares expected GL with existing GL rows but does not
exclude cancelled rows (is_cancelled = 1). When a submitted voucher still has old cancelled
GL rows with the expected amounts, the repost wrongly decides the GL is already correct and
never recreates it (seen on SE1R/26-27/0008-1). This replacement only reads active rows.
"""

import frappe

_PATCH_FLAG = "_stock_guard_gl_patched"


def apply(*args, **kwargs):
	try:
		import erpnext.accounts.utils as acc_utils
	except ImportError:
		return
	if getattr(acc_utils, _PATCH_FLAG, False):
		return
	if not hasattr(acc_utils, "get_voucherwise_gl_entries"):
		return
	acc_utils.get_voucherwise_gl_entries = get_voucherwise_gl_entries
	setattr(acc_utils, _PATCH_FLAG, True)


def get_voucherwise_gl_entries(future_stock_vouchers, posting_date):
	gl_entries = {}
	if not future_stock_vouchers:
		return gl_entries

	voucher_nos = [d[1] for d in future_stock_vouchers]
	gles = frappe.db.sql(
		"""
		select name, account, credit, debit, cost_center, project, voucher_type, voucher_no
		from `tabGL Entry`
		where is_cancelled = 0
			and posting_date >= %s
			and voucher_no in ({})
		""".format(", ".join(["%s"] * len(voucher_nos))),
		tuple([posting_date, *voucher_nos]),
		as_dict=1,
	)
	for d in gles:
		gl_entries.setdefault((d.voucher_type, d.voucher_no), []).append(d)
	return gl_entries
