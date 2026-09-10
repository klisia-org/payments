# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

import json
import warnings
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.desk.doctype.todo.todo import ToDo
from frappe.integrations.utils import create_request_log
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, getdate, nowdate
from requests.exceptions import HTTPError
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from payments.payment_gateways.doctype.asaas_settings.asaas_settings import (
	MAX_CALLBACK_URL_LENGTH,
	AsaasSettings,
	as_user,
	get_asaas_error_message,
	get_description,
	get_success_url,
	is_valid_cpf_cnpj,
	sanitize_cpf_cnpj,
	webhook,
)

VALID_CPF = "111.444.777-35"
VALID_CNPJ = "11.222.333/0001-81"
WEBHOOK_TOKEN = "a" * 32
INVOICE = "https://sandbox.asaas.com/i/abc123"


class FakeAsaas:
	"""Stands in for Asaas at the seam the controller reaches it through.

	Patching `make_get_request`/`make_post_request` rather than the controller's
	own methods leaves the payloads under test: what these tests assert on is the
	body that would have gone over the wire.
	"""

	def __init__(self, replies=None):
		#: ("POST", "/payments") -> the parsed reply. Paths match by prefix, so a
		#: subscription's charges can be answered without knowing its id, and a
		#: reply that is an exception is raised instead of returned.
		self.replies = dict(replies or {})
		self.calls = []

	def get(self, url, headers=None, params=None):
		return self.answer("GET", url, params)

	def post(self, url, headers=None, json=None):
		return self.answer("POST", url, json)

	def answer(self, method, url, body):
		path = url.split("/v3", 1)[-1]
		self.calls.append((method, path, body))

		matches = [key for key in self.replies if key[0] == method and path.startswith(key[1])]
		if not matches:
			raise AssertionError(f"{method} {path} was not expected by this test")

		reply = self.replies[max(matches, key=lambda key: len(key[1]))]
		if isinstance(reply, Exception):
			if response := getattr(reply, "response", None):
				# what `make_request` leaves behind for the error to be read off
				frappe.flags.integration_request = response
			raise reply

		return reply

	def sent_to(self, path):
		"""The body of the one POST to `path`."""
		bodies = [body for method, called, body in self.calls if method == "POST" and called == path]
		if len(bodies) != 1:
			raise AssertionError(f"expected one POST to {path}, saw {len(bodies)}")

		return bodies[0]

	def installed(self):
		return patch.multiple(
			"payments.payment_gateways.doctype.asaas_settings.asaas_settings",
			make_get_request=self.get,
			make_post_request=self.post,
		)


class FakeResponse:
	"""Just enough of a `requests` response for the error reader."""

	def __init__(self, body):
		self.body = body

	def json(self):
		if self.body is None:
			raise ValueError("response body is not JSON")

		return self.body


@contextmanager
def contained():
	"""Keep the gateway's own commits inside the test transaction.

	Every Integration Request write on these paths commits - ours with
	`commit=True`, Frappe's own `update_status` unconditionally - which would
	otherwise outlive the rollback that isolates one test from the next.
	"""
	frappe.db._disable_transaction_control += 1
	try:
		with warnings.catch_warnings():
			warnings.simplefilter("ignore")
			yield
	finally:
		frappe.db._disable_transaction_control -= 1


@contextmanager
def asaas_calls_back(payload, token=WEBHOOK_TOKEN):
	"""Put a real webhook request in front of the handler, headers and all."""
	headers = {"asaas-access-token": token} if token is not None else {}
	environ = EnvironBuilder(method="POST", json=payload, headers=headers).get_environ()
	frappe.local.request = Request(environ)
	try:
		yield
	finally:
		delattr(frappe.local, "request")


def make_settings(**values):
	settings = frappe.get_doc({"doctype": "Asaas Settings", "api_key": "test-key", **values})
	# what `create_request` would have put there; the charge builders only read its name
	settings.integration_request = frappe._dict(name="IR-TEST")
	settings.data = frappe._dict(cpf_cnpj="11144477735")

	return settings


def payment_details(**overrides):
	return {
		"amount": 250,
		"title": "Tuition",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR0001",
		"payer_name": "Joao da Silva",
		"payer_email": "joao@example.com",
		"currency": "BRL",
		**overrides,
	}


