"""What the processor reports about a paid charge, for reconciling its payouts.

A processor pays out many charges in one bank deposit, net of its fees. To
match the deposit to the charges, an app needs per charge the processor's own
id, the gross, the fee, the net and when the money is credited. Gateways keep
that under the `settlement` key of the charge's Integration Request data:

	{
		"processor_id": "pay_123",
		"gross": 100.0,
		"fee": 2.99,  # None when the processor does not report it
		"net": 97.01,  # None when the processor does not report it
		"credit_date": "2026-11-20",  # None until the money is credited
		"estimated_credit_date": "2026-11-19",  # None when not announced
	}
"""

import json

import frappe
from frappe.utils import flt, getdate

SETTLEMENT_KEYS = ("processor_id", "gross", "fee", "net", "credit_date", "estimated_credit_date")
AMOUNT_KEYS = ("gross", "fee", "net")
DATE_KEYS = ("credit_date", "estimated_credit_date")


def record_settlement(integration_request, **values) -> dict:
	"""Merge `values` into the settlement of `integration_request` (document or name).

	Processors report a charge in several events - approved first, credited
	later - so a value left out or passed as None keeps what an earlier event
	recorded. Returns the settlement as stored.
	"""
	unknown = set(values) - set(SETTLEMENT_KEYS)
	if unknown:
		raise TypeError(f"unknown settlement values: {', '.join(sorted(unknown))}")

	if isinstance(integration_request, str):
		integration_request = frappe.get_doc("Integration Request", integration_request)

	data = json.loads(integration_request.data or "{}")
	settlement = dict.fromkeys(SETTLEMENT_KEYS)
	settlement.update(data.get("settlement") or {})

	for key, value in values.items():
		if value is None or value == "":
			continue
		if key in AMOUNT_KEYS:
			value = flt(value)
		elif key in DATE_KEYS:
			value = str(getdate(value))
		else:
			value = str(value)
		settlement[key] = value

	data["settlement"] = settlement
	integration_request.db_set("data", frappe.as_json(data), update_modified=False)

	return settlement


def get_settlement(integration_request_name: str) -> dict | None:
	"""The settlement recorded on an Integration Request, or None."""
	data = frappe.db.get_value("Integration Request", integration_request_name, "data")
	if not data:
		return

	return json.loads(data).get("settlement")
