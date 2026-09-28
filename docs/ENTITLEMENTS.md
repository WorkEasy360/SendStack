# Entitlements

What a customer's plan lets them consume, where that is enforced, and what
happens when it is not.

The rule the whole design serves:

> A customer can only consume the features and resources their current
> subscription entitles them to, whether the request comes from the UI, the
> REST API, Celery, an automation, a workflow, a CSV import, or an internal
> service call.

The frontend explains limits. **The backend enforces them.** The database
protects against two requests racing for the last slot. The credit ledger
stays authoritative for credits.

---

## The shape of it

```
Tenant
  └── Subscription        (one per tenant; status + billing period)
        └── Plan          (limits + feature switches)
              ↓
     apps/subscriptions/services.py      ← the only place that decides
       ├── Quotas
       │     ├── messages_per_month
       │     ├── contacts
       │     ├── devices
       │     └── users
       └── Features
             ├── ai_agent
             ├── workflows
             ├── api_access
             ├── click_tracking
             ├── number_filter
             └── group_grabber
```

Nothing outside `apps/subscriptions` reads `Plan` or `Subscription` to make a
decision. Everything asks the service.

### The service

| Function | Answers |
| --- | --- |
| `get_active_subscription(tenant)` | The subscription in force, whatever its status |
| `get_current_plan(tenant)` | The plan whose rules apply, or `None` for unlimited |
| `check_quota(tenant, metric, requested_amount=1)` | `(allowed, reason)`; **fails closed** |
| `require_quota(...)` | The same, raising `QuotaExceeded` (403) |
| `quota_guard(tenant, metric, n)` | A context manager: checks *and holds* a slot across a create |
| `feature_enabled(tenant, feature)` | `True`/`False`; **fails closed** |
| `require_feature(...)` | The same, raising `FeatureNotAvailable` (403) |
| `get_usage(tenant)` | Four aggregate counts |
| `get_entitlements(tenant)` | Everything the UI renders. A read — does **not** fail closed |
| `claim_message_allowance(message)` | Takes one message off the month, atomically |
| `release_message_allowance(message)` | Gives it back |

Metric names are accepted either way round: `contacts` and `max_contacts` are
the same ceiling. No database field was renamed.

**0 means unlimited**, the same convention team capacity and AI reply caps
already use.

---

## Quotas

### `messages_per_month`

The hard one, because a count and a send are two steps and any number of
workers can read the same free slot in between.

* **Enforced in** `apps/messaging/gating.py` — the single send gate every
  outbound message passes through, called immediately before the send from
  `apps/messaging/tasks.send_message_task`. Campaign, inbox reply, automation,
  workflow and follow-up all arrive there.
* **Also checked at** `apps/campaigns/services.launch`, as a pre-flight, so
  nobody launches ten thousand messages that would be skipped one at a time.
  The gate remains the authority — a campaign launched at 9am has no claim on
  the allowance an inbox reply spends at 11.
* **Concurrency:** the allowance is *claimed*, not merely checked. Inside one
  transaction, under a `SELECT … FOR UPDATE` on the tenant row, the service
  counts the period's usage and stamps `Message.plan_counted_at`. A second
  worker blocks on that lock and, when it proceeds, its count already includes
  the first worker's message.
* **Idempotent:** a message that already carries a stamp has spent its slot, so
  a hold, a retry or a redelivered task claims nothing further.
* **Released** wherever the credit reservation is released — a blocked send, a
  send with no connected device, and a terminal failure. A message that never
  reached anybody costs neither a credit nor a month's quota.
* **Usage** = outbound messages whose `plan_counted_at` falls in the current
  period. Not "messages that exist": counting QUEUED rows was the pre-Phase-2
  bug that let a plan allowing 100 messages send **none** of them, because a
  campaign of 100 was counted in full the moment it was queued.
* **Errors:** the gate returns `BLOCK` and the message is marked `SKIPPED` with
  the reason on the row. `NOTICE` messages (opt-out confirmations) are exempt —
  cutting off the message that says "you are over your limit" would be a poor
  way to deliver that news.

### `contacts`