def stored_password(self, fieldname="password", raise_exception=True):
	"""Answer for the credentials without touching the site's real ones."""
	return {"api_key": "test-key", "webhook_auth_token": WEBHOOK_TOKEN}.get(fieldname)


class UnitTestBrazilianDocuments(UnitTestCase):
	def test_accepts_well_formed_cpf_and_cnpj(self):
		self.assertTrue(is_valid_cpf_cnpj("11144477735"))
		self.assertTrue(is_valid_cpf_cnpj("11222333000181"))

	def test_rejects_malformed_cpf_and_cnpj(self):
		for value in (
			"11144477734",  # wrong check digit
			"11222333000180",  # wrong check digit
			"1114447773",  # too short
			"11111111111",  # repeated digits pass the check digits but are not issued
			"",
		):
			self.assertFalse(is_valid_cpf_cnpj(value), msg=value)

	def test_sanitize_strips_punctuation(self):
		self.assertEqual(sanitize_cpf_cnpj(VALID_CPF), "11144477735")
		self.assertEqual(sanitize_cpf_cnpj(VALID_CNPJ), "11222333000181")

	def test_sanitize_rejects_invalid_document(self):
		self.assertRaises(frappe.ValidationError, sanitize_cpf_cnpj, "111.444.777-34")
		self.assertRaises(frappe.ValidationError, sanitize_cpf_cnpj, None)


class UnitTestTheReturnUrl(UnitTestCase):
	def test_success_url_keeps_the_reference(self):
		url = get_success_url({"reference_doctype": "Payment Request", "reference_docname": "PR0001"})
		self.assertIn("doctype=Payment+Request", url)
		self.assertIn("docname=PR0001", url)

	def test_success_url_drops_redirect_to_when_too_long(self):
		url = get_success_url(
			{
				"reference_doctype": "Payment Request",
				"reference_docname": "PR0001",
				"redirect_to": "/" + "x" * MAX_CALLBACK_URL_LENGTH,
			}
		)
		self.assertLessEqual(len(url), MAX_CALLBACK_URL_LENGTH)
		self.assertNotIn("redirect_to", url)

	def test_no_success_url_without_a_reference(self):
		self.assertIsNone(get_success_url({"redirect_to": "/courses"}))

	def test_no_success_url_when_even_the_reference_will_not_fit(self):
		# Asaas rejects the whole charge over a too-long callback, so there is
		# nothing left to trim and no redirect is better than no payment
		self.assertIsNone(
			get_success_url(
				{"reference_doctype": "Payment Request", "reference_docname": "x" * MAX_CALLBACK_URL_LENGTH}
			)
		)


class UnitTestTheBillingCycle(UnitTestCase):
	def test_cycle_falls_back_to_the_configured_default(self):
		settings = make_settings(subscription_cycle="YEARLY")

		self.assertEqual(settings.get_cycle({}), "YEARLY")
		self.assertEqual(settings.get_cycle({"billing_period": "Month"}), "MONTHLY")
		self.assertEqual(settings.get_cycle({"cycle": "BIWEEKLY"}), "BIWEEKLY")

	def test_cycle_is_monthly_when_nothing_at_all_is_configured(self):
		self.assertEqual(make_settings().get_cycle({}), "MONTHLY")

	def test_cycle_rejects_a_period_asaas_does_not_bill_on(self):
		self.assertRaises(frappe.ValidationError, make_settings().get_cycle, {"cycle": "DAILY"})

	def test_a_period_asaas_cannot_charge_on_is_not_silently_rewritten(self):
		# "Day" is in the Razorpay vocabulary and "SemiMonth" in PayPal's; billing
		# someone monthly when they asked for daily is worse than not billing them
		settings = make_settings(subscription_cycle="MONTHLY")

		for period in ("Day", "SemiMonth"):
			with self.subTest(period=period):
				self.assertRaises(frappe.ValidationError, settings.get_cycle, {"billing_period": period})

	def test_an_explicit_cycle_wins_over_the_period(self):
		self.assertEqual(
			make_settings().get_cycle({"cycle": "QUARTERLY", "billing_period": "Month"}), "QUARTERLY"
		)


