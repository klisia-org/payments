import frappe, json, secrets
from frappe import _

no_cache = 1


def get_context(context):
    """
    Two flows:
      • Payment Request flow — URL has ?payment_request_name=… (or ?order_id=…).
        We render the page directly from the PR doc; no token/cache needed.
      • Webshop flow — URL has ?token=… seeded by webshop checkout.
        We read cart_data from cache as before.
    """
    fd = frappe.form_dict
    # Accept any of the conventional keys used by callers (button, email link, etc.)
    pr_name = (
        fd.get("payment_request_name")
        or fd.get("order_id")
        or (fd.get("reference_docname") if fd.get("reference_doctype") == "Payment Request" else None)
    )
    token = fd.get("token")

    if pr_name:
        pr = frappe.get_doc("Payment Request", pr_name)
        gateway_full = pr.payment_gateway or ""
        gateway_short = (
            gateway_full[len("MoMo-"):] if gateway_full.startswith("MoMo-") else gateway_full
        )
        # Resilient title: don't blow up if the reference doc was unlinked.
        try:
            title = fd.get("title") or f"Payment for {pr.reference_name}"
        except Exception:
            title = fd.get("title") or f"Payment {pr.name}"

        context.update({
            "is_payment_request": True,
            "payment_request_name": pr.name,
            "amount": pr.grand_total,
            "currency": pr.currency,
            "gateway_name": gateway_short,
            "title": title,
            "customer_name": pr.party_name or "",
            # token is unused in the PR flow but kept in template for consistency.
            "token": "",
            "no_cache": 1,
        })
        return

    # Webshop flow — needs a valid cache token.
    if not token:
        frappe.throw(_("Invalid or missing payment token"), frappe.PermissionError)

    raw = frappe.cache().get_value(f"momo_pending_{token}")
    if not raw:
        frappe.throw(_("Payment session expired. Please go back and try again."))

    cart_data = json.loads(raw)
    context.update({
        "is_payment_request": False,
        "payment_request_name": "",
        "token": token,
        "amount": fd.get("amount") or cart_data["grand_total"],
        "currency": cart_data["currency"],
        "gateway_name": fd.get("gateway_name"),
        "title": fd.get("title") or "Complete Your Payment",
        "customer_name": cart_data.get("customer_name", ""),
        "no_cache": 1,
    })
