# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from types import MappingProxyType
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, flt, get_url

from payments.utils import create_payment_gateway
from payments.utils.recording import record_payment

currency_wise_minimum_charge_amount = {
	"JPY": 50,
	"MXN": 10,
	"DKK": 2.50,
	"HKD": 4.00,
	"NOK": 3.00,
	"SEK": 3.00,
	"USD": 0.50,
	"AUD": 0.50,
	"BRL": 0.50,
	"CAD": 0.50,
	"CHF": 0.50,
	"EUR": 0.50,
	"GBP": 0.30,
	"NZD": 0.50,
	"SGD": 0.50,
}


class StripeSettings(Document):
	supported_currencies = (
		"AED",
		"ALL",
		"ANG",
		"ARS",
		"AUD",
		"AWG",
		"BBD",
		"BDT",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CVE",
		"CZK",
		"DJF",
		"DKK",
		"DOP",
		"DZD",
		"EGP",
		"ETB",
		"EUR",
		"FJD",
		"FKP",
		"GBP",
		"GIP",
		"GMD",
		"GNF",
		"GTQ",
		"GYD",
		"HKD",
		"HNL",
		"HRK",
		"HTG",
		"HUF",
		"IDR",
		"ILS",
		"INR",
		"ISK",
		"JMD",
		"JPY",
		"KES",
		"KHR",
		"KMF",
		"KRW",
		"KYD",
		"KZT",
		"LAK",
		"LBP",
		"LKR",
		"LRD",
		"MAD",
		"MDL",
		"MNT",
		"MOP",
		"MRO",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"NAD",
		"NGN",
		"NIO",
		"NOK",
		"NPR",
		"NZD",
		"PAB",
		"PEN",
		"PGK",
		"PHP",
		"PKR",
		"PLN",
		"PYG",
		"QAR",
		"RUB",
		"SAR",
		"SBD",
		"SCR",
		"SEK",
		"SGD",
		"SHP",
		"SLL",
		"SOS",
		"STD",
		"SVC",
		"SZL",
		"THB",
		"TOP",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VND",
		"VUV",
		"WST",
		"XAF",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
	)

	currency_wise_minimum_charge_amount = MappingProxyType(currency_wise_minimum_charge_amount)

	def on_update(self):
		create_payment_gateway(
			"Stripe-" + self.gateway_name,
			settings="Stripe Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Stripe-" + self.gateway_name)
		if not self.flags.ignore_mandatory:
			self.validate_stripe_credentails()

	def validate_stripe_credentails(self):
		if self.publishable_key and self.secret_key:
			header = {
				"Authorization": "Bearer {}".format(
					self.get_password(fieldname="secret_key", raise_exception=False)
				)
			}
			try:
				make_get_request(url="https://api.stripe.com/v1/payment_intents?limit=1", headers=header)
			except Exception:
				frappe.throw(_("Seems Publishable Key or Secret Key is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Stripe does not support transactions in currency '{0}'"
				).format(currency)
			)

	def validate_minimum_transaction_amount(self, currency, amount):
		if currency in self.currency_wise_minimum_charge_amount:
			if flt(amount) < self.currency_wise_minimum_charge_amount.get(currency, 0.0):
				frappe.throw(
					_("For currency {0}, the minimum transaction amount should be {1}").format(
						currency, self.currency_wise_minimum_charge_amount.get(currency, 0.0)
					)
				)

	def get_payment_url(self, **kwargs):
		return get_url(f"./stripe_checkout?{urlencode(kwargs)}")

	def create_request(self, data):
		"""Charge the payment method the Payment Element created (decision 002 §2)."""
		import stripe

		self.data = frappe._dict(data)
		set_api_key(self)

		try:
			self.integration_request = create_request_log(self.data, service_name="Stripe")
			intent = stripe.PaymentIntent.create(
				amount=to_minor_units(self.data.amount, self.data.currency),
				currency=self.data.currency.lower(),
				payment_method=self.data.payment_method_id,
				payment_method_types=["card"],
				confirm=True,
				description=self.data.description,
				receipt_email=self.data.payer_email or None,
				metadata={
					"integration_request": self.integration_request.name,
					"reference_doctype": self.data.reference_doctype,
					"reference_docname": self.data.reference_docname,
				},
			)
			self.integration_request.db_set("request_id", intent.id, update_modified=False)
			return self.settle(intent)
		except stripe.error.CardError as e:
			# A decline: the payer can try another card on the same page.
			self.integration_request.db_set("status", "Failed", update_modified=False)
			return {"status": "Failed", "message": e.user_message}
		except Exception:
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def confirm_request(self, integration_request):
		"""After 3-D Secure, settle the intent or subscription the request started."""
		import stripe

		from payments.payment_gateways.stripe_integration import settle_subscription

		self.integration_request = integration_request
		self.data = frappe._dict(json.loads(integration_request.data))
		if integration_request.status == "Completed":
			return {"status": "Completed", "redirect_to": self.success_url()}
		set_api_key(self)
		if (integration_request.request_id or "").startswith("sub_"):
			subscription = stripe.Subscription.retrieve(
				integration_request.request_id, expand=["latest_invoice.payment_intent"]
			)
			return settle_subscription(self, subscription)
		return self.settle(stripe.PaymentIntent.retrieve(integration_request.request_id))

	def settle(self, intent):
		if intent.status == "succeeded":
			self.integration_request.db_set("status", "Completed", update_modified=False)
			self.flags.status_changed_to = "Completed"
		elif intent.status == "requires_action":
			return self.needs_action(intent.client_secret)
		else:
			self.integration_request.db_set("status", "Failed", update_modified=False)
			frappe.log_error(
				f"Stripe PaymentIntent {intent.id}: {intent.status}", "Stripe Payment not completed"
			)
		return self.finalize_request()

	def needs_action(self, client_secret):
		return {
			"status": "Queued",
			"requires_action": True,
			"client_secret": client_secret,
			"integration_request": self.integration_request.name,
		}

	def success_url(self):
		return f"payment-success?doctype={self.data.reference_doctype}&docname={self.data.reference_docname}"

	def finalize_request(self):
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		status = self.integration_request.status

		if self.flags.status_changed_to == "Completed":
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				custom_redirect_to = record_payment(
					self.integration_request,
					self.data.reference_doctype,
					self.data.reference_docname,
					self.flags.status_changed_to,
				)

				if custom_redirect_to:
					redirect_to = custom_redirect_to

				redirect_url = self.success_url()

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
		else:
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode({"redirect_to": redirect_to})

		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}


# Stripe charges these in whole units, not cents.
ZERO_DECIMAL_CURRENCIES = frozenset(
	(
		"BIF",
		"CLP",
		"DJF",
		"GNF",
		"JPY",
		"KMF",
		"KRW",
		"MGA",
		"PYG",
		"RWF",
		"UGX",
		"VND",
		"VUV",
		"XAF",
		"XOF",
		"XPF",
	)
)


def to_minor_units(amount, currency):
	if (currency or "").upper() in ZERO_DECIMAL_CURRENCIES:
		return cint(round(flt(amount)))
	return cint(round(flt(amount) * 100))


def set_api_key(settings):
	import stripe

	stripe.api_key = settings.get_password(fieldname="secret_key", raise_exception=False)
	stripe.default_http_client = stripe.http_client.RequestsClient()


def get_gateway_controller(doctype, docname, payment_gateway=None):
	if not payment_gateway:
		reference_doc = frappe.get_doc(doctype, docname)
		payment_gateway = reference_doc.payment_gateway
	return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")
