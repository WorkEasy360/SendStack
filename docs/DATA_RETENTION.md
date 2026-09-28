# Data retention

What this deployment keeps, for how long, and why.

**Most rows in the table below say `[BUSINESS DECISION]`.** That is deliberate.
Retention periods are business and legal decisions, not technical ones, and
inventing a number and calling it a requirement would be worse than leaving the
gap visible. Where a period *is* fixed by how the software works, it says so.

---

## The principle

Two rules decide everything below:

1. **Data a customer put in is theirs, and is kept until they remove it.** The
   application does not expire contacts, messages or campaigns on its own.
2. **Evidence that a rule was followed outlives the data the rule was about.**
   Consent records, audit events and financial records survive the deletion of
   what they describe. Deleting an opt-out record along with the contact would
   destroy the customer's own defence against a complaint about messages
   already sent.

---

## What is retained

| Data | Where | Retention today | Decision needed |
| --- | --- | --- | --- |
| Account (username, email, password hash) | `accounts_user` | Life of the account | `[BUSINESS DECISION]` after closure |
| Workspace, plan, subscription | `tenants_tenant`, `subscriptions_*` | Life of the account, then retained inactive | — |
| **Contacts** | `contacts_contact` | Until the customer deletes them | `[BUSINESS DECISION]` |
| **Consent events** (opt-in / opt-out) | `compliance_consentevent` | **Kept even when the contact is deleted** — they are the evidence the opt-out was honoured | `[BUSINESS DECISION]` on an outer bound |
| Messages and conversations | `messaging_message`, `inbox_conversation` | Until deleted with the workspace | `[BUSINESS DECISION]` |
| Campaigns and per-recipient results | `campaigns_*` | Until deleted with the workspace | `[BUSINESS DECISION]` |
| Uploaded media | `MEDIA_ROOT` + `media_library_mediaasset` | Until deleted | `[BUSINESS DECISION]` |
| CRM (deals, tasks, notes) | `crm_*` | Until deleted | `[BUSINESS DECISION]` |
| **Audit events** | `audit_auditevent` | Never pruned by the application | `[BUSINESS DECISION]` — pruning removes the record of who did what |
| **Credit ledger** | `credits_*` | Never pruned — financial record | `[BUSINESS DECISION]`, likely a statutory period |
| **Legal acceptances** | `legal_legalacceptance` | Never pruned — append-only | `[BUSINESS DECISION]` |
| Link-click events | `tracking_*` | Until deleted with the workspace | `[BUSINESS DECISION]` |
| Voice call records and transcripts | `voice_*` | Until deleted | `[BUSINESS DECISION]` — recordings are sensitive |
| AI conversation state, knowledge documents | `ai_*` | Until deleted | `[BUSINESS DECISION]` |
| Impersonation sessions | `accounts_impersonationsession` | Never pruned — security record | `[BUSINESS DECISION]` |
| **Application logs** | Container stdout | Whatever the container platform keeps | `[BUSINESS DECISION]` — configure log rotation |
| **Error reports** | Monitoring provider, if enabled | The provider's own retention | `[BUSINESS DECISION]` |
| **Database backups** | `BACKUP_DIR` | **14 days** (`BACKUP_RETENTION_DAYS`) | Confirm; this one *is* set in software |
| Redis (cache, queue, counters) | Redis | Transient. Cache TTLs, lockouts 15 min, queue until executed | — |
| **Provider webhook events** | `whatsapp_whatsappwebhookevent` | **30 days** (`RETENTION_WEBHOOK_EVENT_DAYS`) — enforced | Confirm; this one *is* set in software |
| **Payment webhook payloads** | `billing_webhookevent.payload` | **90 days** (`BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS`) — enforced. The event row itself is never deleted | Confirm |
| Finished scheduled jobs | `scheduling_scheduledjob` | **30 days** (`RETENTION_SCHEDULED_JOB_DAYS`) for COMPLETED/CANCELLED. FAILED jobs are kept | Confirm |
| Expired numbered-list prompts | `messaging_pendingbuttonprompt` | **7 days** (`RETENTION_BUTTON_PROMPT_DAYS`) — enforced | Confirm |
| Expired refresh tokens | `token_blacklist_outstandingtoken` | Removed once past their own expiry — enforced | — |

---

## How retention is enforced

Until Phase 5 this document described a policy that **nothing implemented**.
Every table named above grew for ever, and
`BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS` — a setting that says ninety days in
so many words — was read by no code at all.

It is now enforced by `apps/health/retention.py`, run daily by
`apps.health.tasks.run_retention_sweep` on the `maintenance` queue.

```bash
python manage.py prune_expired_data --dry-run    # counts; deletes nothing
python manage.py prune_expired_data              # runs every rule
python manage.py prune_expired_data --rule whatsapp_webhook_events
```

