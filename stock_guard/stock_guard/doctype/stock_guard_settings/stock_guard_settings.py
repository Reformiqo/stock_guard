import frappe
from frappe.model.document import Document


class StockGuardSettings(Document):
	def validate(self):
		if self.batch_rate_cap_ratio is not None and self.batch_rate_cap_ratio < 2:
			frappe.throw("Max Rate vs Last Inward Rate must be at least 2.")
		if self.max_repair_jobs is not None and self.max_repair_jobs < 1:
			self.max_repair_jobs = 1

	def on_update(self):
		frappe.clear_document_cache("Stock Guard Settings", "Stock Guard Settings")


@frappe.whitelist()
def run_check_now():
	frappe.only_for("System Manager")
	frappe.enqueue("stock_guard.health.run_daily_check", queue="long", timeout=3600)
	return "Queued"
