# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

"""
# Integrating Asaas

Asaas (https://asaas.com) is a Brazilian gateway. It settles in BRL only and
every charge belongs to a customer identified by a CPF/CNPJ, so the payer is
first sent to the `asaas_checkout` page to fill that in. The charge is created
from there and the payer is forwarded to the Asaas hosted invoice, where Boleto,
Pix and credit card are offered.

### 1. Validate Currency

Example:

	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("Asaas")
	controller.validate_transaction_currency(currency)

### 2. Redirect for payment

Example:

	payment_details = {
		"amount": 600,
		"title": "Payment for bill : 111",
		"description": "payment via cart",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR0001",
		"payer_email": "joao@example.com",
		"payer_name": "Joao da Silva",
		"currency": "BRL",
		"payment_gateway": "Asaas",
		"subscription_details": {  # only if the charge should recur
			"billing_period": "Month",  # or pass "cycle": "MONTHLY" directly
			"billing_frequency": 12,  # maximum number of charges, optional
			"start_date": "2026-10-01",  # due date of the first charge, optional
		},
	}

	# Redirect the user to this url
	url = controller.get_payment_url(**payment_details)

### 3. On Completion of Payment

Write a method for `on_payment_authorized` in the reference doctype.

Example:

	def on_payment_authorized(payment_status):
		# this method will be called when payment is complete

Asaas confirms money out of band, so `on_payment_authorized` is called from the
webhook and never from the browser redirect - a Boleto is typically paid days
after the payer has left the site. Register the webhook URL shown on Asaas
Settings before going live, otherwise no payment is ever marked as completed.
"""

import hmac
import json
import re
from contextlib import contextmanager
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import (
	create_request_log,
	make_get_request,
	make_post_request,
)
from frappe.model.document import Document
from frappe.utils import add_days, call_hook_method, cint, flt, get_url, getdate, nowdate

from payments.utils import create_payment_gateway

API_BASE_URL = {
	"Sandbox": "https://api-sandbox.asaas.com/v3",
	"Production": "https://api.asaas.com/v3",
}

# https://docs.asaas.com/reference/create-new-subscription
BILLING_CYCLES = (
	"WEEKLY",
	"BIWEEKLY",
	"MONTHLY",
	"BIMONTHLY",
	"QUARTERLY",
	"SEMIANNUALLY",
	"YEARLY",
)

# Maps the `billing_period` convention already used by the Razorpay controller
BILLING_PERIOD_TO_CYCLE = {
	"Week": "WEEKLY",
	"Fortnight": "BIWEEKLY",
	"Month": "MONTHLY",
	"Quarter": "QUARTERLY",
	"Half-Yearly": "SEMIANNUALLY",
	"Year": "YEARLY",
}

# https://docs.asaas.com/docs/payment-events
PAID_EVENTS = ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED")
FAILED_EVENTS = (
	"PAYMENT_DELETED",
	"PAYMENT_REFUNDED",
	"PAYMENT_CREDIT_CARD_CAPTURE_REFUSED",
	"PAYMENT_REPROVED_BY_RISK_ANALYSIS",
	"PAYMENT_CHARGEBACK_REQUESTED",
)
WEBHOOK_EVENTS = ("PAYMENT_CREATED", *PAID_EVENTS, "PAYMENT_OVERDUE", *FAILED_EVENTS)

# Asaas rejects a callback.successUrl longer than this
MAX_CALLBACK_URL_LENGTH = 255


class AsaasSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password
		billing_type: DF.Literal["UNDEFINED", "BOLETO", "PIX", "CREDIT_CARD"]
		days_until_due: DF.Int
		environment: DF.Literal["Sandbox", "Production"]
		fine_percentage: DF.Percent
		interest_percentage: DF.Percent
		subscription_cycle: DF.Literal[
			"WEEKLY", "BIWEEKLY", "MONTHLY", "BIMONTHLY", "QUARTERLY", "SEMIANNUALLY", "YEARLY"
		]
		webhook_auth_token: DF.Password
		webhook_url: DF.Data | None
	# end: auto-generated types

	supported_currencies = ("BRL",)

	def validate(self):
		self.webhook_url = get_webhook_url()
		self.validate_webhook_auth_token()

	def on_update(self):
		create_payment_gateway("Asaas")
		call_hook_method("payment_gateway_enabled", gateway="Asaas")
		if not self.flags.ignore_mandatory:
			self.validate_asaas_credentials()

	def validate_webhook_auth_token(self):
		# Asaas only accepts a webhook whose authToken is 32 to 255 characters long
		token = self.get_password("webhook_auth_token", raise_exception=False)
		if token and not 32 <= len(token) <= 255:
			frappe.throw(_("Webhook Auth Token must be between 32 and 255 characters long."))

	def validate_asaas_credentials(self):
		try:
			self.get_request("/customers", params={"limit": 1})
		except Exception:
			frappe.throw(
				_("Seems the API Key is wrong or does not belong to the {0} environment !!!").format(
					self.environment
				)
			)

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Asaas does not support transactions in currency '{0}'"
				).format(currency)
			)

	# --- HTTP -------------------------------------------------------------

	@property
	def base_url(self):
		return API_BASE_URL[self.environment or "Sandbox"]

	def get_headers(self):
		return {
			"access_token": self.get_password("api_key", raise_exception=False),
			"Content-Type": "application/json",
		}

	def get_request(self, path, params=None):
		return make_get_request(f"{self.base_url}{path}", headers=self.get_headers(), params=params)

	def post_request(self, path, payload):
		try:
			return make_post_request(f"{self.base_url}{path}", headers=self.get_headers(), json=payload)
		except Exception:
			# Asaas explains rejections - a value below its minimum, a document it
			# refuses - well enough to show the payer
			if message := get_asaas_error_message():
				frappe.throw(message, title=_("Asaas could not accept this charge"))
			raise

	# --- Charge creation --------------------------------------------------

	def get_payment_url(self, **kwargs):
		self.validate_transaction_currency(kwargs.get("currency") or "BRL")
		integration_request = create_request_log(kwargs, service_name="Asaas")
		return get_url(f"./asaas_checkout?token={integration_request.name}")

	def create_request(self, data):
		"""Create the Asaas charge and return the hosted invoice to redirect to.

		Called by the `asaas_checkout` page once the payer has supplied the
		CPF/CNPJ that Asaas requires on every customer.
		"""
		self.data = frappe._dict(data)
		# validated up front so a typo reaches the payer instead of the generic
		# server error the rest of this method falls back to
		self.data.cpf_cnpj = sanitize_cpf_cnpj(self.data.cpf_cnpj)
		self.integration_request = None

		try:
			self.integration_request = frappe.get_doc("Integration Request", self.data.token)
			# the CPF/CNPJ is only needed to look the customer up in Asaas, so it
			# is deliberately kept out of the Integration Request
			payer = {key: self.data.get(key) for key in ("payer_name", "payer_phone") if self.data.get(key)}
			self.integration_request.update_status(payer, "Queued")
			return self.create_charge()

		except Exception as e:
			if self.integration_request:
				self.integration_request.db_set(
					"error", frappe.get_traceback(), update_modified=False, commit=True
				)
			frappe.log_error(frappe.get_traceback(), "Asaas Charge Creation Failed")

			if isinstance(e, frappe.ValidationError):
				# something the payer can act on, not a misconfigured site
				raise

			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"It seems that there is an issue with the server's Asaas configuration. No payment has been made."
					),
				),
				"status": 401,
			}

	def create_charge(self):
		data = frappe._dict(json.loads(self.integration_request.data))
		customer_id = self.get_or_create_customer(data)

		if data.get("subscription_details"):
			charge = self.create_subscription(customer_id, data)
			invoice_url = self.get_subscription_invoice_url(charge.get("id"))
			ids = {"asaas_customer_id": customer_id, "asaas_subscription_id": charge.get("id")}
		else:
			charge = self.create_payment(customer_id, data)
			invoice_url = charge.get("invoiceUrl") or charge.get("bankSlipUrl")
			ids = {"asaas_customer_id": customer_id, "asaas_payment_id": charge.get("id")}

		if not invoice_url:
			frappe.throw(_("Asaas did not return an invoice to redirect the payer to."))

		self.integration_request.update_status(ids, "Queued")
		self.integration_request.db_set("output", frappe.as_json(charge), update_modified=False, commit=True)

		return {"redirect_to": invoice_url, "status": 200}

	def get_or_create_customer(self, data):
		cpf_cnpj = self.data.cpf_cnpj

		existing = self.get_request("/customers", params={"cpfCnpj": cpf_cnpj, "limit": 1})
		for customer in (existing or {}).get("data") or []:
			if not customer.get("deleted"):
				return customer["id"]

		payload = {
			"name": data.get("payer_name"),
			"cpfCnpj": cpf_cnpj,
			"email": data.get("payer_email"),
			"externalReference": data.get("payer_email"),
		}
		if data.get("payer_phone"):
			payload["mobilePhone"] = re.sub(r"\D", "", data["payer_phone"])

		customer = self.post_request("/customers", payload)
		if not (customer or {}).get("id"):
			frappe.throw(_("Could not create the customer in Asaas."))

		return customer["id"]

	def create_payment(self, customer_id, data):
		payload = {
			"customer": customer_id,
			"billingType": self.billing_type or "UNDEFINED",
			"value": flt(data.get("amount"), 2),
			"dueDate": self.get_due_date(),
			"description": get_description(data),
			"externalReference": self.integration_request.name,
		}
		payload.update(self.get_fine_and_interest())

		if success_url := get_success_url(data):
			payload["callback"] = {"successUrl": success_url, "autoRedirect": True}

		payment = self.post_request("/payments", payload)
		if not (payment or {}).get("id"):
			frappe.throw(_("Could not create the charge in Asaas."))

		return payment

	def create_subscription(self, customer_id, data):
		details = frappe._dict(data.get("subscription_details") or {})

		payload = {
			"customer": customer_id,
			"billingType": self.billing_type or "UNDEFINED",
			"value": flt(data.get("amount"), 2),
			"nextDueDate": self.get_due_date(details.get("start_date")),
			"cycle": self.get_cycle(details),
			"description": get_description(data),
			"externalReference": self.integration_request.name,
		}
		payload.update(self.get_fine_and_interest())

		if billing_frequency := cint(details.get("billing_frequency")):
			payload["maxPayments"] = billing_frequency

		if details.get("end_date"):
			payload["endDate"] = str(getdate(details["end_date"]))

		if success_url := get_success_url(data):
			payload["callback"] = {"successUrl": success_url, "autoRedirect": True}

		subscription = self.post_request("/subscriptions", payload)
		if not (subscription or {}).get("id"):
			frappe.throw(_("Could not create the subscription in Asaas."))

		return subscription

	def get_subscription_invoice_url(self, subscription_id):
		"""A subscription carries no invoice of its own, so redirect the payer to
		the first charge it generated."""
		payments = self.get_request(f"/subscriptions/{subscription_id}/payments", params={"limit": 1})

		for payment in (payments or {}).get("data") or []:
			if url := payment.get("invoiceUrl") or payment.get("bankSlipUrl"):
				return url

	def get_cycle(self, details):
		"""Resolve the cycle to bill on, refusing what Asaas cannot charge.

		The `billing_period` vocabulary is wider than Asaas - Razorpay documents
		"Day", PayPal "SemiMonth" - so a period with no cycle behind it is an
		error. Falling back to the default would bill the payer monthly for a
		subscription they asked to be charged daily.
		"""
		cycle = details.get("cycle")

		if not cycle and (period := details.get("billing_period")):
			cycle = BILLING_PERIOD_TO_CYCLE.get(period)
			if not cycle:
				frappe.throw(_("Asaas cannot bill on a {0} period").format(period))

		if cycle and cycle not in BILLING_CYCLES:
			frappe.throw(_("{0} is not a billing cycle supported by Asaas").format(cycle))

		return cycle or self.subscription_cycle or "MONTHLY"

	def get_due_date(self, start_date=None):
		if start_date:
			return str(getdate(start_date))

		days = cint(self.days_until_due) if self.days_until_due is not None else 3
		return str(getdate(add_days(nowdate(), days)))

	def get_fine_and_interest(self):
		values = {}
		if flt(self.fine_percentage):
			values["fine"] = {"value": flt(self.fine_percentage), "type": "PERCENTAGE"}
		if flt(self.interest_percentage):
			values["interest"] = {"value": flt(self.interest_percentage)}

		return values

	# --- Webhook registration --------------------------------------------

	@frappe.whitelist()
	def register_webhook(self):
		"""Point the Asaas account's webhook at this site."""
		frappe.only_for("System Manager")

		url = get_webhook_url()
		payload = {
			"name": f"Frappe Payments ({frappe.local.site})",
			"url": url,
			"email": frappe.session.user,
			"enabled": True,
			"interrupted": False,
			"apiVersion": 3,
			"authToken": self.get_password("webhook_auth_token"),
			"sendType": "SEQUENTIALLY",
			"events": list(WEBHOOK_EVENTS),
		}

		webhook = self.post_request("/webhooks", payload)
		if not (webhook or {}).get("id"):
			frappe.throw(_("Could not register the webhook in Asaas."))

		return url


