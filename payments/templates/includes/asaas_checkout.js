$(document).ready(function () {
	var form = document.querySelector("#payment-form");
	var button = document.querySelector("#submit-button");
	var cpfCnpjInput = document.querySelector("#cpf-cnpj");
	var cpfCnpjError = document.querySelector("#cpf-cnpj-error");
	var token = "{{ token }}";

	function digitsOf(value) {
		return (value || "").replace(/\D/g, "");
	}

	// 000.000.000-00 for a CPF, 00.000.000/0000-00 for a CNPJ
	function formatCpfCnpj(digits) {
		if (digits.length <= 11) {
			return digits
				.replace(/^(\d{3})(\d)/, "$1.$2")
				.replace(/^(\d{3})\.(\d{3})(\d)/, "$1.$2.$3")
				.replace(/\.(\d{3})(\d{1,2})$/, ".$1-$2");
		}
		return digits
			.slice(0, 14)
			.replace(/^(\d{2})(\d)/, "$1.$2")
			.replace(/^(\d{2})\.(\d{3})(\d)/, "$1.$2.$3")
			.replace(/\.(\d{3})(\d)/, ".$1/$2")
			.replace(/(\d{4})(\d{1,2})$/, "$1-$2");
	}

	// Same check digit rules the server applies, so the payer is told about a
	// typo before we call Asaas.
	function isValidCpfCnpj(digits) {
		var weightSets;
		if (digits.length === 11) {
			weightSets = [[10, 9, 8, 7, 6, 5, 4, 3, 2], [11, 10, 9, 8, 7, 6, 5, 4, 3, 2]];
		} else if (digits.length === 14) {
			weightSets = [
				[5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2],
				[6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2],
			];
		} else {
			return false;
		}

		if (/^(\d)\1+$/.test(digits)) {
			return false;
		}

		return weightSets.every(function (weights) {
			var sum = weights.reduce(function (total, weight, index) {
				return total + parseInt(digits[index], 10) * weight;
			}, 0);
			var remainder = sum % 11;
			var checkDigit = remainder < 2 ? 0 : 11 - remainder;
			return parseInt(digits[weights.length], 10) === checkDigit;
		});
	}

	cpfCnpjInput.addEventListener("input", function () {
		cpfCnpjInput.value = formatCpfCnpj(digitsOf(cpfCnpjInput.value));
		cpfCnpjError.classList.add("hidden");
	});

	form.addEventListener("submit", function (event) {
		event.preventDefault();

		var cpfCnpj = digitsOf(cpfCnpjInput.value);
		if (!isValidCpfCnpj(cpfCnpj)) {
			cpfCnpjError.classList.remove("hidden");
			cpfCnpjInput.focus();
			return;
		}

		button.setAttribute("disabled", true);

		frappe.call({
			method: "payments.templates.pages.asaas_checkout.make_payment",
			freeze: true,
			freeze_message: __("Creating your charge..."),
			headers: {
				"X-Requested-With": "XMLHttpRequest",
			},
			args: {
				token: token,
				cpf_cnpj: cpfCnpj,
				payer_name: document.querySelector("#payer-name").value,
				payer_phone: digitsOf(document.querySelector("#payer-phone").value),
			},
			callback: function (r) {
				if (r.message && r.message.redirect_to) {
					window.location.href = r.message.redirect_to;
				} else {
					button.removeAttribute("disabled");
				}
			},
			error: function () {
				button.removeAttribute("disabled");
			},
		});
	});
});
