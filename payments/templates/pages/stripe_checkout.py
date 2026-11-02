# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, fmt_money

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
	to_minor_units,
)
from payments.utils import payment_page

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
	"payment_gateway",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]
		gateway_controller = get_gateway_controller(
			context.reference_doctype, context.reference_docname, context.payment_gateway
		)
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.logo = get_header_image(context.reference_docname, gateway_controller)
		subscription = is_a_subscription(context.reference_doctype, context.reference_docname)
		payment_page.prepare(context, "stripe")
		context.payment_boot = {
			"csrf_token": context.csrf_token,
			"publishable_key": context.publishable_key,
			"mode": "subscription" if subscription else "payment",
			"amount": to_minor_units(context["amount"], context["currency"]),
			"currency": context["currency"].lower(),
			"data": dict(frappe.form_dict),
			"text": {
				"processing": _("Processing..."),
				"success": _("Your payment has been successfully registered."),
				"error": _("An error occured during the payment process. Please contact us."),
			},
		}

		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])

		if subscription:
			payment_plan = frappe.db.get_value(
				context.reference_doctype, context.reference_docname, "payment_plan"
			)
			recurrence = frappe.db.get_value("Payment Plan", payment_plan, "recurrence")

			context["amount"] = context["amount"] + " " + _(recurrence)

	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key(doc, gateway_controller):
	publishable_key = frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		publishable_key = frappe.conf.sandbox_publishable_key

	return publishable_key


def get_header_image(doc, gateway_controller):
	return frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")


@frappe.whitelist(allow_guest=True)
def make_payment(
	payment_method_id, data, reference_doctype=None, reference_docname=None, payment_gateway=None
):
	if isinstance(data, str):
		data = json.loads(data)

	data.update({"payment_method_id": payment_method_id})

	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)

	if is_a_subscription(reference_doctype, reference_docname):
		reference = frappe.get_doc(reference_doctype, reference_docname)
		data = reference.create_subscription("stripe", gateway_controller, data)
	else:
		data = frappe.get_doc("Stripe Settings", gateway_controller).create_request(data)

	frappe.db.commit()
	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")


@frappe.whitelist(allow_guest=True)
def confirm_payment(integration_request):
	"""After the payer completes 3-D Secure, settle what make_payment started."""
	request = frappe.get_doc("Integration Request", integration_request)
	if request.integration_request_service != "Stripe":
		frappe.throw(_("Not a Stripe payment."))
	data = json.loads(request.data)
	gateway_controller = get_gateway_controller(
		data.get("reference_doctype"), data.get("reference_docname"), data.get("payment_gateway")
	)
	result = frappe.get_doc("Stripe Settings", gateway_controller).confirm_request(request)
	frappe.db.commit()
	return result
