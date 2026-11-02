# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
import stripe
from frappe import _
from frappe.integrations.utils import create_request_log


def create_stripe_subscription(gateway_controller, data):
	stripe_settings = frappe.get_doc("Stripe Settings", gateway_controller)
	stripe_settings.data = frappe._dict(data)

	stripe.api_key = stripe_settings.get_password(fieldname="secret_key", raise_exception=False)
	stripe.default_http_client = stripe.http_client.RequestsClient()

	try:
		stripe_settings.integration_request = create_request_log(stripe_settings.data, "Host", "Stripe")
		stripe_settings.payment_plans = frappe.get_doc(
			"Payment Request", stripe_settings.data.reference_docname
		).subscription_plans
		return create_subscription_on_stripe(stripe_settings)

	except Exception:
		stripe_settings.log_error("Unable to create Stripe subscription")
		return {
			"redirect_to": frappe.redirect_to_message(
				_("Server Error"),
				_(
					"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
				),
			),
			"status": 401,
		}


def create_subscription_on_stripe(stripe_settings):
	items = []
	for payment_plan in stripe_settings.payment_plans:
		plan = frappe.db.get_value("Subscription Plan", payment_plan.plan, "product_price_id")
		items.append({"price": plan, "quantity": payment_plan.qty})

	try:
		payment_method = stripe_settings.data.payment_method_id
		customer = stripe.Customer.create(
			payment_method=payment_method,
			invoice_settings={"default_payment_method": payment_method},
			description=stripe_settings.data.payer_name,
			email=stripe_settings.data.payer_email,
		)
		subscription = stripe.Subscription.create(
			customer=customer.id,
			items=items,
			expand=["latest_invoice.payment_intent"],
			metadata={"integration_request": stripe_settings.integration_request.name},
		)
		stripe_settings.integration_request.db_set("request_id", subscription.id, update_modified=False)
		return settle_subscription(stripe_settings, subscription)
	except Exception:
		stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
		stripe_settings.log_error("Unable to create Stripe subscription")

	return stripe_settings.finalize_request()


def settle_subscription(stripe_settings, subscription):
	"""Active, or waiting on 3-D Secure for its first invoice, or failed."""
	invoice = subscription.latest_invoice
	intent = invoice.get("payment_intent") if invoice else None
	if subscription.status == "active":
		stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
		stripe_settings.flags.status_changed_to = "Completed"
	elif subscription.status == "incomplete" and intent and intent.status == "requires_action":
		return stripe_settings.needs_action(intent.client_secret)
	else:
		stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
		frappe.log_error(f"Stripe Subscription ID {subscription.id}: Payment failed")
	return stripe_settings.finalize_request()
