app_name = "stock_guard"
app_title = "Stock Guard"
app_publisher = "Reformiqo"
app_description = (
	"Keeps Stock Ledger, Bin, Stock Balance and GL Stock In Hand consistent "
	"when late (back-dated) stock entries are posted."
)
app_email = "consultant.reformiqo@gmail.com"
app_license = "mit"

required_apps = ["erpnext"]

after_install = "stock_guard.install.after_install"
after_migrate = "stock_guard.install.after_install"

# Apply the batch valuation guard in every web request and every background job.
# Stock reposting runs as a background job, so before_job is the important one.
before_request = [
	"stock_guard.overrides.batch_valuation.apply",
	"stock_guard.overrides.gl_repost.apply",
]
before_job = [
	"stock_guard.overrides.batch_valuation.apply",
	"stock_guard.overrides.gl_repost.apply",
]

# Per-document back-date window (Purchase Receipt can be longer because of QC).
doc_events = {
	dt: {"validate": "stock_guard.backdate.validate_backdate"}
	for dt in (
		"Purchase Receipt",
		"Purchase Invoice",
		"Delivery Note",
		"Sales Invoice",
		"Stock Entry",
		"Stock Reconciliation",
		"Subcontracting Receipt",
	)
}

scheduler_events = {
	"cron": {
		# 06:30 every day, after the nightly reposting window.
		"30 6 * * *": ["stock_guard.health.enqueue_daily_check"],
	},
}