class UnitTestTheDueDate(UnitTestCase):
	def test_the_due_date_defaults_to_three_days_out(self):
		self.assertEqual(make_settings().get_due_date(), str(getdate(add_days(nowdate(), 3))))

	def test_the_due_date_follows_the_setting(self):
		self.assertEqual(
			make_settings(days_until_due=10).get_due_date(), str(getdate(add_days(nowdate(), 10)))
		)

	def test_a_charge_can_be_made_due_today(self):
		self.assertEqual(make_settings(days_until_due=0).get_due_date(), str(getdate(nowdate())))

	def test_a_requested_start_date_wins(self):
		self.assertEqual(make_settings(days_until_due=10).get_due_date("2026-10-01"), "2026-10-01")


class UnitTestFineAndInterest(UnitTestCase):
	def test_nothing_is_sent_when_nothing_is_charged(self):
		self.assertEqual(make_settings().get_fine_and_interest(), {})
		self.assertEqual(make_settings(fine_percentage=0, interest_percentage=0).get_fine_and_interest(), {})

	def test_the_fine_is_a_percentage_of_the_charge(self):
		self.assertEqual(
			make_settings(fine_percentage=2).get_fine_and_interest(),
			{"fine": {"value": 2.0, "type": "PERCENTAGE"}},
		)

	def test_interest_is_sent_on_its_own(self):
		self.assertEqual(
			make_settings(interest_percentage=1).get_fine_and_interest(), {"interest": {"value": 1.0}}
		)


class UnitTestTheDescription(UnitTestCase):
	def test_the_description_is_preferred_over_the_title(self):
		self.assertEqual(get_description({"title": "Tuition", "description": "Term 1"}), "Term 1")

	def test_the_title_is_used_when_there_is_no_description(self):
		self.assertEqual(get_description({"title": "Tuition"}), "Tuition")

	def test_a_charge_can_go_out_unlabelled(self):
		self.assertEqual(get_description({}), "")

	def test_the_description_is_cut_to_what_asaas_accepts(self):
		self.assertEqual(len(get_description({"description": "x" * 600})), 500)


class UnitTestTheEnvironment(UnitTestCase):
	def test_sandbox_is_assumed_until_told_otherwise(self):
		self.assertIn("sandbox", make_settings().base_url)

	def test_production_talks_to_the_live_api(self):
		self.assertEqual(make_settings(environment="Production").base_url, "https://api.asaas.com/v3")

	def test_only_brl_is_accepted(self):
		make_settings().validate_transaction_currency("BRL")
		self.assertRaises(frappe.ValidationError, make_settings().validate_transaction_currency, "USD")


class UnitTestTheReasonAsaasGave(UnitTestCase):
	def test_the_refusal_is_read_off_the_response(self):
		frappe.flags.integration_request = FakeResponse(
			{
				"errors": [
					{"description": "O valor deve ser maior que zero."},
					{"description": "CPF inválido."},
				]
			}
		)
		self.addCleanup(frappe.flags.pop, "integration_request", None)

		self.assertEqual(get_asaas_error_message(), "O valor deve ser maior que zero. CPF inválido.")

	def test_a_response_that_is_not_json_explains_nothing(self):
		frappe.flags.integration_request = FakeResponse(None)
		self.addCleanup(frappe.flags.pop, "integration_request", None)

		self.assertIsNone(get_asaas_error_message())

	def test_nothing_to_read_when_no_request_was_made(self):
		frappe.flags.pop("integration_request", None)

		self.assertIsNone(get_asaas_error_message())


class UnitTestTheSettingsForm(UnitTestCase):
	def test_the_url_asaas_must_call_is_published_on_save(self):
		settings = make_settings(webhook_auth_token=WEBHOOK_TOKEN)
		settings.validate()

		self.assertTrue(settings.webhook_url.endswith("asaas_settings.webhook"))

	def test_a_token_asaas_would_reject_is_refused_here(self):
		# Asaas only accepts an authToken of 32 to 255 characters, and it fails
		# the registration silently rather than at the first webhook
		self.assertRaises(frappe.ValidationError, make_settings(webhook_auth_token="short").validate)
		self.assertRaises(frappe.ValidationError, make_settings(webhook_auth_token="a" * 256).validate)


