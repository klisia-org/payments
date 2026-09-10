# MTN MoMo Sandbox — Test Phone Numbers

Per-MSISDN outcome map observed against this fork's MoMo sandbox account.
The last digit of the MSISDN determines the simulated transaction result.

> **Use `46733123454` for end-to-end SUCCESSFUL tests.**

## Observed mapping

| MSISDN | Final `status` | `reason` | Notes |
|---|---|---|---|
| `46733123450` | `FAILED` | `INTERNAL_PROCESSING_ERROR` | |
| `46733123451` | `FAILED` | `APPROVAL_REJECTED` | |
| `46733123452` | `FAILED` | `EXPIRED` | |
| `46733123453` | *no resolution* | — | Stays `PENDING`; client times out after 120s. |
| **`46733123454`** | **`SUCCESSFUL`** | — | Returns `financialTransactionId`. **Use this for happy-path tests.** |
| `46733123455` | `FAILED` | `PAYER_NOT_FOUND` | |
| `46733123456` | *not tested* | — | |
| `46733123457` | *not tested* | — | |
| `46733123458` | *not tested* | — | |
| `46733123459` | *not tested* | — | |

## Caveats

- **Mapping is account-specific.** Different MTN sandbox provisionings expose different per-digit behaviors. The table above is what *this* sandbox account returned in April 2026 — re-verify before relying on it elsewhere.
- **Currency.** All tests above used `EUR`, which is the default sandbox account currency. Production deployments will use a real African currency (e.g. `LRD` for Liberia, `XAF` for Cameroon, `GHS` for Ghana).
- **Subscription product.** The subscription key on MoMo Settings must be from the **Collection** product, not Disbursement or Remittance.
- **API user provisioning.** Sandbox requires `POST /v1_0/apiuser` followed by `POST /v1_0/apiuser/{uuid}/apikey`. Skipping the first step leads to misleading errors like `INTERNAL_PROCESSING_ERROR` or stuck `PENDING`.
- **Polling timeout.** `momo_checkout.html` polls every 4s for a max of 120s. If MTN never resolves a transaction (e.g. `…53`), the UI shows a timeout — that is *not* a `FAILED` status, and the Integration Request stays `Queued`.

## Sample SUCCESSFUL response

```json
{
  "amount": "220",
  "currency": "EUR",
  "externalId": "ACC-PRQ-2026-00005",
  "financialTransactionId": "1869662433",
  "payeeNote": "Thank you",
  "payer": {
    "partyId": "46733123454",
    "partyIdType": "MSISDN"
  },
  "payerMessage": "Payment",
  "status": "SUCCESSFUL"
}
```