def get_description(data):
	# Asaas caps the description at 500 characters
	return (data.get("description") or data.get("title") or "")[:500]


def get_asaas_error_message():
	"""Read the `errors` array Asaas returns on a 4xx off the last response."""
	response = getattr(frappe.flags, "integration_request", None)
	try:
		errors = response.json().get("errors") or []
	except Exception:
		return

	return " ".join(error["description"] for error in errors if error.get("description"))


def get_webhook_url():
	return get_url("/api/method/payments.payment_gateways.doctype.asaas_settings.asaas_settings.webhook")


def get_success_url(data):
	"""Where Asaas sends the payer back to once the invoice is paid.

	Asaas rejects anything longer than 255 characters, so drop the optional
	parts before giving up on the redirect altogether.
	"""
	if not (data.get("reference_doctype") and data.get("reference_docname")):
		return

	params = {"doctype": data["reference_doctype"], "docname": data["reference_docname"]}
	if data.get("redirect_to"):
		params["redirect_to"] = data["redirect_to"]

	base = get_url("./payment-success")
	url = f"{base}?{urlencode(params)}"
	if len(url) > MAX_CALLBACK_URL_LENGTH:
		params.pop("redirect_to", None)
		url = f"{base}?{urlencode(params)}"

	if len(url) <= MAX_CALLBACK_URL_LENGTH:
		return url