* **Enforced in**
  * `apps/contacts/views.ContactViewSet.perform_create` — manual creation
  * `apps/contacts/tasks.process_contact_import` — CSV import, per row
* **CSV import** charges only rows that genuinely add somebody: after phone
  normalisation, after duplicates inside the file, after contacts already on
  the account. A 5,000-row re-export of a list already imported adds nothing
  and is never refused. An import that runs out of room part-way **keeps what
  it wrote**, stops, and reports the row it stopped at plus how many were left.
* The upload itself is never refused up front — how many contacts a file adds
  cannot be known without reading it, and refusing a free re-import because the
  account is full would be wrong. `POST /contacts/imports/preview/` answers the
  question properly before anybody presses Import, returning `within_quota` and
  `quota_warning`.
* **Concurrency:** `quota_guard` — the tenant row lock is held across the
  create. Import takes it only for rows that will actually create, so a
  10,000-row import adding 12 contacts contends 12 times, not 10,000.
* **Not enforced** on the two inbound paths — `apps/inbox/services.record_inbound_message`
  and `apps/voice/services` — where a contact is created because a real person
  just messaged or called. Refusing there would mean **dropping their message**,
  which is worse than being one over a limit. Those contacts still count
  toward usage, so the tenant is over their limit and blocked from adding more
  themselves until they upgrade. This is a deliberate, documented exemption.

### `devices`

* **Enforced in** `apps/whatsapp/views.WhatsAppAccountViewSet.perform_create`,
  the only path that creates a `WhatsAppAccount`.
* **Concurrency:** `quota_guard`. Two requests arriving on the last free slot
  both block on the tenant row; the second counts the first one's device and is
  refused.
* **Connecting, reconnecting, refreshing a QR and disconnecting are not
  gated** — they act on a device that already holds its slot and must never be
  refused for holding it.

### `users`

Counting rule, in full:

> The **active, undeleted** users whose `tenant` is the tenant being checked.

Not counted: superadmins in other tenants, resellers, users of other tenants
below this one, suspended accounts, soft-deleted accounts.

* **Enforced in**
  * `apps/accounts/views.ManagedUserViewSet.create`, against the tenant the new
    account will belong to
  * `ManagedUserViewSet._set_active(..., is_active=True)` — reactivating puts
    somebody back into a workspace's headcount, so it is a create as far as the
    plan is concerned. Suspending never is: an account can always be taken
    *out* of a workspace that is over its limit.
* **Today the first of those never bites.** Every account this endpoint creates
  gets its own new tenant, so the count is always zero — which is exactly what
  the audit meant by the team subsystem being unreachable. The check is in
  place so that the day a second user can join an existing workspace, the
  ceiling is already there rather than shipped afterwards. Team invites and
  role permissions are a later phase.

---

## Features

Every switch is checked at **execution** time, not only where the feature is
configured. A workflow built on a plan that included workflows must stop when
the plan changes; a Celery task queued before a downgrade must not run on the
old plan's terms.

### `ai_agent`

| Entry point | Behaviour when off |
| --- | --- |
| `ai/services.answer()` — the choke point every automated reply reaches (inbound ladder, `generate_reply_task`, workflow `AI_REPLY` node) | Returns `False`, logs, leaves the conversation untouched |
| `ai/services.maybe_reply()` | Returns `False` without queueing a task |
| `ai/services.ask()` — the test panel, `POST /ai/ask/`, `POST /ai/knowledge-bases/{id}/ask/` | 403 `FEATURE_NOT_AVAILABLE` |
| `ai/services.suggest_reply()` / `summarise()` | 403 |
| `PATCH /ai/agent/` | 403 — configuring a feature you do not have is how a downgraded tenant quietly keeps one |
| `GET /ai/agent/` | **Allowed.** The settings screen renders the locked state from it |
| `POST /ai/agent/clear-key/` | **Allowed.** Removing your own credential is always allowed |
| `ai/tasks.embed_document_task` | Returns 0 without calling the embedding endpoint — a paid call the tenant will never query |
| Knowledge recommendations | 403 |
| The `KNOWLEDGE` automation preset | Refused with the upgrade sentence |