class UnitTestTheChargeAsaasReceives(UnitTestCase):
	def test_the_body_of_a_one_off_charge(self):
		settings = make_settings(billing_type="BOLETO", days_until_due=5)
		asaas = FakeAsaas({("POST", "/payments"): {"id": "pay_1", "invoiceUrl": INVOICE}})

		with asaas.installed():
			settings.create_payment("cus_1", payment_details(amount=250.4567))

		self.assertEqual(
			asaas.sent_to("/payments"),
			{
				"customer": "cus_1",
				"billingType": "BOLETO",
				"value": 250.46,
				"dueDate": str(getdate(add_days(nowdate(), 5))),
				"description": "Tuition",
				"externalReference": "IR-TEST",
				"callback": {"successUrl": get_success_url(payment_details()), "autoRedirect": True},
			},
		)

	def test_a_charge_with_nothing_to_come_back_to_carries_no_callback(self):
		settings = make_settings()
		asaas = FakeAsaas({("POST", "/payments"): {"id": "pay_1", "invoiceUrl": INVOICE}})

		with asaas.installed():
			settings.create_payment("cus_1", {"amount": 10})

		self.assertNotIn("callback", asaas.sent_to("/payments"))

	def test_the_late_terms_ride_along_with_the_charge(self):
		settings = make_settings(fine_percentage=2, interest_percentage=1)
		asaas = FakeAsaas({("POST", "/payments"): {"id": "pay_1", "invoiceUrl": INVOICE}})

		with asaas.installed():
			settings.create_payment("cus_1", payment_details())

		sent = asaas.sent_to("/payments")
		self.assertEqual(sent["fine"], {"value": 2.0, "type": "PERCENTAGE"})
		self.assertEqual(sent["interest"], {"value": 1.0})

	def test_a_charge_asaas_answers_without_an_id_is_not_taken_as_made(self):
		settings = make_settings()
		asaas = FakeAsaas({("POST", "/payments"): {}})

		with asaas.installed():
			self.assertRaises(frappe.ValidationError, settings.create_payment, "cus_1", payment_details())

	def test_the_reason_a_charge_was_refused_reaches_the_payer(self):
		settings = make_settings()
		refusal = HTTPError(response=FakeResponse({"errors": [{"description": "O valor mínimo é R$ 5,00."}]}))
		asaas = FakeAsaas({("POST", "/payments"): refusal})
		self.addCleanup(frappe.flags.pop, "integration_request", None)

		with asaas.installed():
			with self.assertRaises(frappe.ValidationError) as refused:
				settings.create_payment("cus_1", payment_details(amount=1))

		self.assertIn("R$ 5,00", str(refused.exception))


class UnitTestTheSubscriptionAsaasReceives(UnitTestCase):
	def test_the_body_of_a_recurring_charge(self):
		settings = make_settings(days_until_due=3)
		asaas = FakeAsaas({("POST", "/subscriptions"): {"id": "sub_1"}})
		data = payment_details(
			subscription_details={
				"billing_period": "Month",
				"billing_frequency": 12,
				"end_date": "2027-10-01",
			}
		)

		with asaas.installed():
			settings.create_subscription("cus_1", data)

		sent = asaas.sent_to("/subscriptions")
		self.assertEqual(sent["cycle"], "MONTHLY")
		self.assertEqual(sent["maxPayments"], 12)
		self.assertEqual(sent["endDate"], "2027-10-01")
		self.assertEqual(sent["nextDueDate"], str(getdate(add_days(nowdate(), 3))))
		self.assertEqual(sent["externalReference"], "IR-TEST")

	def test_a_subscription_can_be_told_when_to_start(self):
		settings = make_settings()
		asaas = FakeAsaas({("POST", "/subscriptions"): {"id": "sub_1"}})
		data = payment_details(subscription_details={"cycle": "YEARLY", "start_date": "2026-10-01"})

		with asaas.installed():
			settings.create_subscription("cus_1", data)

		self.assertEqual(asaas.sent_to("/subscriptions")["nextDueDate"], "2026-10-01")

	def test_an_open_ended_subscription_carries_no_limit(self):
		settings = make_settings()
		asaas = FakeAsaas({("POST", "/subscriptions"): {"id": "sub_1"}})

		with asaas.installed():
			settings.create_subscription("cus_1", payment_details(subscription_details={}))

		sent = asaas.sent_to("/subscriptions")
		self.assertNotIn("maxPayments", sent)
		self.assertNotIn("endDate", sent)

	def test_a_subscription_asaas_answers_without_an_id_is_not_taken_as_made(self):
		settings = make_settings()
		asaas = FakeAsaas({("POST", "/subscriptions"): {}})

		with asaas.installed():
			self.assertRaises(
				frappe.ValidationError,
				settings.create_subscription,
				"cus_1",
				payment_details(subscription_details={}),
			)

	def test_the_payer_is_sent_to_the_first_charge_a_subscription_raised(self):
		settings = make_settings()
		asaas = FakeAsaas({("GET", "/subscriptions"): {"data": [{"id": "pay_1", "bankSlipUrl": INVOICE}]}})

		with asaas.installed():
			self.assertEqual(settings.get_subscription_invoice_url("sub_1"), INVOICE)

	def test_a_subscription_that_has_raised_nothing_yet_has_nowhere_to_send_them(self):
		settings = make_settings()
		asaas = FakeAsaas({("GET", "/subscriptions"): {"data": []}})

		with asaas.installed():
			self.assertIsNone(settings.get_subscription_invoice_url("sub_1"))


