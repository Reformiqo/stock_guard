import frappe

BACKDATE_ROLE = "Stock Backdate Approver"


def after_install():
	"""Create the approver role used for late stock entries. Settings are not changed here."""
	if not frappe.db.exists("Role", BACKDATE_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": BACKDATE_ROLE,
				"desk_access": 1,
			}
		).insert(ignore_permissions=True)