**Not gated:** uploading, editing, reading and deleting knowledge documents.
They are the customer's own data, and holding it hostage is not a pricing
strategy. Lexical retrieval over them keeps working.

**Not gated:** the voice agent. It is a separate product surface with no plan
switch of its own; folding it under `ai_agent` would be inventing pricing this
phase was not asked to invent.

### `workflows`

| Entry point | Behaviour when off |
| --- | --- |
| `POST /automation/workflows/` | 403 |
| `PATCH`/`PUT` on one — including flipping `is_active` | 403 |
| `POST /automation/workflows/bulk/` with `activate` | Reported as a failed row, the way every bulk endpoint here reports refusals |
| `services.start_workflow()` — covers `POST /workflows/{id}/run/` | `WorkflowError` naming the plan |
| `services.start_workflows_for()` — the inbound trigger ladder | Returns `False`; the rest of the ladder still runs |
| `services.resume_workflow()` — a run waking from a WAIT node | The run is **finished as FAILED** with the reason on it. A sequence that simply went quiet is indistinguishable from one that broke |

**Allowed when off:** listing, reading and deleting. A customer keeps what they
built and must be able to tidy it up.

**Not gated:** welcome messages, keyword auto-replies and follow-up rules.
Those are `AutomationRule`/`FollowUpRule`, not workflows, and are on every plan.

### `click_tracking`

* **Enforced in** `apps/tracking/services.rewrite()` — the only place a
  `TrackedLink` is ever created, reached from campaigns, automation sends and
  inbox replies alike.
* When off, the text comes back **unchanged** and no rows are written. The
  message still sends and the tenant's own URLs still work. A campaign whose
  links are broken is far worse than one whose clicks are not counted.
* `apps/campaigns/services._build_messages` asks once per campaign and passes
  the answer down, so building ten thousand messages is not ten thousand plan
  lookups.
* **Deliberately still working when off:** `GET /r/<code>` keeps redirecting
  and keeps recording the click. Those links are already in people's phones;
  breaking them punishes the contact, not the customer. Historical reports stay
  readable for the same reason.

### `number_filter`

* **Enforced in** `apps/leadtools/views.NumberCheckViewSet` as a permission:
  the whole route is the feature, so a plan without it gets a 403 on every
  call, including reads of old checks.
* What it gates is the *asking*: a paced, batched registration lookup through
  one of the workspace's QR-linked numbers. Cloud API numbers cannot do this
  at any plan level -- Meta has no contact-lookup endpoint -- and the screen
  says so rather than offering them.
* **Not a consent source.** Numbers saved from a check go through the ordinary
  contact importer with consent left UNKNOWN, exactly like a pasted list.

### `group_grabber`

* **Enforced in** `apps/leadtools/views.GroupGrabViewSet` and
  `WhatsAppGroupsView`, the same way.
* Reads the groups a linked number belongs to and imports their members into
  a contact group. Members WhatsApp exposes only by an anonymous id are
  counted as hidden, never guessed at.
* **Not a consent source**, for the same reason as above and with more force:
  being in a group is not agreeing to be marketed to. The consent gate in
  front of every campaign applies to these contacts unchanged.

### `api_access`

There is no public customer-facing REST API in this product yet. Phase 2
establishes the boundary and nothing more:

* `apps/subscriptions/permissions.RequiresApiAccess` — a DRF permission class
  that refuses with the upgrade sentence.
* `apps/subscriptions/permissions.RequiresFeature.for_feature(...)` — the
  general form, for any route that *is* one feature.

**API access entitlement prepared; public API implementation deferred to a
later phase.** No fake API was invented to mark the feature complete.

---

## Subscription status

`status` records what a person decided. `access_state()` says what that
currently means. They are not the same question.

