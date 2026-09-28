# Privacy Policy

**Version {{ version }}** · Last updated {{ updated }}

> **DRAFT FOR BUSINESS AND LEGAL REVIEW.** This document was written by reading
> the application's own data model, so the categories below are the data the
> software actually stores rather than a generic list. It has **not** been
> reviewed by a qualified lawyer.
>
> **This deployment does not claim GDPR, UK GDPR, DPDP or any other compliance
> certification.** Publishing a privacy policy is not compliance. Fields marked
> `[TO BE COMPLETED]` require a decision by the operator.

**Data controller for account data:** {{ legal_entity_name }}
**Registered address:** {{ legal_entity_address }}
**Privacy contact:** {{ legal_contact_email }}

---

## 1. Two different roles

This matters, and it is the reason the rest of the document is split in two.

- **For your account** — your name, your login, your billing and your usage —
  we are the **controller**. We decide what to collect and why.
- **For the people you message** — your contacts, their phone numbers and your
  conversations with them — **you are the controller and we are the
  processor**. We hold and transmit that data on your instructions. You decide
  who is in your contact list, whether you have a lawful basis to message them,
  and how long the data is kept. See the [Data Processing
  Agreement](/dpa).

## 2. Data we hold as controller (your account)

| What | Why |
| --- | --- |
| Username, email address, mobile number, password hash | To create and secure your account |
| Workspace/tenant name, your role, your reseller if you have one | To scope your data and decide what you may do |
| Plan, subscription status, credit balance and ledger | To apply your entitlements and bill you |
| Usage counts (messages sent per period, contacts, devices, team members) | To enforce plan limits and show usage |
| Audit events: sign-ins, user administration, impersonation, consent changes, bulk actions | Security, and answering "who did this" |
| Which version of these documents you accepted, when, and the IP address and browser string at that moment | Evidence of acceptance |
| Business profile: what your business does, products, services, locations, opening hours, tone | To configure the AI features you enable |

We store a **hash** of your password, never the password. We never store
payment card details — this deployment has no payment integration.

## 3. Data we process on your behalf (your customers)

| What | Notes |
| --- | --- |
| Contact records: name, phone number, email, company, country, tags, groups, custom fields | Whatever you import or enter |
| Consent state: whether a contact is subscribed, when they opted out, and an append-only log of every consent decision with its source | This is evidence you may need |
| Conversations and message content, inbound and outbound, with delivery and read status | |
| Media you or your contacts send: images, video, audio, documents | Stored as files, with metadata |
| Campaign definitions, audiences, per-recipient results and failures | |
| CRM records: deals, pipeline stages, tasks, notes, lead scores, lifecycle status | |
| Automation rules, keyword replies and workflow runs | |
| Link-click events on tracked links: the click, its time, and a coarse client description | Only for links the service rewrote |
| AI conversation state and, where you enable the knowledge base, the documents you upload | |
| Voice-agent call records, and transcripts or recordings where you enable them | |

We do not sell this data, use it to build profiles across customers, or use it
to train AI models.

## 4. Technical and log data

- **Application logs.** Timestamped operational records: which request, which
  workspace, what operation, what result, and a correlation ID for tracing a
  request across the API and the background workers. Logs are written to
  standard output and captured by the container platform.
- **Error reports.** Where the operator enables error monitoring, an unhandled
  exception is reported to the monitoring provider. Before it leaves the
  server, the payload is scrubbed: passwords, tokens, API keys, session data,
  signatures and encryption keys are removed by key name, message bodies and
  email addresses are removed, and phone numbers are masked (`+91******3210`).
- **Web server logs** kept by the reverse proxy, including IP address, request
  path and status.

## 5. Cookies

The application is a single-page app that authenticates with tokens held in
your browser's local storage, not with cookies. It sets **no analytics,
advertising or tracking cookies**.

The Django administration interface, used by operators, uses a session cookie
and a CSRF cookie. Both are strictly necessary.

## 6. AI processing

AI features are **optional and off unless you enable them**.

