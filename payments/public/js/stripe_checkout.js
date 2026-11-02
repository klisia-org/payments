/* global Stripe */
// Stripe's Payment Element on the bare payment page (decision 002 §1–§2). The payment method is
// created here and charged on the server; 3-D Secure, when the bank asks, runs in Stripe's frame.
(function () {
  const { boot, call, show, finish } = window.paymentPage;
  const data = boot.data;
  const stripe = Stripe(boot.publishable_key);
  const elements = stripe.elements({
    mode: boot.mode,
    amount: boot.amount,
    currency: boot.currency,
    paymentMethodTypes: ["card"],
    appearance: {
      variables: {
        colorPrimary: getComputedStyle(document.documentElement)
          .getPropertyValue("--payment-primary")
          .trim(),
      },
    },
  });
  elements.create("payment").mount("#payment-element");

  const form = document.getElementById("payment-form");
  const button = document.getElementById("submit");
  const label = button.textContent;

  function reset(text) {
    show(text, true);
    button.disabled = false;
    button.textContent = label;
  }

  function settle(result) {
    if (result && result.requires_action) {
      return stripe
        .handleNextAction({ clientSecret: result.client_secret })
        .then((r) => {
          if (r.error) return reset(r.error.message);
          return call(
            "payments.templates.pages.stripe_checkout.confirm_payment",
            {
              integration_request: result.integration_request,
            }
          ).then(settle);
        });
    }
    if (result && result.message && !result.redirect_to)
      return reset(result.message);
    form.hidden = true;
    finish(result, result && result.status === "Completed");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    show("");
    button.disabled = true;
    button.textContent = boot.text.processing;
    const submitted = await elements.submit();
    if (submitted.error) return reset(submitted.error.message);
    const { error, paymentMethod } = await stripe.createPaymentMethod({
      elements,
      params: {
        billing_details: {
          name: document.getElementById("payer-name").value,
          email: document.getElementById("payer-email").value,
        },
      },
    });
    if (error) return reset(error.message);
    call("payments.templates.pages.stripe_checkout.make_payment", {
      payment_method_id: paymentMethod.id,
      data: data,
      reference_doctype: data.reference_doctype,
      reference_docname: data.reference_docname,
      payment_gateway: data.payment_gateway,
    })
      .then(settle)
      .catch((e) => reset(e.message));
  });
})();