def sanitize_cpf_cnpj(cpf_cnpj: str | None) -> str:
	"""Strip the punctuation Brazilians type and verify the check digits."""
	digits = re.sub(r"\D", "", cpf_cnpj or "")

	if not is_valid_cpf_cnpj(digits):
		frappe.throw(_("Please enter a valid CPF or CNPJ."))

	return digits


def is_valid_cpf_cnpj(digits: str) -> bool:
	if len(digits) == 11:
		weights = [list(range(10, 1, -1)), list(range(11, 1, -1))]
	elif len(digits) == 14:
		weights = [[5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]]
	else:
		return False

	if len(set(digits)) == 1:
		return False

	for weight in weights:
		body = digits[: len(weight)]
		remainder = sum(int(d) * w for d, w in zip(body, weight, strict=True)) % 11
		check_digit = 0 if remainder < 2 else 11 - remainder
		if int(digits[len(weight)]) != check_digit:
			return False

	return True


@contextmanager
def as_user(user: str):
	"""Run as the payer so that `on_payment_authorized` sees the same session
	the payment was started with - the webhook itself arrives as Guest."""
	original_user = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(original_user)


@frappe.whitelist(allow_guest=True)
def webhook():
	"""Receive payment events from Asaas.

	Deliveries are "at least once" and the queue is interrupted after repeated
	failures, so this answers 200 for anything it does not act on.
	"""
	settings = frappe.get_doc("Asaas Settings")
	validate_webhook_token(settings)

	payload = frappe.request.get_json(silent=True) or {}
	event = payload.get("event")
	payment = payload.get("payment") or {}

	if not (event and payment.get("id")):
		return {"status": "ignored"}

	integration_request = get_integration_request(payment)
	if not integration_request:
		return {"status": "ignored"}

	if event in PAID_EVENTS:
		return handle_payment_paid(integration_request, payment)

	if event in FAILED_EVENTS:
		return handle_payment_failed(integration_request, event, payment)

	return {"status": "ignored"}


