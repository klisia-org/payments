import frappe, json, secrets
from frappe import _

no_cache = 1


def get_context(context):
    fd = frappe.form_dict
    token = fd.get("token")
    pr_name = fd.get("payment_request_name") or fd.get("order_id")

    # Bridge: Payment Request flow → mint a token + seed cache, then render.
    if not token and pr_name:
        pr = frappe.get_doc("Payment Request", pr_name)
        gateway_full = pr.payment_gateway or ""
        gateway_short = (
            gateway_full[len("MoMo-"):] if gateway_full.startswith("MoMo-") else gateway_full
        )
        token = secrets.token_urlsafe(24)
        frappe.cache().set_value(
            f"momo_pending_{token}",
            json.dumps({
                "grand_total": pr.grand_total,
                "currency": pr.currency,
                "customer_name": pr.party_name or "",
                "reference_doctype": "Payment Request",
                "reference_docname": pr_name,
                "payment_request_name": pr_name,
                "gateway": gateway_full,
            }),
            expires_in_sec=3600,
        )
        fd["token"] = token
        fd["gateway_name"] = gateway_short
        fd.setdefault("title", f"Payment for {pr.reference_name}")

    token = fd.get("token")
    if not token:
        frappe.throw(_("Invalid or missing payment token"), frappe.PermissionError)

    raw = frappe.cache().get_value(f"momo_pending_{token}")
    if not raw:
        frappe.throw(_("Payment session expired. Please go back and try again."))

    cart_data = json.loads(raw)
    context.update({
        "token": token,
        "amount": fd.get("amount") or cart_data["grand_total"],
        "currency": cart_data["currency"],
        "gateway_name": fd.get("gateway_name"),
        "title": fd.get("title") or "Complete Your Payment",
        "customer_name": cart_data.get("customer_name", ""),
        "no_cache": 1,
    })