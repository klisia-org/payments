// Helpers for the bare payment page (decision 002 §1). Plain JS: frappe's web bundle does not load here.
window.paymentPage = (function () {
  const boot = JSON.parse(document.getElementById("payment-boot").textContent);
  const message = document.getElementById("payment-message");

  function call(method, args) {
    return fetch(`/api/method/${method}`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Frappe-CSRF-Token": boot.csrf_token,
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify(args),
    }).then((r) =>
      r.json().then((body) => {
        if (!r.ok) throw new Error(boot.text.error);
        return body.message;
      })
    );
  }

  function show(text, isError) {
    message.textContent = text;
    message.classList.toggle("error", !!isError);
    message.hidden = !text;
  }

  function finish(result, ok) {
    // The server answers with where to go next, success or not.
    show(ok ? boot.text.success : boot.text.error, !ok);
    if (result && result.redirect_to) {
      setTimeout(() => (window.location.href = result.redirect_to), 1500);
    }
  }

  return { boot, call, show, finish };
})();
