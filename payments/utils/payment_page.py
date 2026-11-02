"""The bare page that holds a gateway's card frame (decision 002 §1).

`templates/payment_page.html` loads no Website Script, head HTML or `web_include_js`, so nothing
an admin adds to the website runs beside the card frame. Its Content-Security-Policy lets scripts,
frames and connections come only from our origin and the gateway's domains.
"""

import re

import frappe

# Each gateway's domains, from its own CSP guidance. A change here is a change to the policy.
GATEWAY_SOURCES = {
	"stripe": {
		"script-src": ["https://js.stripe.com", "https://*.js.stripe.com"],
		"frame-src": ["https://js.stripe.com", "https://*.js.stripe.com", "https://hooks.stripe.com"],
		"connect-src": ["https://api.stripe.com"],
	},
	"braintree": {
		"script-src": [
			"https://js.braintreegateway.com",
			"https://assets.braintreegateway.com",
			"https://www.paypalobjects.com",
			"https://*.paypal.com",
		],
		"frame-src": ["https://assets.braintreegateway.com", "https://*.paypal.com"],
		"connect-src": [
			"https://api.braintreegateway.com",
			"https://api.sandbox.braintreegateway.com",
			"https://client-analytics.braintreegateway.com",
			"https://client-analytics.sandbox.braintreegateway.com",
			"https://*.braintree-api.com",
			"https://*.paypal.com",
		],
	},
	"razorpay": {
		"script-src": ["https://checkout.razorpay.com"],
		"frame-src": ["https://api.razorpay.com", "https://checkout.razorpay.com"],
		"connect-src": ["https://api.razorpay.com", "https://lumberjack.razorpay.com"],
	},
}

# Script versions live here so an update is one deliberate change (decision 002 §3).
SCRIPTS = {
	"stripe": "https://js.stripe.com/v3/",
	"braintree": "https://js.braintreegateway.com/web/dropin/1.48.0/js/dropin.min.js",
	"razorpay": "https://checkout.razorpay.com/v1/checkout.js",
}

THEME_COLOURS = ("primary_color", "text_color", "background_color")
HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def policy(gateway: str) -> str:
	sources = GATEWAY_SOURCES[gateway]
	directives = {
		"default-src": ["'self'"],
		"script-src": ["'self'", *sources["script-src"]],
		"frame-src": sources["frame-src"],
		"connect-src": ["'self'", *sources["connect-src"]],
		"img-src": ["'self'", "data:", "https:"],
		"style-src": ["'self'", "'unsafe-inline'"],
		"font-src": ["'self'", "data:", "https:"],
		"base-uri": ["'self'"],
		"form-action": ["'self'"],
		"object-src": ["'none'"],
	}
	return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


def prepare(context, gateway: str):
	"""Set the policy header and what the bare page reads from `context`."""
	frappe.local.response_headers["Content-Security-Policy"] = policy(gateway)
	context.gateway_script = SCRIPTS[gateway]
	context.page_script = f"/assets/payments/js/{gateway}_checkout.js"
	context.brand_css = brand_css()
	context.csrf_token = frappe.sessions.get_csrf_token()


def brand_css() -> str:
	"""The website theme's colours as CSS variables; only hex values pass."""
	theme = frappe.db.get_single_value("Website Settings", "website_theme")
	if not theme or not frappe.db.exists("Website Theme", theme):
		return ""
	values = frappe.db.get_value("Website Theme", theme, THEME_COLOURS, as_dict=True) or {}
	declarations = [
		f"--payment-{field.split('_')[0]}: {value};"
		for field, value in values.items()
		if value and HEX.match(value)
	]
	return ":root{" + "".join(declarations) + "}" if declarations else ""
