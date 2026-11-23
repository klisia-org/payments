# Copyright (c) 2018, Frappe Technologies and Contributors
# License: MIT. See LICENSE

import datetime
import json
import warnings
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import braintree
import frappe
from frappe.integrations.utils import create_request_log
from frappe.tests import IntegrationTestCase

from payments.payment_gateways.doctype.braintree_settings.braintree_settings import (
	BraintreeSettings,
	webhook,
)
from payments.utils.settlement import get_settlement

BRAINTREE = "payments.payment_gateways.doctype.braintree_settings.braintree_settings"
Kind = braintree.WebhookNotification.Kind


@contextmanager
def contained():
	"""Keep the gateway's own commits inside the test transaction."""
	frappe.db._disable_transaction_control += 1
	try:
		with warnings.catch_warnings():
			warnings.simplefilter("ignore")
			yield
	finally:
		frappe.db._disable_transaction_control -= 1


class BraintreeIntegrationTestCase(IntegrationTestCase):
	"""The class shares one transaction, so every test makes its own ids."""

	def setUp(self):
		super().setUp()
		self.reference = frappe.get_doc({"doctype": "ToDo", "description": "Braintree test charge"}).insert()
		self.enterContext(patch(f"{BRAINTREE}.call_hook_method"))
		self.settings = self.make_settings()

	def make_settings(self):
		name = f"Test BT {frappe.generate_hash(length=6)}"
		settings = frappe.get_doc(
			{
				"doctype": "Braintree Settings",
				"gateway_name": name,
				"merchant_id": "merchant",
				"public_key": "public",
				"private_key": "private",  # pragma: allowlist secret
				"use_sandbox": 1,
			}
		)
		settings.flags.ignore_mandatory = True
		with contained():
			settings.insert(ignore_permissions=True)
		return settings

	def make_request(self, **data):
		with contained():
			return create_request_log(
				{
					"amount": 40,
					"reference_doctype": "ToDo",
					"reference_docname": self.reference.name,
					"payer_email": "payer@example.com",
					**data,
				},
				service_name="Braintree",
			)

	def sell(self, transaction_id):
		"""Run a sale through the controller that Braintree approves."""
		request = self.make_request()
		self.settings.data = frappe._dict(json.loads(request.data), payload_nonce="nonce")
		self.settings.integration_request = request
		result = SimpleNamespace(
			is_success=True,
			transaction=SimpleNamespace(
				id=transaction_id, amount=Decimal("40.00"), status="submitted_for_settlement"
			),
		)
		with (
			patch("braintree.Transaction.sale", return_value=result),
			patch(f"{BRAINTREE}.record_payment"),
			patch("braintree.Configuration.configure"),
			contained(),
		):
			self.settings.create_charge_on_braintree()

		return request


class IntegrationTestTheSale(BraintreeIntegrationTestCase):
	def test_a_sale_keeps_the_transaction_for_reconciliation(self):
		transaction_id = frappe.generate_hash(length=8)
		request = self.sell(transaction_id)

		data = json.loads(frappe.db.get_value("Integration Request", request.name, "data"))
		self.assertEqual(data["braintree_transaction_id"], transaction_id)
		self.assertEqual(
			get_settlement(request.name),
			{
				"processor_id": transaction_id,
				"gross": 40.0,
				"fee": None,
				"net": None,
				"credit_date": None,
				"estimated_credit_date": None,
			},
		)


class IntegrationTestTheWebhook(BraintreeIntegrationTestCase):
	def call(self, notification=None, gateway=None, error=None):
		parse = MagicMock(return_value=notification, side_effect=error)
		account = MagicMock()
		account.webhook_notification.parse = parse
		frappe.local.form_dict = frappe._dict(bt_signature="signature", bt_payload="payload")
		with patch.object(BraintreeSettings, "get_braintree_gateway", return_value=account), contained():
			result = webhook(gateway=gateway or self.settings.name)

		parse.assert_called_once_with("signature", "payload")
		return result

	def test_a_bad_signature_is_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self.call(error=braintree.exceptions.InvalidSignatureError("bad"))

	def test_an_unknown_account_is_refused(self):
		frappe.local.form_dict = frappe._dict()
		with self.assertRaises(frappe.PermissionError):
			webhook(gateway="No Such Account")

	def test_a_disbursement_is_handed_to_the_consuming_app(self):
		transaction_id = frappe.generate_hash(length=8)
		request = self.sell(transaction_id)
		notification = SimpleNamespace(
			kind=Kind.Disbursement,
			disbursement=SimpleNamespace(
				id="payout_1",
				disbursement_date=datetime.date(2026, 12, 3),
				amount=Decimal("120.50"),
				transaction_ids=[transaction_id, "elsewhere"],
			),
		)

		with patch(f"{BRAINTREE}.call_hook_method") as hook:
			self.assertEqual(self.call(notification), {"status": "processed"})

		hook.assert_called_once_with(
			"payment_gateway_disbursement",
			gateway=f"Braintree-{self.settings.gateway_name}",
			payout_id="payout_1",
			date="2026-12-03",
			amount=120.5,
			transaction_ids=[transaction_id, "elsewhere"],
		)
		self.assertEqual(get_settlement(request.name)["credit_date"], "2026-12-03")

	def test_the_payment_gateway_name_also_finds_the_account(self):
		notification = SimpleNamespace(kind=Kind.SubscriptionWentActive)
		self.assertEqual(
			self.call(notification, gateway=f"Braintree-{self.settings.gateway_name}"), {"status": "ignored"}
		)

	def test_a_disbursed_transaction_gets_its_credit_date(self):
		transaction_id = frappe.generate_hash(length=8)
		request = self.sell(transaction_id)
		notification = SimpleNamespace(
			kind=Kind.TransactionDisbursed,
			transaction=SimpleNamespace(
				id=transaction_id,
				disbursement_details=SimpleNamespace(disbursement_date="2026-12-04"),
			),
		)

		self.assertEqual(self.call(notification), {"status": "processed"})

		settlement = get_settlement(request.name)
		self.assertEqual(settlement["credit_date"], "2026-12-04")
		self.assertEqual(settlement["gross"], 40.0)

	def test_a_transaction_this_site_did_not_make_is_ignored(self):
		notification = SimpleNamespace(
			kind=Kind.TransactionDisbursed,
			transaction=SimpleNamespace(id="not_ours", disbursement_details=None),
		)
		self.assertEqual(self.call(notification), {"status": "ignored"})

	def test_other_kinds_are_answered_and_dropped(self):
		notification = SimpleNamespace(kind=Kind.SubscriptionWentActive)
		self.assertEqual(self.call(notification), {"status": "ignored"})
