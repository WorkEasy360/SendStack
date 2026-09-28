# Metrics dictionary

Every number this product reports about its own business, defined precisely
enough that two people reading the same dashboard reach the same conclusion.

The rule this document exists to enforce: **a metric without a written
definition is an argument waiting to happen.** "Churn was 4% last month" is
unusable until everyone agrees whether a trial that ended counts. So each
metric below states what is counted, what is deliberately excluded, and where
the code that computes it lives.

The definitions are also returned by the API alongside the numbers
(`GET /api/v1/reports/business/` → `definitions`), and rendered on the
business dashboard. That is deliberate: a definition kept only in a document
drifts from the implementation, and nobody notices for a quarter.

**Source of truth:** [`backend/apps/reports/business.py`](../backend/apps/reports/business.py).
When this file and that one disagree, the code is right and this is stale.

---

## North star

> **Campaigns successfully sent by an activated workspace, per week.**

One metric, and it is the one that means a customer got value. Not signups
(a form submitted), not messages queued (an intention), not logins (a habit
that may deliver nothing). A campaign that actually reached the provider,
sent by a workspace that has completed activation, is the smallest unit of
"this product did the thing somebody pays it for".

It is deliberately not revenue. Revenue is the *result* of this number, and
optimising the result directly is how a product ends up with good quarterly
figures and a churn problem two quarters later.

Computed from `volume.campaigns` restricted to activated workspaces; see
`business._volume` and `OnboardingState.activated_at`.

---

## Acquisition

### Signups
Workspaces of kind `CUSTOMER` created in the window.

Counts workspaces, not users: a five-person team signing up is one customer.
A workspace created by a reseller on a customer's behalf is included, because
it is a real customer of the platform however it arrived.

`business.activation()` → `signups`

### Visitors
**Not available.** This deployment runs no analytics beacon and sets no
tracking cookie, so there is no honest visitor count. The funnel therefore
starts at signup. A "visitors" figure could be scraped from nginx logs, but
bot traffic makes an unfiltered log count worse than no number at all.

### Landing → signup conversion
**Not available**, for the same reason. Reported as a gap rather than
estimated.

---

## Activation

### Activation
A workspace has **connected a WhatsApp number**, **has at least one contact it
is allowed to message**, and **has had at least one message accepted by the
provider**.

All three, and the third is the load-bearing one: `SENT`, `DELIVERED` or
`READ`, never `QUEUED`. A workspace whose every send fails has not activated,
and a definition that counted queued messages would say it had.

Owned by `apps.onboarding.services.ACTIVATION_REQUIREMENTS` and read from
there rather than restated, so the checklist a customer sees and the metric
an operator reads cannot drift apart.

### Activation rate
Workspaces created in the window that reached activation, divided by
workspaces created in the window.

**Cohorted by creation date, not activation date.** Cohorting by activation
would make the rate rise whenever an old customer finally got going, which
flatters the number and says nothing about whether onboarding works.

Reported as `null`, never `0`, when there were no signups in the window.

`business.activation()` → `activation_rate`

### Time to first value
**Not currently computed.** The data exists — `OnboardingState.created_at` and
`test_message_sent_at` — and it is the obvious next metric. It is listed here
as absent rather than approximated.

---

## Revenue

### MRR (monthly recurring revenue)
The sum of the **monthly-equivalent price of every subscription currently
entitled to service**.

- A yearly plan contributes **one twelfth** of its price.
- **Trials contribute nothing.** A trial is not revenue, and counting it is
  the single most common way a SaaS dashboard flatters itself.
- A **cancelled** subscription still inside a paid period **does** contribute:
  the money was taken and the service is being delivered.
- A **past-due** subscription inside its grace window contributes; past the
  window it does not. Decided by `Subscription.access_state()`, the same
  method the send gate consults, so revenue and entitlement cannot disagree.
- A free plan (price 0) contributes nothing and is not a paying customer.

A **snapshot of a run rate**, not a sum of payments. Arithmetic is done in
`Decimal` and rounded once at the end: dividing yearly prices by twelve in
integers and then summing loses a minor unit per subscription, which is small,
wrong, and exactly the kind of thing somebody spends an afternoon reconciling.

`business.mrr()` → `mrr_minor`

### ARR
MRR × 12. A run rate. Not a forecast, and not booked revenue.

### ARPU
MRR ÷ paying customers. Trials are in neither the numerator nor the
denominator.

### Revenue collected
Payments **actually captured** in the window.

A cash figure and **not MRR**. A yearly plan sold today moves this by twelve
months' worth and moves MRR by one month's worth. The dashboard prints both
with that sentence beside them, because the two look wildly inconsistent for a
month and somebody always asks.

`business.revenue_movements()` → `revenue_collected_minor`

### Failed payments
Payment attempts recorded with status `FAILED` in the window. A customer
retrying a declined card three times is three failures and one customer;
this is an operational count, not a customer count.

---

## Retention and churn

### Churn
Subscriptions that **ended** in the window: cancelled or expired.

