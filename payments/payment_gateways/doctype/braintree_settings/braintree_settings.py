# Copyright (c) 2018, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from urllib.parse import urlencode

import braintree
import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url, getdate

from payments.utils import create_payment_gateway
from payments.utils.recording import record_payment
from payments.utils.settlement import record_settlement


class BraintreeSettings(Document):
	supported_currencies = (
		"AED",
		"AMD",
		"AOA",
		"ARS",
		"AUD",
		"AWG",
		"AZN",
		"BAM",
		"BBD",
		"BDT",
		"BGN",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BWP",
		"BYN",
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
		"GEL",
		"GHS",
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
		"KGS",
		"KHR",
		"KMF",
		"KRW",
		"KYD",
		"KZT",
		"LAK",
		"LBP",
		"LKR",
		"LRD",
		"LSL",
		"LTL",
		"MAD",
		"MDL",
		"MKD",
		"MNT",
		"MOP",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"MZN",
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
		"RON",
		"RSD",
		"RUB",
		"RWF",
		"SAR",
		"SBD",
		"SCR",
		"SEK",
		"SGD",
		"SHP",
		"SLL",
		"SOS",
		"SRD",
		"STD",
		"SVC",
		"SYP",
		"SZL",
		"THB",
		"TJS",
		"TOP",
		"TRY",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VEF",
		"VND",
		"VUV",
		"WST",
		"XAF",
		"XCD",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
		"ZMK",
		"ZWD",
	)

	def validate(self):
		if not self.flags.ignore_mandatory:
			self.configure_braintree()

	def on_update(self):
		create_payment_gateway(
			"Braintree-" + self.gateway_name,
			settings="Braintree Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Braintree-" + self.gateway_name)

	def get_credentials(self):
		return {
			"environment": "sandbox" if self.use_sandbox else "production",
			"merchant_id": self.merchant_id,
			"public_key": self.public_key,
			"private_key": self.get_password(fieldname="private_key", raise_exception=False),
		}

	def configure_braintree(self):
		braintree.Configuration.configure(**self.get_credentials())

	def get_braintree_gateway(self):
		"""A gateway bound to this account alone, unlike the global configuration."""
		return braintree.BraintreeGateway(braintree.Configuration(**self.get_credentials()))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Stripe does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs):
		return get_url(f"./braintree_checkout?{urlencode(kwargs)}")

	def create_payment_request(self, data):
		self.data = frappe._dict(data)

		try:
			self.integration_request = create_request_log(self.data, service_name="Braintree")
			return self.create_charge_on_braintree()

		except Exception:
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"There seems to be an issue with the server's braintree configuration. Don't worry, in case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def create_charge_on_braintree(self):
		self.configure_braintree()

		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None

		result = braintree.Transaction.sale(
			{
				"amount": self.data.amount,
				"payment_method_nonce": self.data.payload_nonce,
				"options": {"submit_for_settlement": True},
			}
		)

		if result.is_success:
			self.integration_request.db_set("status", "Completed", update_modified=False)
			self.flags.status_changed_to = "Completed"
			self.integration_request.db_set("output", result.transaction.status, update_modified=False)
			record_braintree_sale(self.integration_request, result.transaction)

		elif result.transaction:
			self.integration_request.db_set("status", "Failed", update_modified=False)
			error_log = frappe.log_error(
				"code: "
				+ str(result.transaction.processor_response_code)
				+ " | text: "
				+ str(result.transaction.processor_response_text),
				"Braintree Payment Error",
			)
			self.integration_request.db_set("error", error_log.error, update_modified=False)
		else:
			self.integration_request.db_set("status", "Failed", update_modified=False)
			for error in result.errors.deep_errors:
				error_log = frappe.log_error(
					"code: " + str(error.code) + " | message: " + str(error.message),
					"Braintree Payment Error",
				)
				self.integration_request.db_set("error", error_log.error, update_modified=False)

		if self.flags.status_changed_to == "Completed":
			status = "Completed"
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				custom_redirect_to = record_payment(
					self.integration_request,
					self.data.reference_doctype,
					self.data.reference_docname,
					self.flags.status_changed_to,
				)
				try:
					braintree_success_page = frappe.get_hooks("braintree_success_page")
					if braintree_success_page:
						custom_redirect_to = frappe.get_attr(braintree_success_page[-1])(self.data)
				except Exception:
					frappe.log_error(frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

			redirect_url = "payment-success"
		else:
			status = "Error"
			redirect_url = "payment-failed"

		get_parameters = [("doctype", self.data.reference_doctype), ("docname", self.data.reference_docname)]
		if redirect_to:
			get_parameters.append(("redirect_to", redirect_to))
		if redirect_message:
			get_parameters.append(("redirect_message", redirect_message))

		redirect_url += "?" + urlencode(get_parameters)
		return {"redirect_to": redirect_url, "status": status}


def get_gateway_controller(doc):
	payment_request = frappe.get_doc("Payment Request", doc)
	return frappe.db.get_value("Payment Gateway", payment_request.payment_gateway, "gateway_controller")


def get_client_token(doc):
	gateway_controller = get_gateway_controller(doc)
	settings = frappe.get_doc("Braintree Settings", gateway_controller)
	settings.configure_braintree()

	return braintree.ClientToken.generate()


def record_braintree_sale(integration_request, transaction):
	"""Keep the transaction id and amount for payout reconciliation.

	Braintree does not report a fee per transaction on most pricing plans, so
	fee and net stay unknown; the credit date arrives with the
	`transaction_disbursed` webhook.
	"""
	data = json.loads(integration_request.data or "{}")
	data["braintree_transaction_id"] = transaction.id
	integration_request.db_set("data", frappe.as_json(data), update_modified=False)

	record_settlement(integration_request, processor_id=transaction.id, gross=transaction.amount)


# Braintree calls this unauthenticated; it verifies the signature with the
# account's keys before it reads or writes anything.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def webhook(gateway: str | None = None):
	"""Receive webhook notifications from Braintree.

	There is one Braintree Settings record per merchant account, so each
	account registers its own URL in the Braintree Control Panel, naming its
	record in the query string:

		/api/method/payments.payment_gateways.doctype.braintree_settings.braintree_settings.webhook?gateway=<Payment Gateway Name>

	where the name is the record's Payment Gateway Name (its Payment Gateway,
	"Braintree-<name>", is accepted too).

	`disbursement` notifications are handed to the `payment_gateway_disbursement`
	hook, called with `gateway`, `payout_id`, `date`, `amount` and
	`transaction_ids`, after the date is recorded as the credit date of every
	transaction it pays out. `transaction_disbursed` records the credit date of
	its one transaction. Anything else is answered and dropped.
	"""
	if gateway and gateway.startswith("Braintree-") and not frappe.db.exists("Braintree Settings", gateway):
		gateway = gateway.removeprefix("Braintree-")
	if not gateway or not frappe.db.exists("Braintree Settings", gateway):
		frappe.throw(_("Unknown Braintree account"), exc=frappe.PermissionError)

	settings = frappe.get_doc("Braintree Settings", gateway)
	form = frappe.local.form_dict
	try:
		notification = settings.get_braintree_gateway().webhook_notification.parse(
			form.get("bt_signature"), form.get("bt_payload")
		)
	except braintree.exceptions.InvalidSignatureError:
		frappe.throw(_("Braintree Webhook Signature Verification Failed"), exc=frappe.PermissionError)

	if notification.kind == braintree.WebhookNotification.Kind.Disbursement:
		disbursement = notification.disbursement
		date = iso_date(disbursement.disbursement_date)
		transaction_ids = list(disbursement.transaction_ids or [])
		for transaction_id in transaction_ids:
			record_disbursed(transaction_id, date)

		call_hook_method(
			"payment_gateway_disbursement",
			gateway="Braintree-" + settings.gateway_name,
			payout_id=disbursement.id,
			date=date,
			amount=float(disbursement.amount),
			transaction_ids=transaction_ids,
		)
		return {"status": "processed"}

	if notification.kind == braintree.WebhookNotification.Kind.TransactionDisbursed:
		transaction = notification.transaction
		details = getattr(transaction, "disbursement_details", None)
		if record_disbursed(transaction.id, iso_date(getattr(details, "disbursement_date", None))):
			return {"status": "processed"}

	return {"status": "ignored"}


def record_disbursed(transaction_id, date):
	"""Record when a transaction was paid out; False if this site never made it."""
	integration_request = get_integration_request(transaction_id)
	if not integration_request:
		return False

	record_settlement(integration_request, processor_id=transaction_id, credit_date=date)
	return True


def get_integration_request(transaction_id):
	requests = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": "Braintree",
			"data": ["like", f'%"braintree_transaction_id": "{transaction_id}"%'],
		},
		pluck="name",
		limit=1,
	)
	if requests:
		return frappe.get_doc("Integration Request", requests[0])


def iso_date(value):
	if not value:
		return None
	return str(getdate(value))
