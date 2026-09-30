# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.desk.doctype.todo.todo import ToDo
from frappe.integrations.utils import create_request_log
from frappe.tests import IntegrationTestCase

from payments.payments.doctype.unrecorded_payment.unrecorded_payment import (
	MAX_ATTEMPTS,
	retry_unrecorded_payments,
)
from payments.utils.recording import record_payment

NOTIFY = "payments.payments.doctype.unrecorded_payment.unrecorded_payment.enqueue_create_notification"


def explode(self, payment_status):
	# a write the savepoint must undo, then the failure
	frappe.get_doc({"doctype": "ToDo", "description": "half-recorded payment"}).insert()
	raise RuntimeError("the consuming app is broken")


def redirect(self, payment_status):
	return f"/paid/{self.name}/{payment_status}"


class RecordingTestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		# the hourly job and create_request_log both commit, which would keep these rows
		self.enterContext(patch.object(frappe.db, "commit"))
		self.reference = frappe.get_doc({"doctype": "ToDo", "description": "the thing paid for"}).insert()
		self.request = create_request_log(
			{"reference_doctype": "ToDo", "reference_docname": self.reference.name},
			service_name="Stripe",
		)
		self.enterContext(patch("frappe.log_error"))

	def record(self, method, **kwargs):
		with patch.object(ToDo, "on_payment_authorized", method, create=True):
			return record_payment(self.request, "ToDo", self.reference.name, **kwargs)

	def unrecorded(self):
		name = frappe.db.get_value("Unrecorded Payment", {"integration_request": self.request.name})
		return name and frappe.get_doc("Unrecorded Payment", name)

	def half_recorded(self):
		return frappe.db.exists("ToDo", {"description": "half-recorded payment"})


class IntegrationTestRecordingAPayment(RecordingTestCase):
	def test_what_the_reference_answers_is_handed_back(self):
		self.assertEqual(self.record(redirect), f"/paid/{self.reference.name}/Completed")
		self.assertFalse(self.unrecorded())

	def test_a_reference_that_raises_is_undone_and_kept(self):
		self.assertIsNone(self.record(explode, status="Authorized"))

		self.assertFalse(self.half_recorded())
		unrecorded = self.unrecorded()
		self.assertEqual(unrecorded.gateway, "Stripe")
		self.assertEqual(unrecorded.reference_doctype, "ToDo")
		self.assertEqual(unrecorded.reference_docname, self.reference.name)
		self.assertEqual(unrecorded.payment_status, "Authorized")
		self.assertEqual(unrecorded.status, "Retrying")
		self.assertIn("the consuming app is broken", unrecorded.error)

	def test_the_call_runs_as_the_user_asked_and_the_session_comes_back(self):
		seen = []

		def remember_and_explode(self, payment_status):
			seen.append(frappe.session.user)
			raise RuntimeError("broken")

		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		self.record(remember_and_explode, run_as="Administrator")

		self.assertEqual(seen, ["Administrator"])
		self.assertEqual(frappe.session.user, "Guest")
		self.assertEqual(self.unrecorded().run_as, "Administrator")

	def test_nothing_is_called_without_a_reference(self):
		self.assertIsNone(record_payment(self.request, None, None))

	def test_an_integration_request_that_is_gone_does_not_stop_the_record(self):
		with patch.object(ToDo, "on_payment_authorized", explode, create=True):
			record_payment("no-such-request", "ToDo", self.reference.name)

		self.assertTrue(
			frappe.db.exists(
				"Unrecorded Payment", {"reference_docname": self.reference.name, "gateway": None}
			)
		)


class IntegrationTestRetrying(RecordingTestCase):
	def test_a_retry_that_goes_through_resolves_it(self):
		self.record(explode)

		with patch.object(ToDo, "on_payment_authorized", redirect, create=True):
			retry_unrecorded_payments()

		unrecorded = self.unrecorded()
		self.assertEqual(unrecorded.status, "Resolved")
		self.assertEqual(unrecorded.attempts, 1)

	def test_the_gateway_data_is_there_again_for_the_retry(self):
		frappe.flags.data = {"subscription_id": "sub_1"}
		self.addCleanup(setattr, frappe.flags, "data", None)
		self.record(explode)
		frappe.flags.data = None

		seen = []

		def remember(self, payment_status):
			seen.append(frappe.flags.data.subscription_id)

		with patch.object(ToDo, "on_payment_authorized", remember, create=True):
			retry_unrecorded_payments()

		self.assertEqual(seen, ["sub_1"])
		self.assertIsNone(frappe.flags.data)

	def test_staff_are_told_only_once_the_retries_run_out(self):
		self.record(explode)

		with patch.object(ToDo, "on_payment_authorized", explode, create=True), patch(NOTIFY) as notify:
			for _attempt in range(MAX_ATTEMPTS):
				self.assertFalse(notify.called)
				retry_unrecorded_payments()

			# no longer retried once staff have it
			retry_unrecorded_payments()

		unrecorded = self.unrecorded()
		self.assertEqual(unrecorded.status, "Needs Attention")
		self.assertEqual(unrecorded.attempts, MAX_ATTEMPTS)
		self.assertFalse(self.half_recorded())

		notify.assert_called_once()
		users, notification = notify.call_args.args
		self.assertIn("Administrator", users)
		self.assertEqual(notification["document_name"], unrecorded.name)

	def test_staff_can_retry_or_close_it_by_hand(self):
		self.record(explode)
		unrecorded = self.unrecorded()
		unrecorded.db_set("status", "Needs Attention")

		with patch.object(ToDo, "on_payment_authorized", redirect, create=True):
			unrecorded.retry_now()

		unrecorded.reload()
		self.assertEqual(unrecorded.status, "Resolved")
		self.assertEqual(unrecorded.resolved_by, "Administrator")

	def test_a_payment_recorded_by_hand_is_marked_resolved(self):
		self.record(explode)
		unrecorded = self.unrecorded()

		unrecorded.mark_resolved()

		unrecorded.reload()
		self.assertEqual(unrecorded.status, "Resolved")
		self.assertEqual(unrecorded.resolved_by, "Administrator")
