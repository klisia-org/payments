# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.payment_gateways.doctype.asaas_settings.asaas_settings import (
	MAX_CALLBACK_URL_LENGTH,
	get_success_url,
	is_valid_cpf_cnpj,
	sanitize_cpf_cnpj,
)


class TestAsaasSettings(FrappeTestCase):
	def test_accepts_well_formed_cpf_and_cnpj(self):
		self.assertTrue(is_valid_cpf_cnpj("11144477735"))
		self.assertTrue(is_valid_cpf_cnpj("11222333000181"))

	def test_rejects_malformed_cpf_and_cnpj(self):
		for value in (
			"11144477734",  # wrong check digit
			"11222333000180",  # wrong check digit
			"1114447773",  # too short
			"11111111111",  # repeated digits pass the check digits but are not issued
			"",
		):
			self.assertFalse(is_valid_cpf_cnpj(value), msg=value)

	def test_sanitize_strips_punctuation(self):
		self.assertEqual(sanitize_cpf_cnpj("111.444.777-35"), "11144477735")
		self.assertEqual(sanitize_cpf_cnpj("11.222.333/0001-81"), "11222333000181")

	def test_sanitize_rejects_invalid_document(self):
		self.assertRaises(frappe.ValidationError, sanitize_cpf_cnpj, "111.444.777-34")
		self.assertRaises(frappe.ValidationError, sanitize_cpf_cnpj, None)

	def test_success_url_keeps_the_reference(self):
		url = get_success_url({"reference_doctype": "Payment Request", "reference_docname": "PR0001"})
		self.assertIn("doctype=Payment+Request", url)
		self.assertIn("docname=PR0001", url)

	def test_success_url_drops_redirect_to_when_too_long(self):
		url = get_success_url(
			{
				"reference_doctype": "Payment Request",
				"reference_docname": "PR0001",
				"redirect_to": "/" + "x" * MAX_CALLBACK_URL_LENGTH,
			}
		)
		self.assertLessEqual(len(url), MAX_CALLBACK_URL_LENGTH)
		self.assertNotIn("redirect_to", url)

	def test_no_success_url_without_a_reference(self):
		self.assertIsNone(get_success_url({"redirect_to": "/courses"}))

	def test_cycle_falls_back_to_the_configured_default(self):
		settings = frappe.get_doc({"doctype": "Asaas Settings", "subscription_cycle": "YEARLY"})

		self.assertEqual(settings.get_cycle({}), "YEARLY")
		self.assertEqual(settings.get_cycle({"billing_period": "Month"}), "MONTHLY")
		self.assertEqual(settings.get_cycle({"cycle": "BIWEEKLY"}), "BIWEEKLY")

	def test_cycle_rejects_a_period_asaas_does_not_bill_on(self):
		settings = frappe.get_doc({"doctype": "Asaas Settings"})

		self.assertRaises(frappe.ValidationError, settings.get_cycle, {"cycle": "DAILY"})
