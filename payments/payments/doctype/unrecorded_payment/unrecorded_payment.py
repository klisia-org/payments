# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.model.document import Document
from frappe.utils import now_datetime

from payments.utils.recording import call_reference

# Staff are told only once these run out, so a payment recorded by hand never
# races an automatic retry
MAX_ATTEMPTS = 3


class UnrecordedPayment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attempts: DF.Int
		error: DF.Code | None
		flags_data: DF.Code | None
		gateway: DF.Data | None
		integration_request: DF.Link | None
		last_attempt: DF.Datetime | None
		payment_status: DF.Data | None
		reference_docname: DF.DynamicLink | None
		reference_doctype: DF.Link | None
		resolved_by: DF.Link | None
		run_as: DF.Link | None
		status: DF.Literal["Retrying", "Needs Attention", "Resolved"]
	# end: auto-generated types

	def retry(self) -> bool:
		"""Try recording the payment again; True when it went through."""
		previous_data = frappe.flags.get("data")
		frappe.flags.data = frappe._dict(json.loads(self.flags_data)) if self.flags_data else None
		try:
			call_reference(
				self.reference_doctype,
				self.reference_docname,
				self.payment_status or "Completed",
				self.run_as,
			)
		except Exception:
			self.error = frappe.get_traceback()
			recorded = False
		else:
			self.status = "Resolved"
			recorded = True
		finally:
			frappe.flags.data = previous_data

		self.attempts += 1
		self.last_attempt = now_datetime()
		if not recorded and self.status == "Retrying" and self.attempts >= MAX_ATTEMPTS:
			self.status = "Needs Attention"
			self.notify_staff()

		self.save(ignore_permissions=True)
		return recorded

	@frappe.whitelist()
	def retry_now(self):
		self.check_permission("write")
		if self.status == "Resolved":
			frappe.throw(_("This payment is already resolved."))

		if self.retry():
			self.db_set("resolved_by", frappe.session.user)
			return

		frappe.msgprint(
			_("The payment still could not be recorded. See Last Error."),
			indicator="red",
			alert=True,
		)

	@frappe.whitelist()
	def mark_resolved(self):
		self.check_permission("write")
		self.status = "Resolved"
		self.resolved_by = frappe.session.user
		self.save()

	def notify_staff(self):
		users = get_users_who_can_read()
		if not users:
			return

		enqueue_create_notification(
			users,
			{
				"type": "Alert",
				"document_type": self.doctype,
				"document_name": self.name,
				"subject": _("{0} confirmed a payment for {1} {2}, but it could not be recorded").format(
					self.gateway or _("The gateway"), _(self.reference_doctype), self.reference_docname
				),
				"email_content": _(
					"The payer was charged. Recording it failed {0} times, so it is no longer retried. "
					"Fix the cause and press Retry, or record it by hand and press Mark Resolved."
				).format(self.attempts),
			},
		)


def get_users_who_can_read() -> list[str]:
	# Custom DocPerm rows, once a doctype has any, replace its DocPerm rows
	perm_doctype = (
		"Custom DocPerm"
		if frappe.db.exists("Custom DocPerm", {"parent": "Unrecorded Payment"})
		else "DocPerm"
	)
	roles = frappe.get_all(
		perm_doctype,
		filters={"parent": "Unrecorded Payment", "read": 1, "permlevel": 0},
		pluck="role",
	)
	if not roles:
		return []

	users = frappe.get_all(
		"Has Role", filters={"parenttype": "User", "role": ["in", roles]}, pluck="parent", distinct=True
	)
	return frappe.get_all(
		"User",
		filters={"name": ["in", users], "enabled": 1, "user_type": "System User"},
		pluck="name",
	)


def retry_unrecorded_payments():
	"""Hourly: give every payment still being retried another go."""
	for name in frappe.get_all("Unrecorded Payment", filters={"status": "Retrying"}, pluck="name"):
		try:
			frappe.get_doc("Unrecorded Payment", name).retry()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Unrecorded Payment {name} could not be retried")
