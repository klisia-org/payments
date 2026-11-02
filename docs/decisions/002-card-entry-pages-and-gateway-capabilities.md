# 002 — Card entry pages and gateway capabilities

**Date:** 2026-11-02
**Status:** Accepted 2026-11-02; implemented 2026-11-02 (37763db)

## Context

No gateway here collects card numbers in our own fields. Asaas, PayPal, Paytm and Paymob redirect to
the gateway. GoCardless, MoMo and M-Pesa take no cards. Stripe, Braintree and Razorpay show the
gateway's card frame inside our checkout page. That page extends `templates/web.html`, which loads
Website Script, Website Settings head HTML and every app's `web_include_js`. A merchant on SAQ A must
confirm that the page holding the frame is not exposed to scripts that could tamper with it. That
cannot be confirmed for a page that runs whatever an admin pastes in. Stripe also still uses
`createToken` and the Charges API, which has no 3-D Secure, and Braintree's Drop-in is pinned to
1.9.3 (2018). A consuming app cannot ask which gateways keep card entry off its pages or support
recurring charges.

## Decision

1. **Card frames load on a bare page.** The Stripe, Braintree and Razorpay checkouts extend a new
   `templates/payment_page.html`, not `web.html`. It has no Website Script, head HTML or
   `web_include_js`. It takes the logo and brand colours as CSS only. It sends a
   Content-Security-Policy that allows scripts only from our origin and that gateway's domains, with
   a nonce for the page's own boot values.
2. **Stripe moves to PaymentIntents and the Payment Element.** The browser creates a payment
   method; the server confirms the intent, and 3-D Secure runs in Stripe's frame when the bank
   asks. Cards only, wallets included: methods that settle days later need a Stripe webhook, which
   this app does not have. The Charges path is removed, for subscriptions too.
3. **Braintree's Drop-in moves to the current 1.x release.** Each gateway's script URL and version
   live in one place, so updates are deliberate.
4. **Gateways declare their capabilities.** `payments.utils.capabilities.get(gateway)` returns
   `card_entry` (`redirect`, `frame` or `none`) and `recurring` (true when this app creates the
   gateway's subscriptions: Stripe, PayPal, Asaas today). Consuming apps offer only gateways that
   suit them.
5. **A test pins it.** Every `frame` gateway's checkout extends `payment_page.html`, and the policy
   holds only the listed domains.

## Consequences

Merchants using any gateway here can file SAQ A. Card pages lose site-wide scripts, analytics
included, by design. Stripe card payments meet 3-D Secure rules (EU, UK, India, Brazil). Rewriting
Stripe touches every consuming app's Stripe flow once. PayPal's recurring support uses the legacy NVP
recurring profiles; moving it to PayPal Subscriptions is a later decision.
