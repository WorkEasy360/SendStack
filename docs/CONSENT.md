# Consent and opt-out

How this product decides whether a person may be messaged, and why it is built
the way it is.

---

## Two different questions

The single most common mistake in a messaging platform is treating these as one
field:

| Field | Question | Default | Set by |
| --- | --- | --- | --- |
| `Contact.subscribed` | **Have they told us to stop?** | `True` — somebody who has never spoken has not said stop | Replying STOP, tapping unsubscribe, a team member, a bulk action |
| `Contact.marketing_consent` | **Did they ever say yes, and where?** | `UNKNOWN` — never inferred | An explicit consent recording, or a declared source on an import |

`UNKNOWN` is the honest value for every contact that existed before this
feature and for every row of every CSV anybody has ever imported. **Possessing a
phone number is not evidence of consent**, and a platform that defaults this to
`GRANTED` is inventing consent on its customers' behalf — which is exactly the
thing that gets a WhatsApp number banned and a business fined.

---

## The precedence rule

```
explicit opt-out  >  marketing consent  >  campaign eligibility
```

An opt-out is authoritative. Nothing outranks it: not a later import claiming
consent, not a CRM sync, not a workflow, not the AI agent, not a plan that has
paid for the feature.

It lives in exactly one function —
`apps/compliance/services.py::may_receive_marketing` — called from exactly one
place on the send path, `apps/messaging/gating.py::evaluate`. That is the whole
design. A second sending feature added later inherits the rule by construction,
because it cannot send without going through the gate.

---

## Where consent can come from

`Contact.ConsentSource`, and nothing else. A source somebody can type freely is
a source nobody can audit.

`MANUAL` · `WEBSITE` · `FORM` · `CRM` · `API` · `CSV` · `CUSTOMER_PROVIDED` ·
`INBOUND` · `OTHER`

Alongside the source, a contact carries `consent_at` (when) and
`consent_reference` (which form, list or campaign — e.g.
`newsletter-signup-2026`). The reference is deliberately short and is **not** a
place to store a copy of somebody's submission: keeping raw personal data
"as evidence" collects more than the purpose needs.

The full history is in `compliance.ConsentEvent`, which is **append-only**.
Opting back in writes a new row; it never edits the old one. An opt-out that a
later opt-in erases is not an audit trail.

---

## Importing a list

The importer never asserts consent on the uploader's behalf.

- Upload **without** a consent source → every contact stays `UNKNOWN`. This is
  the default.
- Upload **with** a declared source and reference → each contact gets
  `GRANTED`, that source, a timestamp, and its own `ConsentEvent` row. One row
  per contact, because the source is per-contact evidence and a single summary
  row would not survive one of them being questioned.

**A declared source never clears an opt-out.** Someone who replied STOP last
week stays unsubscribed no matter what this week's spreadsheet says. That is
enforced in `set_marketing_consent`, not in the importer, so every caller gets
it.

For contacts imported before this existed, the workflow is: filter the contact
list by `marketing_consent = Not recorded`, decide where those contacts came
from, and record it in bulk (`grant_consent` with a source). If the answer is
"we do not know where they came from", the correct action is to leave them
`UNKNOWN` and not market to them.

---

## Turning on consent enforcement

`CompliancePolicy.require_marketing_consent`, per tenant, **default off**.

That default is a deliberate, documented decision rather than a soft one. Every
contact that exists today is `UNKNOWN`, so switching it on by default would stop
every existing customer's campaigns at the moment they deployed the upgrade.

With it **off**: an opt-out still blocks marketing. Consent is recorded and
visible but not required.

With it **on**: marketing additionally requires `GRANTED`. `UNKNOWN` is refused
— refusing is the only safe reading of "we do not know". Service replies to
somebody who just messaged in are **not** affected: requiring a marketing opt-in
before answering an inbound message would break the inbox.

Turning it on is a business decision each tenant makes once they have recorded
consent for the contacts they intend to market to.

---

## What the send gate actually checks

In order, in `apps/messaging/gating.py`, immediately before each send and never
earlier:

1. **Workspace active** — a suspended customer sends nothing.
2. **Opt-out** — blocks every non-notice message.
3. **Marketing consent** — marketing only, and only where the policy requires it.
4. **Quiet hours** — marketing only; holds rather than blocks.
5. **Plan allowance** — claimed transactionally, per message.
6. **Sender health and rate limits** — holds or blocks.

Every input is read live. A campaign launched on Monday and dispatched on Friday
is authorised against Friday's state, because a scheduled task must never trust
the authorization it was scheduled with.

`ALLOW` sends. `HOLD` keeps the message queued and asks again later — nobody
loses a recipient to a quiet window. `BLOCK` marks it `SKIPPED` with the reason
attached, and is reserved for decisions that will not reverse themselves.

---

## Paid features do not bypass any of this

A premium feature is not an authorization layer. Campaigns, workflows, the AI
agent, voice follow-up and scheduled sends all produce a `Message` and all hit
the same gate with the same answer. There is no code path that sends without it.

---

## Where to look

| | |
| --- | --- |
| The precedence rule | `apps/compliance/services.py::may_receive_marketing` |
| Recording consent | `apps/compliance/services.py::set_marketing_consent` |
| Opt-out and opt-in | `apps/compliance/services.py::opt_out`, `opt_in` |
| Inbound STOP/START handling | `apps/compliance/services.py::handle_inbound` |
| The send gate | `apps/messaging/gating.py::evaluate` |
| Consent fields | `apps/contacts/models.py::Contact` |
| Append-only history | `apps/compliance/models.py::ConsentEvent` |
| Tenant policy | `apps/compliance/models.py::CompliancePolicy` |
| Tests | `apps/compliance/tests_consent_evidence.py`, `apps/messaging/tests_gating_phase3.py` |