| Status | Access | Detail |
| --- | --- | --- |
| `ACTIVE` | `FULL` | Everything the plan grants. The usage window rolls on by itself. |
| `TRIALING` | `FULL` | Identical to ACTIVE, bounded by the plan assigned to the trial. |
| `PAST_DUE` | `FULL`, then `EXPIRED` | Full entitlements for `BILLING_PAST_DUE_GRACE_DAYS` (default 14) after the period end. Nothing here collects money, so "past due" is a flag somebody set by hand; cutting a customer off the instant it is set would make it unusable. |
| `CANCELLED` | `FULL` until `current_period_end`, then `EXPIRED` | They paid for the month; they keep the month. `POST /billing/subscriptions/{id}/cancel/` stamps the end of the window they are *currently in*. |
| — `EXPIRED` | `EXPIRED` | **Not a stored status.** It is the derived state of a cancelled subscription past its end, or a past-due one past its grace. Reported as `access: "EXPIRED"`. |

`EXPIRED` means **no paid entitlements**: every feature off, every quota
refused. Reading data, exporting it and deleting it all keep working. Nothing
is deleted, ever.

### The billing period

`current_period_end` used to be decorative — a cancellation had no date to run
out on and `renew()` cleared the only one there was. Now:

* `Subscription.save()` fills a missing end from the plan's interval (30 days
  monthly, 365 yearly), rolled up to the period containing *now* so a
  subscription written months ago is not handed an end date in the past.
* `Subscription.current_period()` is what usage is measured over. For a **live**
  subscription it steps the stored anchor forward by whole intervals until the
  window contains now — so **the monthly allowance resets every month without
  anybody pressing Renew**, which matters because nothing in this deployment
  renews on a schedule. For a cancelled or lapsed one it does not move: the
  stored end *is* the day access runs out, and rolling it would be renewing it
  for free.
* It is pure. It runs on the send path and writes nothing.
* `renew()` re-anchors the period, sets a real end date, and clears a
  cancellation or an arrears.
* A tenant governed by `BILLING_DEFAULT_PLAN` has **no subscription and so no
  anchor**, and falls back to the **calendar month**. Without that, "messages
  per month" would mean every message they have ever sent, and such a tenant
  would be blocked permanently the first time they crossed the limit rather
  than for the rest of one month. A tenant on no plan at all has no period,
  because their usage is not a limit on anything.

---

## Messages, credits and quota

A send needs **both**:

```
plan allowance available   AND   credit balance available
```

