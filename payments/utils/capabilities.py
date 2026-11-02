"""What each gateway can do, for apps choosing which to offer (decision 002 §4).

`card_entry` says where the payer types card details:
- `redirect`: on the gateway's own page,
- `frame`: in the gateway's frame on our bare payment page,
- `none`: the gateway takes no cards (bank debit, mobile money).

`recurring` is true when this app creates the gateway's subscriptions.
"""

import frappe

CAPABILITIES = {
	"Asaas Settings": {"card_entry": "redirect", "recurring": True},
	"Braintree Settings": {"card_entry": "frame", "recurring": False},
	"GoCardless Settings": {"card_entry": "none", "recurring": False},
	"MoMo Settings": {"card_entry": "none", "recurring": False},
	"Mpesa Settings": {"card_entry": "none", "recurring": False},
	"Paymob Settings": {"card_entry": "redirect", "recurring": False},
	"PayPal Settings": {"card_entry": "redirect", "recurring": True},
	"Paytm Settings": {"card_entry": "redirect", "recurring": False},
	"Razorpay Settings": {"card_entry": "frame", "recurring": False},
	"Stripe Settings": {"card_entry": "frame", "recurring": True},
}


def get(payment_gateway: str) -> frappe._dict:
	"""The capabilities of a Payment Gateway record, or empty when it is not one of ours."""
	settings = frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_settings")
	return frappe._dict(CAPABILITIES.get(settings) or {})
