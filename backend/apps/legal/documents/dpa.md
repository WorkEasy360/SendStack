# Data Processing Agreement

**Version {{ version }}** · Last updated {{ updated }}

> **INCOMPLETE DRAFT — NOT A BINDING AGREEMENT.** A DPA is a contract between
> two named legal entities. This deployment does not yet have the information
> needed to complete one, and inventing it would be worse than leaving it
> blank. Every `[TO BE COMPLETED]` below is a real gap, not boilerplate.
>
> This document has **not** been reviewed by a qualified lawyer. Do not present
> it to a customer as an executed DPA.

---

## Why this exists

When a business customer uses this service to message their own customers,
personal data belonging to those people passes through the service. Under the
data-protection law of several jurisdictions the customer is the **controller**
and the service operator is the **processor**, and the two must have a written
agreement covering the processing. This is the skeleton of that agreement.

## 1. Parties

| Role | Party |
| --- | --- |
| Controller | The customer identified in the account |
| Processor | {{ legal_entity_name }} |
| Processor's registered address | {{ legal_entity_address }} |
| Processor's data-protection contact | {{ legal_contact_email }} |

`[TO BE COMPLETED — the processor's legal entity name, company number,
registered address and the jurisdiction it is incorporated in. Until these
exist, this agreement identifies nobody and binds nobody.]`

## 2. Subject matter and duration

**Subject matter:** provision of the {{ product_name }} business messaging
platform.

**Duration:** for as long as the controller's account is active, plus the
retention period in section 8.

## 3. Nature and purpose of the processing

Storing, organising, transmitting, retrieving and deleting personal data in
order to:

- deliver and receive WhatsApp messages on the controller's instruction;
- maintain the controller's contact and CRM records;
- run campaigns, automations and workflows the controller configures;
- where enabled by the controller, generate AI replies and place or receive
  voice calls;
- record consent and opt-out decisions.

## 4. Categories of data subject

- The controller's contacts and prospects.
- People who message the controller's WhatsApp numbers.
- The controller's own team members who use the service.

## 5. Categories of personal data

Phone numbers; names; email addresses; company names; country; message content,
including anything a data subject chooses to write; media sent or received;
consent and opt-out state with its evidence; link-click events; CRM records
including notes written by the controller's staff; where enabled, voice-call
records and transcripts.

**Special-category data:** the service is not designed for it and the operator
does not intend to process it. A controller who uses free-text fields or message
content to record health, biometric, religious, political or similar data is
doing so on its own responsibility and must have its own lawful basis.

## 6. Sub-processors

The controller gives general authorisation for the sub-processors below. The
processor will give notice before adding or replacing one.

| Sub-processor | Purpose | Location |
| --- | --- | --- |
| Meta Platforms, Inc. (WhatsApp) | Message delivery | `[TO BE COMPLETED]` |
| `[TO BE COMPLETED — hosting provider]` | Hosting, database, object storage | `[TO BE COMPLETED — region]` |
| `[TO BE COMPLETED — email provider]` | Transactional account email | `[TO BE COMPLETED]` |
| `[TO BE COMPLETED — AI provider: Anthropic and/or OpenAI]` | AI replies, where enabled | `[TO BE COMPLETED]` |
| `[TO BE COMPLETED — telephony provider]` | Voice agent, where enabled | `[TO BE COMPLETED]` |
| `[TO BE COMPLETED — error-monitoring provider]` | Scrubbed error diagnostics, where enabled | `[TO BE COMPLETED]` |

`[TO BE COMPLETED — the notice period for a sub-processor change, and whether
the controller may object.]`

## 7. Processor obligations

The processor will:

1. process personal data only on the controller's documented instructions,
   which include the controller's use of the product's own features;
2. ensure people authorised to process the data are bound by confidentiality;
3. implement the technical and organisational measures in section 9;
4. assist the controller, so far as the product allows, with data-subject
   requests — the product provides contact export, edit, delete, consent
   history and permanent opt-out;
5. assist the controller with security, breach notification and impact
   assessments, taking account of the information available to it;
6. delete or return personal data at the end of the service, per section 8;
7. make available the information needed to demonstrate compliance with this
   agreement.

## 8. Deletion and return

On termination, and on the controller's written request, the processor will
delete the controller's data.

**What deletion actually means here, stated honestly:**

- Live database records for the workspace are deleted.
- Uploaded media files for the workspace are deleted.
- **Consent and opt-out records are retained**, because they are the evidence
  that an opt-out was honoured, and destroying them would destroy the
  controller's own defence. `[TO BE COMPLETED — retention period.]`
- **Backups are not selectively edited.** A backup taken before the deletion
  still contains the data until that backup is rotated out of the retention
  window. `[TO BE COMPLETED — backup retention window.]`
- Audit records of who did what are retained. `[TO BE COMPLETED — period.]`

`[TO BE COMPLETED — the export window after termination.]`

## 9. Technical and organisational measures

Implemented today (each of these is a real control in the application, not an
aspiration):

- **Access control** — every query scoped to a single workspace; role-based
  permissions; a reseller can reach only its own customers.
- **Authentication** — Argon2 password hashing; 15-minute access tokens;
  refresh tokens revocable server-side and revoked on sign-out and password
  change; per-account lockout after repeated failures; rate limiting on
  authentication endpoints.
- **Encryption in transit** — HTTPS enforced with HSTS.
- **Encryption at rest for credentials** — WhatsApp provider credentials
  encrypted with a key held only in the deployment's environment.
- **Upload safety** — files validated by signature rather than filename, stored
  under generated names, and served with headers that prevent execution.
- **Webhook integrity** — inbound provider webhooks verified against the
  provider's signature.
- **Network isolation** — database, cache and the WhatsApp bridge are not
  published to the internet.
- **Logging and monitoring** — structured logs with correlation IDs; optional
  error monitoring with credentials, message bodies and email addresses removed
  and phone numbers masked before an event leaves the server.
- **Backups** — automated, timestamped, encrypted where a passphrase is
  configured, with a documented and tested restore procedure.

`[TO BE COMPLETED — encryption at rest for the database volume as a whole;
personnel screening; secure development practices; penetration testing; and any
certification (ISO 27001, SOC 2). None of these are claimed today.]`

## 10. Personal data breach

The processor will notify the controller without undue delay after becoming
aware of a personal data breach affecting the controller's data, and will
provide the information available to it.

`[TO BE COMPLETED — the notification deadline the processor commits to, e.g. 48
or 72 hours, and the contact channel.]`

The processor's internal procedure is documented in its incident-response
runbook.

## 11. International transfers

`[TO BE COMPLETED — where the data is hosted, whether any transfer leaves that
jurisdiction, and the transfer mechanism relied on (Standard Contractual
Clauses, adequacy decision, or other). This cannot be answered until the
hosting region and the sub-processor list above are decided.]`

## 12. Audit

`[TO BE COMPLETED — what audit rights the controller has, how much notice is
required, how often, and who bears the cost.]`

## 13. Liability

Liability under this agreement is subject to the limitations in the
[Terms of Service](/terms).

## 14. Signature

`[TO BE COMPLETED — how this agreement is executed. It is currently presented
as a document only and is not countersigned by either party.]`