Credits never buy past the plan; plan room never sends without the credits to
pay for it. Both are checked at campaign launch (allowance first, because "your
plan allows 1,000 messages a month" is the more actionable sentence) and the
send gate re-checks the allowance per message.

The credit ledger is untouched. `reserve` / `consume` / `release` and their row
locking work exactly as before; the allowance claim sits **beside** them, is
taken at the same moment, and is released in the same three places.

---

## Failing closed

`check_quota`, `feature_enabled` and `claim_message_allowance` all refuse when
they cannot get an answer. A lookup that raises cannot say the resource is
free, and handing out a paid resource on the strength of an exception is
handing out the product. The refusal says *try again*, not *upgrade* — nothing
is known about the limit.

A tenant genuinely on **no plan** is still allowed. That is a known answer, not
an unknown one.

**Display is not the security boundary and does not fail closed.**
`get_entitlements` catches a counting failure, reports `usage_available: false`
and returns everything else. A dashboard panel that cannot count contacts is a
missing panel, not an outage.

---

## Tenants with no subscription

Most of this product's history has no subscriptions in it, and Phase 2 must not
break those tenants.

| Tenant kind | No subscription |
| --- | --- |
| `PLATFORM` | Unlimited, every feature. Superadmin operations are never subject to customer limits. |
| `RESELLER` | Unlimited, every feature. A reseller with no subscription is running the shop, not shopping in it. A reseller a superadmin *has* put on a plan is governed by it like anybody else. |
| `CUSTOMER` | Governed by `BILLING_DEFAULT_PLAN` if that names a real platform-owned plan; otherwise **unlimited**, which is today's behaviour. |

`BILLING_DEFAULT_PLAN` is unset by default. A name matching no plan falls back
to unlimited rather than guessing — accidentally handing every customer the
Enterprise plan is a worse failure than handing them the status quo. A
reseller-owned plan is never used as the platform default.

To write real subscriptions for tenants that have none:

```
python manage.py assign_default_plan --plan Starter --dry-run
python manage.py assign_default_plan --plan Starter
```

Customers only. Never touches a tenant that already has a subscription, never
creates a plan, never picks one for you.

---

## Downgrades

Moving a tenant to a smaller plan **never deletes anything**.

```
used       = 8,000
limit      = 5,000
over_limit = true
```

They keep all 8,000 contacts. They cannot add an 8,001st until usage is under
the limit or the plan is upgraded. Same for devices and users. The usage
payload reports `over_limit` per metric and in a top-level list, so the UI can
say so plainly instead of showing a bar at an impossible 160%.

---

## Reseller and superadmin

Unchanged, and deliberately so — the audit found tenant isolation strong.

* A reseller sees, prices and assigns only plans they own
  (`Plan.owner`), and only to tenants directly below them. Checked in
  `SubscriptionViewSet._may_manage` on create **and** update, against the
  request rather than trusted from the payload.
* A customer cannot put themselves on a different plan: `IsReseller` refuses
  every write from an ordinary user.
* `GET /billing/usage/<tenant_id>/` answers **404** for a tenant that is not
  below the caller — not 403, because a refusal that distinguishes the two is
  an id oracle.
* Superadmins keep managing plans, subscriptions, credits and customers, and
  are never subject to a customer plan's limits.

---

## Errors

Entitlement refusals are DRF exceptions, so they render in the shape the
frontend already reads (`apiErrorMessage` reads `detail`), plus machine-readable
fields merged in by `core/exceptions.api_exception_handler`.

```json
{
  "detail": "AI Agent is not included in your Starter plan. Upgrade your plan to unlock it.",
  "code": "FEATURE_NOT_AVAILABLE",
  "feature": "ai_agent",
  "request_id": "…"
}
```

```json
{
  "detail": "The Starter plan allows 500 contacts, and you have 500. Upgrade your plan to raise the limit.",
  "code": "QUOTA_EXCEEDED",
  "quota": "contacts",
  "request_id": "…"
}
```

**403**, not 402: there is no payment gateway, nothing is being asked for money
at this moment, and the accurate statement is "this account may not do that".
A billing lookup that failed answers **503** with `ENTITLEMENT_UNAVAILABLE` and
"please try again" — not the customer's fault, and not something to upgrade
over.

No refusal exposes a stack trace, a query, a model name or an internal id.

---

## Frontend

Presentation only. It decides what is *shown*, never what is allowed; a copy of
the app with these checks edited out gets exactly the same 403.

* `context/EntitlementsContext.jsx` — fetches `/billing/usage/` once and shares
  it. An unknown feature reads as *available*, so a half-second of loading does
  not flash an upgrade prompt on every screen.
* `components/billing/PlanUsage.jsx` — messages, contacts, devices and team
  members as meters, on the dashboard. Warnings at 80%, 90% and 100%.
* `components/billing/FeatureLock.jsx` — the locked state. Shown, not hidden:
  somebody who cannot see that a feature exists will never ask for it. Plan
  names come from the API and are **never hardcoded** — "Pro" and "Enterprise"
  are a reseller's words, and every reseller here picks their own.

---

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `BILLING_PAST_DUE_GRACE_DAYS` | `14` | How long a `PAST_DUE` subscription keeps working past its period end. `0` disables the grace. |
| `BILLING_DEFAULT_PLAN` | `""` | Platform-owned plan name governing customer tenants with no subscription. Unset means unlimited. |

---

## Deliberately deferred

Payment gateway, self-service signup, free-trial automation, invoices,
dunning, the public API implementation, team invites, role permissions, Meta
template sync.

Plan and subscription assignment continues through the existing
admin/reseller mechanism. This phase is `PLAN → ENTITLEMENTS → ENFORCEMENT`,
not `PAYMENT → SUBSCRIPTION`.

---

## Note: audit logging

`apps/audit/services.log_event` currently returns immediately and writes
nothing — it was short-circuited before this phase and is unchanged by it.
Entitlement events (`subscription.plan_assigned`, `subscription.plan_changed`,
`subscription.cancelled`, `subscription.renewed`) are recorded through that
same helper, so they will start appearing the moment it is re-enabled. Nothing
in Phase 2 depends on them.