class UnitTestTheAsaasCustomer(UnitTestCase):
	def test_a_payer_asaas_already_knows_is_not_created_twice(self):
		settings = make_settings()
		asaas = FakeAsaas({("GET", "/customers"): {"data": [{"id": "cus_1"}]}})

		with asaas.installed():
			self.assertEqual(settings.get_or_create_customer(payment_details()), "cus_1")

		self.assertEqual(asaas.calls[0][2], {"cpfCnpj": "11144477735", "limit": 1})
		self.assertNotIn("POST", [method for method, _path, _body in asaas.calls])

	def test_a_customer_asaas_has_deleted_is_not_reused(self):
		settings = make_settings()
		asaas = FakeAsaas(
			{
				("GET", "/customers"): {"data": [{"id": "cus_old", "deleted": True}]},
				("POST", "/customers"): {"id": "cus_new"},
			}
		)

		with asaas.installed():
			self.assertEqual(settings.get_or_create_customer(payment_details()), "cus_new")

	def test_the_payer_asaas_is_asked_to_create(self):
		settings = make_settings()
		asaas = FakeAsaas({("GET", "/customers"): {"data": []}, ("POST", "/customers"): {"id": "cus_new"}})

		with asaas.installed():
			settings.get_or_create_customer(payment_details(payer_phone="+55 (11) 98888-7777"))

		self.assertEqual(
			asaas.sent_to("/customers"),
			{
				"name": "Joao da Silva",
				"cpfCnpj": "11144477735",
				"email": "joao@example.com",
				"externalReference": "joao@example.com",
				"mobilePhone": "5511988887777",
			},
		)

	def test_a_payer_without_a_phone_is_sent_without_one(self):
		settings = make_settings()
		asaas = FakeAsaas({("GET", "/customers"): {"data": []}, ("POST", "/customers"): {"id": "cus_new"}})

		with asaas.installed():
			settings.get_or_create_customer(payment_details())

		self.assertNotIn("mobilePhone", asaas.sent_to("/customers"))

	def test_a_customer_asaas_would_not_create_stops_the_charge(self):
		settings = make_settings()
		asaas = FakeAsaas({("GET", "/customers"): {"data": []}, ("POST", "/customers"): {}})

		with asaas.installed():
			self.assertRaises(frappe.ValidationError, settings.get_or_create_customer, payment_details())


class AsaasIntegrationTestCase(IntegrationTestCase):
	"""Shared ground for the tests that put real Integration Requests through.

	`Integration Request.reference_docname` is a Dynamic Link, so a charge has to
	name a document that exists. A ToDo is the cheapest one that does, and it
	doubles as the consumer whose `on_payment_authorized` the webhook calls.
	"""

	def setUp(self):
		super().setUp()
		self.reference = frappe.get_doc({"doctype": "ToDo", "description": "Asaas test charge"}).insert()

	def details(self, **overrides):
		return payment_details(reference_doctype="ToDo", reference_docname=self.reference.name, **overrides)

	def make_request(self, status=None, ids=None, **overrides):
		with contained():
			request = create_request_log(self.details(**overrides), service_name="Asaas")
			if ids or status:
				request.update_status(ids or {}, status or request.status)

		return request


class IntegrationTestStartingAPayment(AsaasIntegrationTestCase):
	def test_the_payer_is_sent_to_the_page_that_collects_the_cpf(self):
		with contained():
			url = make_settings().get_payment_url(**self.details())

		self.assertIn("asaas_checkout?token=", url)
		token = url.rsplit("token=", 1)[1]
		self.assertEqual(
			frappe.db.get_value("Integration Request", token, "integration_request_service"), "Asaas"
		)

	def test_a_currency_asaas_cannot_settle_is_refused_before_anything_is_logged(self):
		settings = make_settings()

		with self.assertRaises(frappe.ValidationError):
			settings.get_payment_url(**self.details(currency="USD"))