def validate_webhook_token(settings):
	expected = settings.get_password("webhook_auth_token", raise_exception=False)
	received = frappe.get_request_header("asaas-access-token") or ""

	if not expected or not hmac.compare_digest(expected.encode(), received.encode()):
		frappe.throw(_("Asaas Webhook Token Verification Failed"), exc=frappe.PermissionError)


def get_integration_request(payment):
	"""Find the Integration Request a webhook belongs to.

	`externalReference` is set on every charge this app creates, but it is not
	copied onto the charges a subscription generates later, so fall back to the
	Asaas ids stored on the request.
	"""
	name = payment.get("externalReference")
	if name and frappe.db.exists(
		"Integration Request", {"name": name, "integration_request_service": "Asaas"}
	):
		return frappe.get_doc("Integration Request", name)

	for field, value in (
		("asaas_subscription_id", payment.get("subscription")),
		("asaas_payment_id", payment.get("id")),
	):
		if not value:
			continue

		requests = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "Asaas",
				"data": ["like", f'%"{field}": "{value}"%'],
			},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)
		if requests:
			return frappe.get_doc("Integration Request", requests[0])


def handle_payment_paid(integration_request, payment):
	if integration_request.status == "Completed":
		# Either a redelivery of an event already acted on, or a renewal of a
		# subscription whose first charge completed the request.
		if payment.get("subscription"):
			notify_subscription_renewal(integration_request, payment)

		return {"status": "already handled"}

	data = frappe._dict(json.loads(integration_request.data))
	integration_request.update_status({"asaas_payment_id": payment.get("id")}, "Completed")

	if data.reference_doctype and data.reference_docname:
		try:
			with as_user(integration_request.owner):
				frappe.get_doc(data.reference_doctype, data.reference_docname).run_method(
					"on_payment_authorized", "Completed"
				)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Asaas on_payment_authorized Failed")

	return {"status": "Completed"}


def handle_payment_failed(integration_request, event, payment):
	if integration_request.status in ("Completed", "Failed"):
		return {"status": "already handled"}

	integration_request.db_set("error", f"{event}: {frappe.as_json(payment)}", update_modified=False)
	integration_request.update_status({"asaas_payment_id": payment.get("id")}, "Failed")

	return {"status": "Failed"}


def notify_subscription_renewal(integration_request, payment):
	"""Log the renewal and let the consuming app extend the subscription."""
	renewal = create_request_log(
		{
			"payment_gateway": "Asaas",
			"asaas_subscription_id": payment.get("subscription"),
			"asaas_payment_id": payment.get("id"),
			"original_request": integration_request.name,
		},
		service_name="Asaas",
		integration_type="Subscription Notification",
		reference_doctype=integration_request.reference_doctype,
		reference_docname=integration_request.reference_docname,
		is_remote_request=1,
		status="Completed",
	)

	call_hook_method("handle_subscription_notification", doctype="Integration Request", docname=renewal.name)
