/* global Razorpay */
// Razorpay Checkout on the bare payment page (decision 002 §1).
(function () {
  const { boot, call, finish } = window.paymentPage;
  const status = document.getElementById("razorpay-status");
  const options = Object.assign({}, boot.options);
  if (!options.subscription_id) delete options.subscription_id;
  if (!options.order_id) delete options.order_id;

  options.handler = (response) => {
    status.textContent = boot.text.confirming;
    call("payments.templates.pages.razorpay_checkout.make_payment", {
      razorpay_payment_id: response.razorpay_payment_id,
      options: JSON.stringify(boot.options),
      reference_doctype: boot.reference_doctype,
      reference_docname: boot.reference_docname,
      token: boot.token,
    })
      .then((r) => finish(r, r && r.status === 200))
      .catch(() => finish(null, false));
  };
  new Razorpay(options).open();
})();
