/* global braintree */
// Braintree's Drop-in on the bare payment page (decision 002 §1, §3).
(function () {
  const { boot, call, show, finish } = window.paymentPage;
  const form = document.getElementById("payment-form");
  const button = document.getElementById("submit");

  braintree.dropin.create(
    {
      authorization: boot.client_token,
      container: "#bt-dropin",
      paypal: { flow: "vault" },
    },
    (createErr, instance) => {
      if (createErr) return show(boot.text.error, true);
      instance.on("paymentMethodRequestable", () => (button.disabled = false));
      instance.on("noPaymentMethodRequestable", () => (button.disabled = true));
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        button.disabled = true;
        instance.requestPaymentMethod((err, payload) => {
          if (err) {
            button.disabled = false;
            return show(err.message, true);
          }
          call("payments.templates.pages.braintree_checkout.make_payment", {
            payload_nonce: payload.nonce,
            data: boot.data,
            reference_doctype: boot.data.reference_doctype,
            reference_docname: boot.data.reference_docname,
          })
            .then((r) => finish(r, r && r.status === "Completed"))
            .catch(() => finish(null, false));
        });
      });
    }
  );
})();
