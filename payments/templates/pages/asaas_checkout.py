# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.utils import flt, fmt_money

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
	"currency",
)


def get_context(context):
	context.no_cache = 1

	try:
		validate_integration_request(frappe.form_dict["token"])

		doc = frappe.get_doc("Integration Request", frappe.form_dict["token"])
		payment_details = json.loads(doc.data)

		for key in expected_keys:
			context[key] = payment_details.get(key)

		context["token"] = frappe.form_dict["token"]
		context["amount"] = flt(context["amount"])
		context["formatted_amount"] = fmt_money(context["amount"], currency=context["currency"])
		context["is_subscription"] = bool(payment_details.get("subscription_details"))

	except Exception:
		frappe.redirect_to_message(
			_("Invalid Token"),
			_("Seems token you are using is invalid!"),
			http_status_code=400,
			indicator_color="red",
		)

		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


@frappe.whitelist(allow_guest=True)
def make_payment(token, cpf_cnpj, payer_name=None, payer_phone=None):
	validate_integration_request(token)

	data = frappe.get_doc("Asaas Settings").create_request(
		{
			"token": token,
			"cpf_cnpj": cpf_cnpj,
			"payer_name": payer_name,
			"payer_phone": payer_phone,
		}
	)
	frappe.db.commit()
	return data
