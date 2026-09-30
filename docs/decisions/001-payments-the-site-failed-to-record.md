# 001 — Payments the site failed to record

**Date:** 2026-09-30
**Status:** Accepted

## Context

A gateway confirms the money, then calls `on_payment_authorized` on the reference document, which
records it (a Payment Entry, an enrollment). When that call raises, every gateway logs the error and
moves on: the Integration Request says `Completed`, the invoice stays unpaid, and only the Error Log
knows. Paytm and M-Pesa go further and mark the request `Failed` although the money was taken. The
payer sees success, so nobody is prompted to look. On tlinktest an Asaas payment was lost this way to
a permission error in the consuming app.

## Decision

1. **One helper, `payments.utils.recording.record_payment`**, replaces the direct
   `run_method("on_payment_authorized", …)` in Asaas, Stripe, Braintree, GoCardless, Razorpay, PayPal,
   Paytm, Paymob and M-Pesa. It runs the call inside a savepoint; on failure it rolls back to the
   savepoint and inserts an **Unrecorded Payment** holding the gateway, Integration Request, reference,
   status passed, the user the call ran as, `frappe.flags.data` if set, and the error. The gateway's
   own response (redirect, webhook 200) is unchanged.
2. The Integration Request records what the gateway said. Paytm and M-Pesa mark it `Completed` when
   the money is confirmed, whatever the reference does.
3. **An hourly job retries** each open Unrecorded Payment as its original user, three times. Success
   marks it Resolved. After the third failure it becomes **Needs Attention**, and every user holding
   a role that can read Unrecorded Payment gets a notification (in-app, and email by their settings).
4. Needs Attention offers **Retry** and **Mark Resolved** (for a payment recorded by hand).
5. MoMo is left as is: its finalizer is idempotent, rolls back whole, and hourly polling retries it.

Rejected: a new Integration Request status. Razorpay already uses `Authorized` for
"authorized, not captured", and Log Settings deletes Integration Requests after 30 days.

## Consequences

A confirmed payment can no longer disappear into the Error Log; it stays on a list until someone or
the retry clears it. Staff are told only after retries stop, so a manual fix never races an automatic
one. The cost is up to three hours before a person hears of it.

A retry can double-record if a consuming app's `on_payment_authorized` commits part way (the
savepoint cannot undo a commit). PayPal commits only after the call, which is safe.

Open: only System Manager can read Unrecorded Payment by default; a consuming app should grant its
bursar role. MoMo raises no notification when its polling keeps failing.
