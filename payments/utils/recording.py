"""Hand a gateway-confirmed payment to the document it pays for.

Every gateway ends the same way: the money is confirmed, and the reference
document (a Payment Request, an order) is told through `on_payment_authorized`,
which is where the payment actually gets recorded. When that call raises, the
payer has still paid, so the failure is kept as an Unrecorded Payment and
retried instead of left in the Error Log. See docs/decisions/001.
"""

import json

import frappe

SAVEPOINT = "record_payment"


def record_payment(
	integration_request,
	reference_doctype: str | None,
	reference_docname: str | None,
	status: str = "Completed",
	run_as: str | None = None,
):
	"""Call `on_payment_authorized` on the reference and return what it returns.

	`integration_request` is the Integration Request (document or name) the
	payment came through. `run_as` is the user the call runs as, the current
	one by default; retries use the same user. If the call raises, whatever it
	wrote is rolled back and an Unrecorded Payment is created, and this returns
	None so that the gateway can answer the payer or the webhook as usual.
	"""
	if not (reference_doctype and reference_docname):
		return

	user = run_as or frappe.session.user
	try:
		return call_reference(reference_doctype, reference_docname, status, user)
	except Exception:
		error = frappe.get_traceback()

	gateway, integration_request_name = get_gateway(integration_request)
	frappe.log_error(error, f"{gateway or 'Payment'}: payment not recorded")

	data = frappe.flags.get("data")
	frappe.get_doc(
		{
			"doctype": "Unrecorded Payment",
			"status": "Retrying",
			"gateway": gateway,
			"integration_request": integration_request_name,
			"reference_doctype": reference_doctype,
			"reference_docname": reference_docname,
			"payment_status": status,
			"run_as": user,
			"error": error,
			"flags_data": json.dumps(data, default=str) if data else None,
		}
	).insert(ignore_permissions=True)


def call_reference(reference_doctype: str, reference_docname: str, status: str, user: str):
	"""Run `on_payment_authorized` as `user`, undoing its writes if it raises."""
	original_user = frappe.session.user
	frappe.db.savepoint(SAVEPOINT)
	try:
		if user != original_user:
			frappe.set_user(user)
		return frappe.get_doc(reference_doctype, reference_docname).run_method(
			"on_payment_authorized", status
		)
	except Exception:
		try:
			frappe.db.rollback(save_point=SAVEPOINT)
		except Exception:
			# the reference committed part way, so the savepoint is gone
			frappe.log_error(title="Payment recording could not be rolled back")
		raise
	finally:
		if frappe.session.user != original_user:
			frappe.set_user(original_user)


def get_gateway(integration_request) -> tuple[str | None, str | None]:
	if not integration_request:
		return None, None

	if isinstance(integration_request, str):
		gateway = frappe.db.get_value(
			"Integration Request", integration_request, "integration_request_service", as_dict=True
		)
		if not gateway:
			return None, None
		return gateway.integration_request_service, integration_request

	return integration_request.integration_request_service, integration_request.name
