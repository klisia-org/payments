# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from payments.utils import payment_page
from payments.utils.utils import validate_integration_request

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"order_id",
	"currency",
)


def get_context(context):
	context.no_cache = 1
	context.api_key = get_api_key()

	try:
		validate_integration_request(frappe.form_dict["token"])

		doc = frappe.get_doc("Integration Request", frappe.form_dict["token"])

		payment_details = json.loads(doc.data)

		for key in expected_keys:
			context[key] = payment_details[key]

		context["token"] = frappe.form_dict["token"]
		context["amount"] = flt(context["amount"])
		context["subscription_id"] = (
			payment_details["subscription_id"] if payment_details.get("subscription_id") else ""
		)
		payment_page.prepare(context, "razorpay")
		context.payment_boot = {
			"csrf_token": context.csrf_token,
			"options": {
				"key": context.api_key,
				"amount": cint(flt(context.amount) * 100),
				"currency": context.currency,
				"name": context.title,
				"description": context.description,
				"subscription_id": context.subscription_id,
				"order_id": context.order_id,
				"prefill": {"name": context.payer_name, "email": context.payer_email},
				"notes": dict(frappe.form_dict),
			},
			"reference_doctype": context.reference_doctype,
			"reference_docname": context.reference_docname,
			"token": context.token,
			"text": {
				"confirming": _("Confirming Payment"),
				"success": _("Your payment has been successfully registered."),
				"error": _("An error occured during the payment process. Please contact us."),
			},
		}

	except Exception:
		frappe.redirect_to_message(
			_("Invalid Token"),
			_("Seems token you are using is invalid!"),
			http_status_code=400,
			indicator_color="red",
		)

		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key():
	api_key = frappe.db.get_single_value("Razorpay Settings", "api_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		api_key = frappe.conf.sandbox_api_key

	return api_key


@frappe.whitelist(allow_guest=True)
def make_payment(razorpay_payment_id, options, reference_doctype, reference_docname, token):
	data = {}

	if isinstance(options, str):
		data = json.loads(options)

	data.update(
		{
			"razorpay_payment_id": razorpay_payment_id,
			"reference_docname": reference_docname,
			"reference_doctype": reference_doctype,
			"token": token,
		}
	)

	data = frappe.get_doc("Razorpay Settings").create_request(data)
	frappe.db.commit()
	return data
