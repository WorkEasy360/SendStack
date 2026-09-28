# Billing, trials and payments

How money and access work in this deployment, what is enforced where, and what
is deliberately not built yet.

The one-line version: **the backend decides everything, and a payment
activates nothing until the provider confirms it server-side.**

---

## The chain

```
Plan  →  Subscription  →  Entitlement service  →  Quotas + Features  →  Send gate
```

Nothing skips a link. A trial is a `Subscription` in `TRIALING` on a real
`Plan`, so trials flow through the Phase 2 entitlement service exactly as paid
subscriptions do — there is no "is this a trial?" branch anywhere in quota or
feature enforcement, because a second entitlement path is a second path to get
wrong and the one nobody tests.

---

## Trials

| | |
|---|---|
| Configured by | `BILLING_TRIAL_PLAN` (a platform-owned plan's name), `BILLING_TRIAL_DAYS` |
| Started by | self-service signup, if `TRIALS_ENABLED` and the plan resolves |
| Ends | on `Subscription.trial_ends_at`. No grace period. |
| On expiry | premium access stops, **data is kept in full** |

**Expiry is derived from the date, not from a job.** `Subscription.access_state()`
reads `trial_ends_at` and answers `EXPIRED` whether or not the lifecycle sweep
has run. This is the single most important property in this document: if expiry
depended on Celery, a dead beat container would silently hand every trialling
tenant unlimited premium access for as long as nobody looked.

`apps.subscriptions.tasks.sweep_subscription_lifecycle` (hourly) does the
bookkeeping only — it writes `status = EXPIRED` so the billing page can say
"your trial ended on the 14th", and it sends the two notices. Both are safe to
skip, safe to repeat, and safe to run twice at once.

### What expiry does and does not do

```
TRIAL EXPIRES
    ↓
PREMIUM ACCESS STOPS      ← quota checks refuse, features switch off
    ↓
DATA REMAINS              ← contacts, campaigns, messages, history: all of it
```

Nothing is deleted by expiry, by downgrade, or by cancellation. A workspace
over its new contact limit after a downgrade keeps all 8,000 contacts and is
blocked from adding the 8,001st. That behaviour is Phase 2's and is unchanged.

### No trial grace period

Deliberate, and stated rather than implied: a trial ends on the day the signup
page promised it would. A grace period would have been modelled explicitly
(as `PAST_DUE` is, with `BILLING_PAST_DUE_GRACE_DAYS`) rather than faked by
leaving the status at `ACTIVE`.

---

## Payments

**Provider: Razorpay.** Chosen because this deployment prices in INR by
default. Everything provider-specific is behind
`apps/billing/providers/base.py`; nothing above that layer knows the word
"Razorpay", and swapping it is writing a sibling of `providers/razorpay.py`.

### Safe defaults

| State | Behaviour |
|---|---|
| `PAYMENTS_ENABLED=false` | No checkout offered. Plans are assigned by an operator. |
| Enabled, no credentials | Checkout returns **503**, never a fake success. `check_production` FAILs. |
| Enabled and configured | Checkout works. |
| No webhook secret | Webhooks are **refused outright**. An unsigned webhook endpoint is an unauthenticated "activate my subscription" endpoint. |

### The flow

```
Customer picks a plan
        ↓
Server resolves the plan, prices it, writes a Checkout row   ← the amount lives here
        ↓
Provider order created
        ↓
Customer pays on the provider's hosted page                  ← we never see a card
        ↓
    ┌───┴────────────────────────┐
    ▼                            ▼
Browser callback            Webhook (signed)
    │                            │
    └──────────┬─────────────────┘
               ▼
        Signature verified
               ▼
        Provider API asked what really happened               ← the authority
               ▼
        Amount + currency compared to the Checkout row
               ▼
        Subscription updated, entitlements follow
```

Both paths end in `services._settle`. A third — `reconcile_checkout` — asks
the provider directly, for the case where the money was taken, the webhook was
lost, and the customer closed the tab.

### What cannot happen

* **A browser cannot activate a plan.** `{"payment_status": "success"}` in the
  body does nothing; only a provider-confirmed `captured` payment settles.
* **A cheap payment cannot buy an expensive plan.** The amount is compared
  against the `Checkout` row written before the customer reached the provider.
  A mismatch is refused, audited (`billing.amount_mismatch`), and never
  partially applied.
* **A webhook cannot be processed twice.** `WebhookEvent(provider, event_id)`
  is a unique constraint, so a redelivery loses the insert — which is the
  normal, expected way a duplicate is handled. Checking first and acting second
  would lose the race, because provider retries are concurrent.
* **A payment cannot be captured twice against one order.**
  `Payment(checkout, status=CAPTURED)` is a partial unique constraint.
* **A webhook cannot bill the wrong tenant.** Tenant mapping goes through our
  own `Checkout` row, found by provider order id. The `notes` we put in the
  order are never read back: a signature proves the message came from the
  provider, not that the provider was told the truth.
* **A customer cannot buy another reseller's plan.** `resolve_plan` filters
  through `purchasable_plans`, which is scoped to the workspace's own parent.

### Signatures

Two, and they are different:

| | Message | Key |
|---|---|---|
| Checkout callback | `order_id\|payment_id` | API secret |
| Webhook | the **raw request body** | webhook secret |

Both compared with `hmac.compare_digest`. The webhook digest is over
`request.body`, not `request.data` re-serialised — a re-serialised body has
different whitespace and key order and would never verify.

### Price changes

A `Checkout` freezes the amount at the moment it is created, and verification
compares against that row rather than re-reading the plan. A plan's price
changing tomorrow does not change what an order opened today is for.

Historical `Payment` rows are never rewritten.

---

## Coupons and promotions (Phase 6)

A promotion is a `Coupon` row plus the discount frozen onto a `Checkout`.
**The plan is never touched.** `Plan.price_cents` stays the list price for
everybody, and a promotion ending changes nothing about the plan, anybody's
existing subscription, or a payment that has already happened.

That is the whole reason a coupon is a separate object rather than a temporary
edit to a price: an edited price is a promotion you cannot end, cannot
restrict to one tier, and cannot report on afterwards.

### Kinds

| Kind | `value` means | Applied |
|---|---|---|
| `PERCENT` | 1–100 | At checkout. Rounds **down**, so the rounding error is the seller's |
| `FIXED` | minor units | At checkout. Refused against a plan in a different currency — this application holds no exchange rates and checkout is the worst place to invent one |
| `TRIAL_EXTENSION` | days | Not a discount. Moves `trial_ends_at`, through its own endpoint |

### What a client may send

**A string.** There is no amount field, no discount field and no plan-price
field on any request. `create_checkout` resolves the code, decides what it is
worth from the coupon row, and freezes the result into `Checkout.amount_minor`
— so the existing amount check in `_settle` guards a discounted order with no
new code and, more importantly, no new gap.

`Checkout.list_amount_minor` and `Checkout.discount_minor` are stored beside
it, so a receipt can say "₹999, less ₹200" a year later, when the coupon may
have been deleted and the plan reprinted.

### Redemption limits, and why reservations exist

Counting redemptions at capture is the obvious design and it is wrong: fifty
checkouts opened at once all see zero used, all pass, and a coupon "limited to
100" is redeemed 150 times.

So a redemption is **reserved** when the checkout is created, under a lock on
the coupon row, and:

* becomes `REDEEMED` when the payment captures — idempotent, so the webhook
  and the browser callback both arriving settles it once;
* becomes `RELEASED` when the checkout expires, so an abandoned payment does
  not strand a slot. `expire_stale_checkouts` does this.

`release` only ever moves `RESERVED` rows, so a late failure webhook for an
earlier attempt cannot undo a redemption a paying customer has already made.

### Refusals are deliberately vague

Every failure — expired, exhausted, wrong plan, wrong reseller, never
existed — returns the **same** message. A specific refusal is a code oracle:
"that code has expired" confirms to somebody guessing that they guessed a real
code. The specific reason goes to the log, where an operator answering "why
won't my code work" can find it. The endpoint is rate limited on top
(`THROTTLE_RATE_COUPON`).

### Ownership

Coupons are owned like plans: `owner=None` is the platform's, and a reseller's
belongs to them. Codes are unique **per owner**, so two resellers can both run
`WELCOME20` without knowing about each other, and the resolver only ever looks
in the set the workspace is actually offered.

---

## Changing plan (Phase 6)

`GET /api/v1/billing/plan-change/?plan=<id>` returns, before anything is
committed: the current and new plan, the price difference, every limit that
moves, what that means for usage already in place, which features are gained
or lost, when it takes effect, and a list of warning sentences.

**A downgrade never deletes anything.** A workspace dropping to a plan allowing
2,000 contacts while holding 3,400 keeps all 3,400; what it loses is the
ability to add the 3,401st. The preview says so explicitly, per metric, with
the numbers — because a customer who finds that out afterwards has been
surprised by their billing, and one who finds it out here has made a decision.

`POST` to the same path applies a **downgrade only**. An upgrade is a payment
and goes through checkout: letting this endpoint move a workspace onto a more
expensive plan would hand out the paid product for free, and it is refused at
the service layer as well as at the view.

A downgrade does **not** restart the billing period. Restarting would hand a
fresh monthly message allowance to somebody moving to a smaller plan, which is
the wrong way round.

---

## Deferred, and why

| | Status | Note |
|---|---|---|
| **Automatic recurring billing** | Deferred | Payments are one-off orders against a `Checkout`. A period is started fresh on payment, not extended. Provider-managed subscriptions (mandates/autopay) are a separate integration. |
| **Dunning** | Deferred | `PAST_DUE` → grace → expiry exists in the model and is driven by an operator, not by a failed-charge schedule. |
| **Tax / GST invoices** | Deferred | No tax is calculated and no tax invoice is generated. The billing page says so rather than showing an empty invoice table. Receipts come from the payment provider. |
| **Multi-currency** | Partial | `Plan.currency` is per-plan and `BILLING_CURRENCY` is the deployment default. Only one currency has been exercised end to end (INR). |
| **Refunds** | Deferred | Issued in the provider's dashboard. The `Payment.REFUNDED` status exists; nothing writes it yet. A refund does not release the coupon redemption it consumed. |
| **Proration** | Deferred | An upgrade starts a fresh period at the new price; unused time on the old plan is not credited. A downgrade keeps the current period and applies the lower ceiling to it immediately. Both are stated on the plan-change preview rather than implied. |

Do not read any of the above as working. They are named here because a
half-built billing feature that nobody documented is worse than one that is
absent.

---

## Reseller billing

```
Platform  →  Reseller  →  Customer
```

Unchanged by this phase. A reseller's customer sees **their reseller's**
public plans (`purchasable_plans` prefers `owner=<parent>`); a direct customer
of the platform sees the platform's. A reseller's private, negotiated plan is
not published on the platform pricing page and cannot be named at signup.

Self-service signup always produces a `CUSTOMER` tenant. It cannot create a
reseller, and there is no field in the request that could ask it to.

**Not supported:** reseller-specific commission, revenue share, or a reseller
taking payment through this system on the platform's behalf. A reseller
charging their own customers does so by their own arrangement, as before.

---

## Testing payments

Use the provider's **test mode keys**. Never test against a live key, and
never with a real customer's payment.

`apps/billing/tests.py` uses a fake provider for the application's rules and
tests the Razorpay signature schemes directly against known HMACs — that half
needs no network. What it does *not* cover is whether the adapter speaks
Razorpay's live API correctly; that needs a sandbox key and a manual run.
