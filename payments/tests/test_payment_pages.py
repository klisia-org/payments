"""Card entry pages and gateway capabilities (decision 002)."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import to_minor_units
from payments.utils import capabilities, payment_page

PACKAGE = Path(__file__).resolve().parents[1]
PAGES = PACKAGE / "templates" / "pages"
GATEWAYS = PACKAGE / "payment_gateways" / "doctype"


def _frame_gateways():
	return {
		settings.split()[0].lower()
		for settings, c in capabilities.CAPABILITIES.items()
		if c["card_entry"] == "frame"
	}


class UnitTestPaymentPages(UnitTestCase):
	def test_every_frame_gateway_checkout_is_a_bare_page(self):
		for gateway in _frame_gateways():
			page = (PAGES / f"{gateway}_checkout.html").read_text()
			self.assertTrue(page.startswith('{% extends "templates/payment_page.html" %}'), gateway)
			self.assertNotIn("<script", page, gateway)

	def test_the_bare_page_loads_no_site_scripts(self):
		page = re.sub(r"\{#.*?#\}", "", (PACKAGE / "templates" / "payment_page.html").read_text(), flags=re.S)
		self.assertNotIn("{% extends", page)
		for site_wide in ("head_html", "head_include", "body_include", "web_include", "website_script"):
			self.assertNotIn(site_wide, page)

	def test_the_policy_allows_scripts_only_from_us_and_the_gateway(self):
		for gateway, sources in payment_page.GATEWAY_SOURCES.items():
			directives = dict(d.split(" ", 1) for d in payment_page.policy(gateway).split("; "))
			self.assertEqual(directives["script-src"].split(), ["'self'", *sources["script-src"]])
			self.assertEqual(directives["object-src"], "'none'")
			self.assertTrue(payment_page.SCRIPTS[gateway].startswith(tuple(sources["script-src"])))

	def test_every_gateway_declares_its_capabilities(self):
		settings = {
			json.loads(path.read_text())["name"] for path in GATEWAYS.glob("*_settings/*_settings.json")
		}
		self.assertEqual(settings, set(capabilities.CAPABILITIES))
		self.assertEqual(_frame_gateways(), set(payment_page.GATEWAY_SOURCES))

	def test_zero_decimal_currencies_are_not_multiplied(self):
		self.assertEqual(to_minor_units(10.5, "usd"), 1050)
		self.assertEqual(to_minor_units(500, "JPY"), 500)


class TestStripePaymentIntents(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe.new_doc("Stripe Settings")
		self.settings.gateway_name = "Test"
		todo = frappe.get_doc({"doctype": "ToDo", "description": "Pay"}).insert(ignore_permissions=True)
		self.data = {
			"amount": 25,
			"currency": "USD",
			"description": "Test payment",
			"payer_email": "payer@example.com",
			"reference_doctype": "ToDo",
			"reference_docname": todo.name,
			"payment_method_id": "pm_test",
		}
		self._patches = [
			patch.object(type(self.settings), "get_password", lambda *a, **k: "sk_test"),
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.record_payment"),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in reversed(self._patches):
			p.stop()
		frappe.db.rollback()

	def intent(self, status):
		return SimpleNamespace(id="pi_test", status=status, client_secret="pi_test_secret")

	def test_a_card_that_needs_3ds_is_settled_after_it(self):
		with patch("stripe.PaymentIntent.create", return_value=self.intent("requires_action")) as create:
			result = self.settings.create_request(self.data)
		self.assertEqual(create.call_args.kwargs["amount"], 2500)
		self.assertEqual(create.call_args.kwargs["payment_method_types"], ["card"])
		self.assertTrue(result["requires_action"])
		request = frappe.get_doc("Integration Request", result["integration_request"])
		self.assertEqual(request.request_id, "pi_test")

		with patch("stripe.PaymentIntent.retrieve", return_value=self.intent("succeeded")):
			done = frappe.new_doc("Stripe Settings").confirm_request(request)
		self.assertEqual(done["status"], "Completed")
		self.assertEqual(frappe.db.get_value("Integration Request", request.name, "status"), "Completed")

	def test_a_decline_lets_the_payer_try_again(self):
		import stripe

		decline = stripe.error.CardError("Your card was declined.", None, "card_declined")
		with patch("stripe.PaymentIntent.create", side_effect=decline):
			result = self.settings.create_request(self.data)
		self.assertEqual(result, {"status": "Failed", "message": "Your card was declined."})