Run `--dry-run` first on a deployment that has been accumulating for a year.
The numbers are usually larger than anybody expects: a provider sends a webhook
per *status transition*, so the webhook table grows at roughly three rows per
outbound message, each carrying the provider's whole JSON body.

**Every horizon is measured in days, and `0` disables that rule entirely** —
the same "0 means unlimited" convention the plan ceilings use, and the reading
that fails safe if somebody sets one by mistake.

Deletion is batched (5,000 rows per statement, 200,000 per pass) so that
clearing a year's backlog cannot hold a lock long enough to stall the send
pipeline behind it. What one pass does not finish, the next picks up.

### What it will never touch

There is deliberately **no rule** for customer business data, consent events,
audit events, impersonation sessions, the credit ledger, payments, checkouts or
legal acceptances. Not a rule that defaults to off — no rule at all, which is
the stronger guarantee: a switch can be flipped by accident, and code that does
not exist cannot be.

`apps/health/tests_retention.py` asserts this directly. It builds a workspace
holding one row of every protected kind, backdates them all ten years past every
horizon in the module, runs a full sweep, and fails if any of them is gone.

---

## What is in logs, and what is not

Production logs are shipped, aggregated and searched by more people than the
database is, so they are treated as a lower-trust destination.

**Never logged, anywhere:** passwords, password hashes, JWTs, refresh tokens,
password-reset tokens, API keys, WhatsApp access tokens, the encryption key,
webhook secrets.

**Masked in production** (`LOG_MASK_PHONE_NUMBERS=true`, the default there):
phone numbers, as `+91******3210`. Enough to tell two recipients apart in a
diagnosis; not enough to dial one or build a list.

**Logged deliberately:** timestamp, log level, correlation id, logger name,
tenant id, message and campaign ids, operation and outcome, error type.

**Message bodies are not logged.** Campaign log lines carry ids and outcomes,
never content.

---

## Deletion — what it actually means

### A contact
`DELETE /api/v1/contacts/contacts/{id}/` removes the contact row. Its consent
events are retained: the fact that this number opted out survives the record of
the number, because a re-import must not silently resurrect somebody, and
because it is the evidence the opt-out was honoured.

### A workspace
`manage.py delete_tenant <slug> --confirm <slug>`. Requires the slug to be
typed twice, refuses a workspace with children, refuses the platform tenant, and
prints what it will do before doing it (`--dry-run`).

**Deleted:** contacts, groups, tags, segments, imports, messages,
conversations, campaigns, templates, media assets, WhatsApp accounts and their
stored (encrypted) credentials.

**Retained, and the command says so as it runs:** consent events, audit events,
credit ledger entries, legal acceptances. The tenant row itself is kept and
marked inactive, because those retained records point at it.

### Backups — the honest part
**Backups are not selectively edited.** Data deleted today is still present in
every backup taken before today, until that backup rotates out of the retention
window (14 days by default).

So a deletion is complete in the live system immediately, and complete
everywhere after the backup retention window has passed. Any statement to a
customer that says otherwise is untrue, and the Privacy Policy and DPA both say
this explicitly rather than implying instant erasure.

---

## Backups

| | |
| --- | --- |
| Frequency | Whatever `scripts/backup_postgres.sh` is scheduled at. **Not scheduled automatically** — see below |
| Retention | 14 days (`BACKUP_RETENTION_DAYS`) |
| Encryption | AES-256 when `BACKUP_PASSPHRASE` is set. The script warns loudly when it is not |
| Location | `BACKUP_DIR`, default `./backups`, mode 700, excluded from Git and from both Docker build contexts |
| Off-site | **Not implemented.** A backup on the same host does not survive losing the host |
| Verification | Every backup is checked with `pg_restore --list`. A real restore test is `scripts/restore_postgres.sh`, run monthly |

Scheduling is left to the host, because a container that backs itself up dies
with the thing it is backing up:

```cron
15 2 * * *  cd /srv/sendstack && ./scripts/backup_postgres.sh >> /var/log/sendstack-backup.log 2>&1
```

---

## What is deliberately *not* collected

Said here because a retention policy is also a list of what there is nothing to
retain:

- No payment card details — there is no payment integration.
- No analytics, advertising or tracking cookies.
- No cross-customer profiling. One workspace's data is never used to inform
  another's.
- No AI training on customer data.
- On a legal acceptance, only the IP address and browser string, because an
  acceptance nobody can corroborate is weak evidence and those two are already
  in the request. Nothing further about the person or their device.

---

## Open decisions

Before onboarding customers who ask about retention, decide and fill in:

1. Retention for messages, campaigns and CRM records.
2. Retention after account closure, and the export window before deletion.
3. An outer bound on consent records, audit events and credit ledger entries.
4. Application log retention on the container platform.
5. Backup retention — confirm 14 days is right, and add off-site copying.
6. Voice recording and transcript retention (the most sensitive category here).
7. Whether AI provider retention is acceptable, or a zero-retention agreement
   is needed.