When you enable an AI feature and a message needs answering, we send the
following to the configured AI provider (Anthropic or OpenAI, depending on the
deployment's configuration):

- the recent messages in that one conversation;
- your business profile, as you wrote it;
- a bounded summary of that one contact, which may include their name, their
  lifecycle status and their recent activity;
- excerpts from your knowledge-base documents that the retrieval step judged
  relevant to the question.

We deliberately do **not** send your contact list, your other conversations,
other contacts' data, credentials, or anything belonging to another workspace.

The AI provider's own retention applies to what we send. `[TO BE COMPLETED —
state the provider's retention and whether a zero-retention agreement is in
place.]` API keys are held server-side only and are never sent to the browser.

## 7. Voice agent

Where you enable the voice agent, calls are placed and received through a
telephony provider. Call metadata, and transcripts or recordings where you
enable them, are stored against your workspace. Recording a call may require
the consent of the other party under the law that applies to you; that
assessment is yours.

## 8. Sub-processors and third parties

| Provider | Purpose | Data reaching them |
| --- | --- | --- |
| Meta Platforms (WhatsApp) | Message delivery | Recipient number, message content, media |
| `[TO BE COMPLETED — hosting provider]` | Hosting, database, storage | All of the above at rest |
| `[TO BE COMPLETED — email provider]` | Account email (password reset, security notices) | Your account email address |
| Anthropic and/or OpenAI | AI features, where enabled | As described in section 6 |
| `[TO BE COMPLETED — telephony provider]` | Voice agent, where enabled | Call audio and metadata |
| `[TO BE COMPLETED — error monitoring provider]` | Error reports, where enabled | Scrubbed diagnostics per section 4 |

`[TO BE COMPLETED — the hosting region, and whether any international transfer
takes place and on what legal basis.]`

## 9. Retention

Retention is described in full in the operator's retention policy. In summary:

| Data | Retained |
| --- | --- |
| Account and workspace records | For the life of the account |
| Consent records (opt-ins and opt-outs) | Kept even when the contact is deleted, because they are the evidence that the opt-out was honoured |
| Messages, conversations, campaigns, CRM records | For the life of the account unless you delete them |
| Uploaded media | For the life of the account unless you delete it |
| Audit events | `[TO BE COMPLETED — business/legal decision]` |
| Application logs | As retained by the container platform — `[TO BE COMPLETED]` |
| Database backups | Per the operator's backup retention — `[TO BE COMPLETED]` |
| Data after account closure | `[TO BE COMPLETED]` |

**Backups are honest here:** deleting a record removes it from the live
database, but a backup taken before the deletion still contains it until that
backup is rotated out. We do not claim otherwise.

## 10. Security

The measures actually implemented in this application:

- passwords hashed with Argon2;
- short-lived access tokens with server-side revocation of refresh tokens on
  sign-out and on password change;
- per-account lockout after repeated failed sign-ins, and rate limiting on
  authentication endpoints;
- every query scoped to one workspace, enforced in the application layer;
- WhatsApp provider credentials encrypted at rest;
- uploads validated by file signature rather than by filename, and served with
  headers that stop a browser executing them;
- webhook payloads verified against the provider's signature;
- HTTPS enforced, with HSTS and a restrictive set of security headers;
- internal services (database, cache, WhatsApp bridge) not published to the
  internet.

No system is perfectly secure, and we do not claim this one is.

## 11. Your rights

Depending on where you are, you may have rights to access, correct, delete,
export or restrict the processing of your personal data, and to object to it.

- **For your account data**, contact {{ legal_contact_email }}.
- **For data about somebody you have messaged**, the request belongs to you as
  the controller — the application gives you export, edit and delete tools for
  contacts, and a permanent opt-out that we will not let you overwrite by
  accident.

`[TO BE COMPLETED — the response time the operator commits to, and the
supervisory authority a complaint may be made to.]`

## 12. Children

The service is for business use and is not directed at children.

## 13. Changes

We may change this policy. When we do, we increase the version number of this
document, and where the change is material we will ask you to accept the new
version.

## 14. Contact

{{ legal_contact_email }}
