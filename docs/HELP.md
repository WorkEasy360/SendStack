# Help centre

Customer-facing documentation for SendStack. Written for somebody using the
product, not somebody building it.

Everything here describes **what the application actually does**. Where a thing
is not supported, it says so rather than describing what a similar product
would do.

**Contents**

- [Getting started](#getting-started) — the shortest path to a first campaign
- [Connecting WhatsApp](#connecting-whatsapp)
- [Contacts](#contacts)
- [Consent and opt-outs](#consent-and-opt-outs)
- [Templates](#templates)
- [Campaigns](#campaigns)
- [Scheduling](#scheduling)
- [Credits and plan limits](#credits-and-plan-limits)
- [Billing](#billing)
- [Troubleshooting](#troubleshooting)
- [API](#api)

---

## Getting started

Seven steps from a new account to results you can read. Nothing here can be
skipped, and none of it needs a developer.

### 1. Create your account

Sign up, and accept the terms when asked. Your workspace is created empty.

### 2. Connect WhatsApp

**Devices → Add a number.** Two ways to connect, and you can use both:

- **Scan a QR code** with the phone that already has the number on it. Fastest
  to set up, and it uses your existing WhatsApp.
- **WhatsApp Business API** if you have an official Meta account. Supports more
  message types and higher volumes.

Wait until the number shows **Connected**. Campaigns cannot send without one.

### 3. Add contacts

**Contacts → Import**, with a CSV, or add them one at a time.

Your CSV needs a phone number column. Numbers should be in international
format (`+919876543210`); numbers without a country code are rejected rather
than guessed at, because a guess sends somebody else's customer a message.

Anything else in the file — name, city, order number — can be imported as a
custom field and used in your messages.

### 4. Record consent

If your workspace requires recorded opt-in (**Compliance → require marketing
consent**), a contact with no consent record is never messaged. Set consent on
import, individually, or by having contacts reply to a message.

**This is the step that is worth not skipping.** Consent is what keeps your
number from being reported, and complaints are what get numbers restricted.

### 5. Create a campaign

**Campaigns → New campaign.**

- **Basics** — name it, and pick the number to send from.
- **Audience** — everyone, a group, a tag, or a saved segment.
- **Message** — write it, or reuse a template. Use `{{name}}` and your own
  fields to personalise. Attach an image, a document, or buttons.
- **Schedule** — send now, or pick a time.

It is saved as a **draft**. Nothing has been sent.

### 6. Review eligibility

Open the draft. Before the Launch button you will see:

| | |
|---|---|
| **Contacts selected** | what your audience rule matched |
| **Eligible to receive** | who will actually be messaged |
| **Opted out — skipped** | asked not to hear from you. Never contacted, never charged |
| **No consent — skipped** | your policy requires opt-in and theirs is not recorded |
| **Credits this will use** | one per eligible recipient |

If anything would stop the send — no connected number, not enough credits,
over your plan's monthly allowance — it is listed here with what to do about it.

These numbers come from the same rules that run when you press Launch, so what
you see is what happens.

### 7. Send, and read the results

Press **Launch**. Messages go out at a paced rate rather than all at once.

The campaign page then shows **sent**, **delivered**, **read**, **failed** and
**skipped**, with failures grouped by reason. Delivery and read receipts appear
where the provider reports them.

---

## Connecting WhatsApp

### QR code

**Devices → Add a number → Scan QR.** Open WhatsApp on the phone → Linked
devices → Link a device → scan.

Keep the phone online. This connection works through your phone, so a phone
that is off or out of signal stops the number sending.

### WhatsApp Business API

Requires an approved Meta WhatsApp Business account. You will need your phone
number ID and an access token from Meta.

### Device states

| State | What it means |
|---|---|
| **Connected** | Ready to send |
| **Connecting** / **QR ready** | Setting up, waiting to be scanned |
| **Reconnecting** | Temporarily dropped, trying again by itself |
| **Disconnected** | Not sending. Campaigns using it will not go out |
| **Expired** | The link was ended. Scan again |

**A disconnected number does not fail a campaign silently.** Messages that
cannot be sent are marked skipped with the reason, not marked sent.

### Buttons

Tappable buttons work on WhatsApp Business API numbers. On QR-connected
numbers, WhatsApp does not render them, so they are automatically shown as a
numbered list instead and replies are matched back to the right option. You do
not have to do anything; the campaign works either way.

---

## Contacts

- **Groups** — a contact can be in several. Good for batches, branches, courses.
- **Tags** — lighter than groups, good for "regulars", "vip", "lapsed".
- **Custom fields** — anything from your CSV. Use in messages as `{{field}}`.
- **Segments** — saved filters that stay up to date, e.g. "opted in, tagged
  regulars, added in the last 90 days".
- **Lifecycle** — where a contact is in your relationship. Separate from a deal
  stage in the pipeline.

Contacts are unique by phone number within your workspace. Importing the same
number twice updates the existing contact rather than creating a duplicate.

---

## Consent and opt-outs

The rule the product enforces, in this order:

```
opt-out   >   consent   >   eligibility
```

**An opt-out always wins.** Nothing overrides it: not a later import claiming
consent, not a CRM sync, not an automation, not the AI agent, not a plan that
has paid for the feature.

- A contact who replies **STOP** is opted out immediately and automatically.
- Opted-out contacts are excluded from every campaign, and are shown in the
  pre-send review so you can see it is working.
- **Compliance → require marketing consent** makes recorded opt-in mandatory.
  With it on, a contact whose consent was never recorded is not messaged —
  "we do not know" is treated as no.

Every consent change is recorded with when it happened and where it came from.

---

## Templates

Save a message you send often. Templates hold the text, any attachment, and
any buttons.

Personalise with `{{name}}` or any custom field. A contact with no value for a
field gets the fallback you set, never an empty gap or a literal `{{name}}`.

Templates here are **your own saved messages**. They are not Meta's approved
message templates, and this product does not submit templates to Meta for
approval.

---

## Campaigns

### Audience

Everyone, a group, a tag, a saved segment, or a specific list of contacts.
Opted-out contacts are excluded whichever you choose.

### Pacing

Messages are sent with a randomised delay between them rather than all at
once. This is deliberate and not configurable to zero: a burst of identical
messages from one number is the pattern most likely to get it restricted.

### Statuses

| Status | Meaning |
|---|---|
| **Draft** | Created, not sent. Nothing charged |
| **Scheduled** | Waiting for its time |
| **Running** | Sending now |
| **Completed** | Finished. Read the results |
| **Paused** | Stopped part-way. Can be resumed |

### Results

**Sent** reached WhatsApp. **Delivered** and **Read** are WhatsApp's receipts,
where the provider reports them. **Failed** was attempted and refused.
**Skipped** was never attempted — no eligible recipient, no connected number,
or refused by the consent rules.

Skipped is not failed, and skipped messages are not charged for.

---

## Scheduling

Pick a date and time when creating a campaign. Scheduled campaigns survive a
restart of the system — they are stored in the database, not held in memory.

A scheduled campaign whose number is disconnected at its time does not send
messages into nothing; it reports the device as the reason.

---

## Credits and plan limits

Two separate things, and both must allow a send:

- **Credits** are consumed one per message. They are reserved when a campaign
  starts and released for any message that was not sent.
- **Plan limits** cap messages per month, contacts, connected numbers and team
  members. `Unlimited` means no cap on that plan.

**Usage** on your billing page shows both, with what you have used against
each. Warnings appear as you approach a limit rather than at the moment it
stops you.

Going over a limit **never deletes anything**. It stops you adding more until
you are back under it or you upgrade.

---

## Billing

**Billing** shows your plan, your usage, your billing period, and your payment
history.

- **Upgrade** — you are shown the new price, the difference, the new limits and
  when it takes effect, before you pay. Your new plan starts as soon as the
  payment completes.
- **Downgrade** — you are shown which limits drop and, if you are already over
  one of them, by how much. **Nothing is deleted.** You keep everything you
  have; you cannot add more until you are under the new limit.
- **Cancel** — you keep full access until the end of the period you have paid
  for. Your data stays where it is.

Card details are typed on the payment provider's own page and never reach us.

Payment receipts are issued by the payment provider. This deployment does not
generate tax invoices.

---

## Troubleshooting

### My campaign did not send

Check, in this order:

1. **Is a number connected?** Devices. A disconnected number sends nothing.
2. **Is anyone eligible?** Open the campaign — the review panel shows the
   split. An audience that is entirely opted-out or missing consent has nobody
   to send to.
3. **Enough credits?** The review panel says what the send needs and what you
   have.
4. **Within your plan's monthly allowance?** Also on the review panel.
5. **Is the subscription active?** An expired subscription pauses sending.
   Nothing is deleted.

### Messages say "failed"

Open the campaign and read the failure reasons. The common ones:

| Reason | What to do |
|---|---|
| Invalid recipient | The number is not on WhatsApp, or is malformed. Fix the contact |
| Device disconnected | Reconnect the number and retry the failed messages |
| Rate limited | The provider is asking you to slow down. It will retry |
| Insufficient credits | Top up, then retry |
| Contact opted out | Working as intended. Not an error |
| No marketing consent | Record consent, or turn the requirement off |

### My number disconnected

For QR-connected numbers: the phone went offline, WhatsApp was reinstalled, or
the linked device was removed. Reconnect by scanning again.

Running campaigns pause rather than marking messages as sent.

### My scheduled campaign did not run on time

Scheduled jobs are durable and survive restarts. A delay usually means the
background workers are behind. If it is more than a few minutes, raise a
support request — the delay itself is recorded and we can see it.

### Trial ended / subscription expired

Sending stops. **Everything else stays exactly where it is** — contacts,
campaigns, templates, history. Choose a plan to start sending again.

### I am being rate limited by the API

The API allows a set number of requests a minute per user. If you are building
an integration, spread requests out; a `429` response tells you to wait.

---

## API

Interactive documentation, generated from the running application, is at
`/api/docs/`. The OpenAPI schema is at `/api/schema/`.

### Authentication

```
POST /api/auth/token/     {"username": "...", "password": "..."}
   → {"access": "...", "refresh": "..."}

POST /api/auth/token/refresh/   {"refresh": "..."}
```

Send the access token as `Authorization: Bearer <access>`. Access tokens are
short-lived; refresh rather than re-authenticating.

API access is a plan feature. If your plan does not include it, requests are
refused with an explanation — the server enforces this, so it cannot be worked
around from a client.

### Conventions

- Base path `/api/v1/`.
- All responses JSON.
- List endpoints paginate: `?page=`, `?page_size=`. The response carries
  `count`, `next`, `previous`, `results`.
- Errors return an appropriate status with `{"detail": "..."}`.
- Every response carries an `X-Request-ID` header. **Quote it when contacting
  support** — it is how a request is traced through to the work it became.

### Rate limits

Per authenticated user, refreshed every minute. Exceeding one returns `429`
with a `Retry-After` header. Authentication endpoints are limited more tightly,
per address, to make credential stuffing impractical.

### Webhooks

Incoming WhatsApp messages and status updates arrive at the provider webhook
endpoints and are verified by signature before anything in them is read.

---

## Getting help

**Support** in the app. Tell us what you were doing, what you expected, and
what happened.

Your request automatically carries a snapshot of your workspace — your plan,
your usage, whether your numbers are connected, and how many messages failed
recently — so we do not have to ask.

**We never attach your contacts, their phone numbers, or the content of any
message you have sent.**

You will get a reference like `K7M2P9QX`. Quote it in any follow-up.