class IntegrationTestCreatingTheCharge(AsaasIntegrationTestCase):
	def test_the_payer_is_sent_to_the_asaas_invoice(self):
		request = self.make_request()
		asaas = FakeAsaas(
			{
				("GET", "/customers"): {"data": []},
				("POST", "/customers"): {"id": "cus_1"},
				("POST", "/payments"): {"id": "pay_1", "invoiceUrl": INVOICE},
			}
		)

		with asaas.installed(), contained():
			result = make_settings().create_request(
				{"token": request.name, "cpf_cnpj": VALID_CPF, "payer_name": "Joao da Silva"}
			)

		self.assertEqual(result, {"redirect_to": INVOICE, "status": 200})

		request.reload()
		self.assertEqual(request.status, "Queued")
		self.assertEqual(json.loads(request.data)["asaas_payment_id"], "pay_1")
		self.assertEqual(json.loads(request.data)["asaas_customer_id"], "cus_1")
		self.assertEqual(json.loads(request.output)["id"], "pay_1")

	def test_a_boleto_only_charge_is_still_a_place_to_send_the_payer(self):
		request = self.make_request()
		asaas = FakeAsaas(
			{
				("GET", "/customers"): {"data": [{"id": "cus_1"}]},
				("POST", "/payments"): {"id": "pay_1", "bankSlipUrl": INVOICE},
			}
		)

		with asaas.installed(), contained():
			result = make_settings().create_request({"token": request.name, "cpf_cnpj": VALID_CPF})

		self.assertEqual(result["redirect_to"], INVOICE)

	def test_the_cpf_is_not_kept_on_the_integration_request(self):
		# it is only needed to find the payer in Asaas, and the Integration
		# Request is readable by anyone who can see the payment
		request = self.make_request()
		asaas = FakeAsaas(
			{
				("GET", "/customers"): {"data": [{"id": "cus_1"}]},
				("POST", "/payments"): {"id": "pay_1", "invoiceUrl": INVOICE},
			}
		)

		with asaas.installed(), contained():
			make_settings().create_request(
				{"token": request.name, "cpf_cnpj": VALID_CPF, "payer_name": "Joao da Silva"}
			)

		request.reload()
		self.assertNotIn("11144477735", request.data)
		self.assertNotIn("cpf_cnpj", json.loads(request.data))
		self.assertEqual(json.loads(request.data)["payer_name"], "Joao da Silva")

	def test_a_malformed_cpf_never_reaches_asaas(self):
		request = self.make_request()
		asaas = FakeAsaas()

		with asaas.installed(), contained():
			self.assertRaises(
				frappe.ValidationError,
				make_settings().create_request,
				{"token": request.name, "cpf_cnpj": "111.444.777-34"},
			)

		self.assertEqual(asaas.calls, [])

	def test_a_subscription_redirects_to_the_charge_it_raised(self):
		request = self.make_request(subscription_details={"billing_period": "Month", "billing_frequency": 12})
		asaas = FakeAsaas(
			{
				("GET", "/customers"): {"data": [{"id": "cus_1"}]},
				("POST", "/subscriptions"): {"id": "sub_1"},
				("GET", "/subscriptions"): {"data": [{"id": "pay_1", "invoiceUrl": INVOICE}]},
			}
		)

		with asaas.installed(), contained():
			result = make_settings().create_request({"token": request.name, "cpf_cnpj": VALID_CNPJ})

		self.assertEqual(result["redirect_to"], INVOICE)
		self.assertEqual(asaas.sent_to("/subscriptions")["cycle"], "MONTHLY")
		self.assertEqual(asaas.sent_to("/subscriptions")["externalReference"], request.name)

		request.reload()
		self.assertEqual(json.loads(request.data)["asaas_subscription_id"], "sub_1")

	def test_a_charge_with_nowhere_to_send_the_payer_is_not_reported_as_started(self):
		# Asaas took the charge but gave back no invoice, so there is nothing to
		# redirect to. The payer is told and the request keeps the reason, rather
		# than the charge being announced as started
		request = self.make_request()
		asaas = FakeAsaas(
			{
				("GET", "/customers"): {"data": [{"id": "cus_1"}]},
				("POST", "/payments"): {"id": "pay_1"},
			}
		)

		with asaas.installed(), contained():
			with self.assertRaises(frappe.ValidationError):
				make_settings().create_request({"token": request.name, "cpf_cnpj": VALID_CPF})

		request.reload()
		self.assertNotEqual(request.status, "Completed")
		self.assertIn("did not return an invoice", request.error)

	def test_a_broken_configuration_does_not_read_as_a_refused_document(self):
		request = self.make_request()
		asaas = FakeAsaas({("GET", "/customers"): RuntimeError("connection refused")})

		with asaas.installed(), contained():
			result = make_settings().create_request({"token": request.name, "cpf_cnpj": VALID_CPF})

		self.assertEqual(result["status"], 401)
		request.reload()
		self.assertIn("connection refused", request.error)


