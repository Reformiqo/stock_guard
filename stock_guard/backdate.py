"""Per-document back-date window for stock transactions.

Core ERPNext has one window for every document (Stock Settings > Stock Frozen Up To Days).
Sona needs a longer window for Purchase Receipts (they are submitted only after QC) and a
short window for everything else. This validation applies those windows; users with the
approver role (Stock Settings > Role Allowed to Edit Frozen Stock) are not limited.
"""

import frappe
from frappe.utils import cint, date_diff, getdate, nowdate

STOCK_DOCTYPES = (
	"Purchase Receipt",
	"Purchase Invoice",
	"Delivery Note",
	"Sales Invoice",
	"Stock Entry",
	"Stock Reconciliation",
	"Subcontracting Receipt",
)


def validate_backdate(doc, method=None):
	if doc.doctype not in STOCK_DOCTYPES:
		return
	if doc.doctype in ("Purchase Invoice", "Sales Invoice") and not cint(doc.get("update_stock")):
		return
	if not doc.get("posting_date"):
		return

	settings = _settings()
	if not settings or not cint(settings.enabled) or not cint(settings.enable_backdate_rule):
		return

	days_back = date_diff(nowdate(), getdate(doc.posting_date))
	if days_back <= 0:
		return

	if doc.doctype in ("Purchase Receipt", "Purchase Invoice"):
		allowed = cint(settings.backdate_days_purchase_receipt)
	else:
		allowed = cint(settings.backdate_days_default)

	if days_back <= allowed:
		return

	approver = frappe.db.get_single_value("Stock Settings", "stock_auth_role")
	if approver and approver in frappe.get_roles():
		return

	frappe.throw(
		f"{doc.doctype} {doc.name or ''} is dated {days_back} days back. "
		f"Up to {allowed} days back is allowed for this document. "
		f"Post it with a later date, or ask a user with the role '{approver or 'Stock Backdate Approver'}'.",
		title="Back-dated stock entry",
	)


def _settings():
	try:
		return frappe.get_cached_doc("Stock Guard Settings")
	except Exception:
		return None
