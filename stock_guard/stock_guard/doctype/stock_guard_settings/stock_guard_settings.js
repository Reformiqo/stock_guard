frappe.ui.form.on("Stock Guard Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Run Check Now"), () => {
			frappe
				.call("stock_guard.stock_guard.doctype.stock_guard_settings.stock_guard_settings.run_check_now")
				.then(() => frappe.show_alert(__("Check queued. The report will be emailed.")));
		});

		if (!frappe.user.has_role("System Manager")) return;

		const start = (args) =>
			frappe.call({ method: "stock_guard.one_time_match.start", args }).then(() => {
				frappe.show_alert(__("Queued. Reload this form in a few minutes to see the result."));
				frm.reload_doc();
			});

		frm.add_custom_button(
			__("Preview"),
			() => start({ mode: "preview" }),
			__("One-time Match")
		);

		frm.add_custom_button(
			__("Apply"),
			() => {
				const d = new frappe.ui.Dialog({
					title: __("Apply One-time Match"),
					fields: [
						{
							fieldtype: "HTML",
							options:
								"<p>Ask users to stop posting first. The job freezes stock posting, takes backup tables, " +
								"corrects the data and commits only if every check passes.</p>",
						},
						{ fieldname: "enable_reposting", fieldtype: "Check", label: __("Switch reposting scheduler on after success"), default: 1 },
						{ fieldname: "apply_settings", fieldtype: "Check", label: __("Apply late-entry settings after success (3-day window, approver role)"), default: 1 },
						{ fieldname: "confirm", fieldtype: "Data", label: __("Type APPLY to confirm"), reqd: 1 },
					],
					primary_action_label: __("Apply"),
					primary_action(values) {
						d.hide();
						start({ mode: "apply", ...values });
					},
				});
				d.show();
			},
			__("One-time Match")
		);

		frm.add_custom_button(
			__("Undo"),
			() => {
				frappe.prompt(
					{ fieldname: "confirm", fieldtype: "Data", label: __("Type UNDO to restore the backup"), reqd: 1 },
					(values) => start({ mode: "undo", confirm: values.confirm }),
					__("Undo One-time Match")
				);
			},
			__("One-time Match")
		);
	},
});