class IntegrationTestRegisteringTheWebhook(IntegrationTestCase):
	def test_asaas_is_asked_for_the_events_this_app_acts_on(self):
		settings = make_settings(webhook_auth_token=WEBHOOK_TOKEN)
		asaas = FakeAsaas({("POST", "/webhooks"): {"id": "wh_1"}})

		with asaas.installed():
			url = settings.register_webhook()

		sent = asaas.sent_to("/webhooks")
		self.assertEqual(sent["url"], url)
		self.assertEqual(sent["authToken"], WEBHOOK_TOKEN)
		self.assertEqual(sent["apiVersion"], 3)
		self.assertTrue(sent["enabled"])
		for event in ("PAYMENT_RECEIVED", "PAYMENT_CONFIRMED", "PAYMENT_REFUNDED", "PAYMENT_OVERDUE"):
			self.assertIn(event, sent["events"])

	def test_a_registration_asaas_did_not_accept_is_not_reported_as_done(self):
		settings = make_settings(webhook_auth_token=WEBHOOK_TOKEN)
		asaas = FakeAsaas({("POST", "/webhooks"): {}})

		with asaas.installed():
			self.assertRaises(frappe.ValidationError, settings.register_webhook)


class IntegrationTestTheWebhook(AsaasIntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.enterContext(patch.object(AsaasSettings, "get_password", stored_password))

	def call(self, payload, token=WEBHOOK_TOKEN):
		with asaas_calls_back(payload, token=token), contained():
			return webhook()

	def paid(self, request=None, **payment):
		return {"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_1", **payment}}

	# --- the token ------------------------------------------------------

	def test_a_call_without_the_token_is_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self.call(self.paid(), token=None)

	def test_a_call_with_the_wrong_token_is_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self.call(self.paid(), token="b" * 32)

	# --- finding the charge ---------------------------------------------

	def test_a_paid_charge_completes_the_request(self):
		request = self.make_request()

		self.assertEqual(self.call(self.paid(externalReference=request.name)), {"status": "Completed"})

		request.reload()
		self.assertEqual(request.status, "Completed")
		self.assertEqual(json.loads(request.data)["asaas_payment_id"], "pay_1")

	def test_a_charge_is_matched_by_the_id_asaas_gave_it(self):
		# a renewal carries no externalReference, so the ids stored on the
		# request are the only way back to it
		request = self.make_request(status="Queued", ids={"asaas_payment_id": "pay_9"})

		self.assertEqual(self.call(self.paid(id="pay_9")), {"status": "Completed"})

		request.reload()
		self.assertEqual(request.status, "Completed")

	def test_a_renewal_is_matched_by_its_subscription(self):
		request = self.make_request(
			status="Queued",
			ids={"asaas_subscription_id": "sub_1"},
			subscription_details={"cycle": "MONTHLY"},
		)

		self.assertEqual(self.call(self.paid(id="pay_2", subscription="sub_1")), {"status": "Completed"})

		request.reload()
		self.assertEqual(request.status, "Completed")

	def test_a_charge_this_site_did_not_create_is_answered_and_dropped(self):
		self.assertEqual(self.call(self.paid(id="pay_from_another_account")), {"status": "ignored"})

	def test_a_call_that_names_no_charge_is_answered_and_dropped(self):
		self.assertEqual(self.call({"event": "PAYMENT_RECEIVED"}), {"status": "ignored"})
		self.assertEqual(self.call({"payment": {"id": "pay_1"}}), {"status": "ignored"})

	# --- what it does with the charge -----------------------------------

	def test_the_reference_is_told_once_and_told_as_the_payer(self):
		request = self.make_request()
		payload = self.paid(externalReference=request.name)
		told = []

		def remember(self, payment_status):
			told.append((self.name, payment_status, frappe.session.user))

		with patch.object(ToDo, "on_payment_authorized", remember, create=True):
			# the webhook itself arrives unauthenticated, which is the whole
			# reason the reference is run as the user who started the payment
			frappe.set_user("Guest")
			self.addCleanup(frappe.set_user, "Administrator")

			self.assertEqual(self.call(payload), {"status": "Completed"})
			self.assertEqual(self.call(payload), {"status": "already handled"})

		self.assertEqual(told, [(self.reference.name, "Completed", request.owner)])
		self.assertEqual(frappe.session.user, "Guest")

	def test_a_reference_that_breaks_does_not_break_the_delivery(self):
		# Asaas interrupts the queue after repeated failures, so a broken
		# consumer must not cost the site every later event
		request = self.make_request()

		def explode(self, payment_status):
			raise RuntimeError("the consuming app is broken")

		with patch.object(ToDo, "on_payment_authorized", explode, create=True):
			self.assertEqual(self.call(self.paid(externalReference=request.name)), {"status": "Completed"})

		request.reload()
		self.assertEqual(request.status, "Completed")

	def test_a_refused_charge_fails_the_request(self):
		request = self.make_request()
		payload = {
			"event": "PAYMENT_REPROVED_BY_RISK_ANALYSIS",
			"payment": {"id": "pay_1", "externalReference": request.name},
		}

		self.assertEqual(self.call(payload), {"status": "Failed"})

		request.reload()
		self.assertEqual(request.status, "Failed")
		self.assertIn("PAYMENT_REPROVED_BY_RISK_ANALYSIS", request.error)

	def test_a_failure_is_not_recorded_twice(self):
		request = self.make_request()
		payload = {"event": "PAYMENT_DELETED", "payment": {"id": "pay_1", "externalReference": request.name}}

		self.assertEqual(self.call(payload), {"status": "Failed"})
		self.assertEqual(self.call(payload), {"status": "already handled"})

	def test_an_event_the_gateway_does_not_act_on_is_still_answered(self):
		# answering anything else would have Asaas interrupt the whole queue
		request = self.make_request()
		payload = {"event": "PAYMENT_OVERDUE", "payment": {"id": "pay_1", "externalReference": request.name}}

		self.assertEqual(self.call(payload), {"status": "ignored"})

		request.reload()
		self.assertEqual(request.status, "Queued")

	def test_a_renewal_after_the_first_charge_is_logged_for_the_consuming_app(self):
		request = self.make_request(
			status="Completed",
			ids={"asaas_subscription_id": "sub_1"},
			subscription_details={"cycle": "MONTHLY"},
		)

		self.assertEqual(
			self.call(self.paid(id="pay_3", subscription="sub_1")), {"status": "already handled"}
		)

		renewals = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "Asaas",
				"data": ["like", f'%"original_request": "{request.name}"%'],
			},
			fields=["name", "status", "reference_doctype", "reference_docname"],
		)
		self.assertEqual(len(renewals), 1)
		self.assertEqual(renewals[0].status, "Completed")
		self.assertEqual(renewals[0].reference_docname, self.reference.name)
		self.assertIn("pay_3", frappe.db.get_value("Integration Request", renewals[0].name, "data"))

	def test_a_redelivered_one_off_charge_raises_no_renewal(self):
		request = self.make_request(status="Completed")

		self.assertEqual(self.call(self.paid(externalReference=request.name)), {"status": "already handled"})

		self.assertFalse(
			frappe.get_all(
				"Integration Request",
				filters={
					"integration_request_service": "Asaas",
					"data": ["like", f'%"original_request": "{request.name}"%'],
				},
			)
		)


class IntegrationTestRunningAsThePayer(IntegrationTestCase):
	def test_the_session_is_restored_even_when_the_reference_raises(self):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with self.assertRaises(RuntimeError):
			with as_user("Administrator"):
				self.assertEqual(frappe.session.user, "Administrator")
				raise RuntimeError("the consuming app is broken")

		self.assertEqual(frappe.session.user, "Guest")