**Inactivity is not churn.** A customer who has not sent anything for a month
has gone quiet, not churned; conflating the two produces a churn figure that
moves when nothing commercial has happened, and it is the most common
definitional error in SaaS reporting. Quiet workspaces are reported separately
as a health signal.

Expiry has no dedicated timestamp — the status moves and `updated_at` records
when — so the expiry half of this number is **approximate to within one status
change**. Said here rather than hidden.

### Churn rate
Churned in the window ÷ paying subscriptions that existed *before* the window
opened.

Subscriptions created inside the window are excluded from the denominator:
they could not have churned out of a population they were not in.

Reported as `null`, never `0%`, when the denominator is zero. A 0% churn rate
on a platform with no customers is a false reassurance.

### Active customer
A workspace that **sent at least one message** in the window.

Signing in is not activity. In a product whose value is delivered by sending,
a workspace whose owner opens the dashboard daily and sends nothing is not
retained — it is about to leave, and counting it as retained is how a
retention chart stays flat right up until the renewals do not.

### Returning customer
Sent in this window **and** in the window immediately before it.

### Lapsed customer
Sent in the previous window and **not** in this one.

Named "lapsed" rather than "churned" on purpose: they may still be paying.
This is a prompt to talk to somebody, not a revenue event.

### Retention rate
Returning ÷ previously active. `null` when nobody was active in the previous
window.

---

## Volume and quality

### Messages
`Message` rows created in the window, by `queued_at`.

### Sent
Messages that reached the provider: `SENT`, `DELIVERED` or `READ`.

### Delivery rate
Delivered ÷ sent, **where the provider reports delivery**. Not every provider
and not every message type produces a delivery receipt, so this is a rate over
the messages that can have one — never a claim about the rest.

### Failure rate
Failed ÷ total messages in the window. Reported as `null` on an empty window:
a 0% failure rate over no messages reads as healthy and means nothing.

Failures are grouped by `Message.failure_reason`, a stable code, and never by
`error_message`, which is prose written for a person and changes whenever the
wording improves.

### Skipped
Messages never attempted — no eligible recipient, no connected device, refused
by the consent gate. **Skipped is not failed**, and they are counted
separately: a campaign that skipped 40 opt-outs worked correctly.

---

## Deliberately not computed

### Customer acquisition cost (CAC)
**Not available.** This deployment records no acquisition spend — there is no
ad platform integration, no campaign cost field, and no place a marketing
invoice is entered. A CAC computed without cost data would be an invented
number wearing a currency symbol.

To produce one honestly, spend would have to be recorded per channel per
period first. That is a real feature, not a calculation.

### Lifetime value (LTV)
**Not available yet.** LTV needs enough completed customer lifetimes to
average over. Computing `ARPU / churn_rate` on a few months of data produces a
number that swings by a factor of three when one customer leaves, and
presenting it as a measurement rather than an extrapolation is misleading in
exactly the direction that encourages overspending.

Revisit when there are at least two full billing cycles of churn data across a
meaningful number of customers.

### Net promoter score, satisfaction
**Not collected.** No survey mechanism exists, and inventing a proxy from
support ticket volume would measure something else entirely.

---

## Where each number is computed

| Metric | Function | Endpoint |
|---|---|---|
| MRR, ARR, ARPU, paying, trialing | `business.mrr` | `/api/v1/reports/business/revenue/` |
| New, churned, collected, failed payments | `business.revenue_movements` | `/api/v1/reports/business/revenue/` |
| Signups, activation funnel, activation rate | `business.activation` | `/api/v1/reports/business/activation/` |
| New, active, returning, lapsed | `business.retention` | `/api/v1/reports/business/retention/` |
| Campaigns, messages, failure rate | `business._volume` | `/api/v1/reports/business/` |
| Acquisition funnel by stage | `business._funnel` | `/api/v1/reports/business/` |
| Health signals | `business.health_signals` | `/api/v1/reports/business/health/` |
| Provider reconciliation | `business.reconciliation` | `/api/v1/reports/business/reconciliation/` |

All of these are **superadmin only** and return `404` — not `403` — to anybody
else. A 403 would confirm that a platform-wide revenue report exists at that
path. A reseller is not platform staff here: these numbers span every
reseller, which is precisely what one must never see about another.

---

## Health signals

Not metrics but prompts: workspaces where something has gone wrong that
somebody could help with.

| Signal | Definition |
|---|---|
| `never_activated` | Signed up more than 3 days ago, never activated |
| `gone_quiet` | Sent before, nothing in the last 30 days |
| `device_disconnected` | A WhatsApp number in `DISCONNECTED` state |
| `trial_ending` | Trial ends within 3 days |
| `repeated_failures` | 20 or more failed messages in the last 7 days |

Deliberately **not a behavioural profile**. Nothing here records how often
somebody signs in, how long they stay, or what they look at. That would be
surveillance rather than support, and it would not tell anybody what to do
next.

**Nothing is sent automatically.** Appearing on this list is a prompt for a
person to decide what to do. Automating outreach off it would turn an
operational tool into a mailing list, and the consent rules that govern
messaging a *contact* are not the rules that govern messaging a *customer* —
see [CONSENT.md](CONSENT.md) and the transactional/marketing split in
`apps/notifications`.
