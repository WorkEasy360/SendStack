# Complete WhatsApp SaaS Product Audit

**Audit date:** 6 September 2026  
**Repository:** MultiSender / SendStack  
**Decision:** **DO NOT SELL YET**  
**Production-readiness score:** **39/100**  
**Audit posture:** skeptical, evidence-first, and focused on ordinary WhatsApp and WhatsApp Business App accounts connected through QR/linked-device sessions. Meta Cloud API is used only as a capability and reliability benchmark.

## Executive verdict

This is not a demo-sized codebase. It has a substantial multi-tenant Django/React SaaS, a public API, contacts, consent controls, campaigns, an inbox, automation, AI/knowledge features, credits, plans, teams, branding, audit logs, scheduled jobs, health endpoints, Docker deployment, and a QR bridge. The backend test estate is unusually broad for a project at this stage.

It is nevertheless **not safe to take money for the QR/session product today**. Nine confirmed or directly observed stop-ship failure classes can send at the wrong time, send twice, erase history, bypass an opt-out, lose an inbound/status event permanently, fetch and transmit internal-network content, or silently stop receiving decryptable messages while reporting the account as connected. These are not feature-polish problems. They violate the minimum integrity properties of a messaging product.

There is also a non-code commercial constraint: the QR engine is built on the reverse-engineered Baileys WhatsApp Web protocol. It is not an official WhatsApp integration. WhatsApp's current terms prohibit unauthorized non-personal use, bulk or automated messaging, reverse engineering, and creating equivalent APIs; its messaging guidelines allow automated enforcement and account suspension. The product must not be represented as approved, ban-proof, guaranteed, or equivalent to Meta's supported Business Platform. See [WhatsApp Terms of Service](https://www.whatsapp.com/legal/terms-of-service), [WhatsApp Messaging Guidelines](https://www.whatsapp.com/legal/messaging-guidelines), and [Baileys' own project disclaimer](https://github.com/WhiskeySockets/Baileys).

The correct launch sequence is: stop the P0 integrity/security failures; prove the repaired system on real devices and forced-crash tests; finish recovery, alerting, security, and onboarding P1s; then run a small, disclosed, single-host private beta. Do not promise high-volume or high-availability service until the QR bridge is sharded, its credentials are in a production store, and measured results exist.

## What was actually examined

- Roughly 669 backend source files and 131 frontend source files.
- Django 6.1, Django REST Framework 3.18, Celery 5.6.3, PostgreSQL 16, Redis 7, React 19/Vite 8, nginx, Docker Compose, and the TypeScript QR bridge.
- QR lifecycle, auth persistence, reconnect paths, outbound send and deduplication, inbound parsing, webhook delivery, status reconciliation, campaign scheduling and recovery.
- Tenant scoping, JWT/API-key authentication, roles, impersonation, public API, signed webhooks, upload/media paths, billing/credits, admin/reseller surfaces, observability, backup/restore, deployment, and frontend journeys.
- Static evidence, tests, build/lint checks, Compose validation, live health probes, bridge self-tests, and recent service logs. No destructive tests and no production code changes were made.

This is a point-in-time audit of the working tree, which already contained extensive user changes. A passing unit test does not prove WhatsApp delivery; only provider acknowledgements, webhook/status reconciliation, and controlled handset tests do.

## Architecture found

```text
Browser
  -> nginx (React SPA, /api proxy, /media, SSE)
      -> Django/DRF + Gunicorn
          -> PostgreSQL (tenants, messages, campaigns, jobs, billing)
          -> Redis (Celery broker/cache/SSE)
          -> Celery worker + beat
          -> QR bridge HTTP API (shared bearer token)
                -> Baileys WebSocket / WhatsApp linked-device protocol
                -> local multi-file authentication state
                -> signed HTTP callbacks to Django
          -> optional external services (AI, Razorpay, Twilio, Meta Cloud API)
```

The data model is predominantly tenant-aware. Durable scheduled jobs and message leases are good design choices. The weakest boundary is the QR bridge: one process, one local credential volume, an in-memory session/socket registry, non-durable callback delivery, and only partial end-to-end state propagation.

## Weighted readiness score

The requested weights are treated as points, not averaged percentages.

| Area | Weight | Awarded | Basis |
|---|---:|---:|---|
| Core WhatsApp features | 20 | **7** | Direct text and common outbound media exist, but inbound media is not retained, interactive compatibility is poor, groups/history/reactions/etc. are missing, and a live session showed sustained decrypt failures. |
| Reliability | 15 | **2** | Durable jobs and leases exist, but wrong-time sends, ambiguous retries, non-atomic deduplication, lossy callbacks, and invisible decrypt failure are P0 defects. |
| Security | 15 | **5** | Strong authentication/scoping primitives are offset by a QR-media SSRF/exfiltration path, public media, browser token storage, incomplete CSP, plaintext session state, and incomplete security verification. |
| SaaS architecture | 10 | **6** | Real tenant, entitlement, audit, API-key, webhook, branding, and admin foundations; team/RBAC is not actually ready, entitlement defaults fail open, and horizontal QR scaling/exhaustive isolation proof are absent. |
| Campaigns and automation | 10 | **4** | Rich targeting, consent, throttling, recovery, and workflows, but future campaigns can send early, opt-outs can resurrect, workflows can duplicate, and schedule timezone semantics are unsafe. |
| AI, chatbot, and inbox | 10 | **6** | Substantial knowledge, handoff, inbox and SSE implementation; inbound media, new-conversation onboarding, and async AI/chatbot idempotency are broken/incomplete. |
| Billing, users, and reseller | 5 | **2** | Plans, credits, transactions and reseller administration exist, but resellers can mint unbacked credits, API entitlements are not enforced, and recurring/dunning/tax completeness is absent. |
| Performance and scalability | 5 | **2** | Some documented load testing and sensible indexes, but real QR throughput, tenant counts, long-lived SSE capacity, queue isolation, and bridge scale are unproven. |
| UX and onboarding | 5 | **2** | Broad, responsive UI with good state handling; contact detail crashes, first test-send is a dead end, lists truncate, and live progress is stale. |
| Testing, observability, deployment | 5 | **3** | Large backend suite and credible deploy/DB-restore tooling; frontend has no test gate, lint fails, QR handset evidence is missing, alerts are not wired, and full recovery is absent. |
| **Total** | **100** | **39** | **Not sell-ready** |

### Supporting quality scores

These are diagnostic, not extra points: backend engineering 72/100; frontend engineering 55/100; protocol capability 34/100; production reliability 20/100; security posture 38/100; UX/onboarding 48/100; deployment/operations 52/100; observability/disaster recovery 38/100; commercial readiness 22/100.

## The nine stop-ship failures

| ID | Failure | Customer impact | Evidence |
|---|---|---|---|
| P0-01 | Future campaigns can be classified as stalled and resumed immediately | A campaign scheduled hours or days ahead can send after about one hour | `backend/apps/campaigns/services.py:246-304`; `backend/apps/scheduling/services.py:358-417`; `backend/apps/scheduling/handlers.py:81-114` |
| P0-02 | Ambiguous send recovery requeues with a new operation ID | A message that may already have reached WhatsApp can be sent a second time | `backend/apps/messaging/recovery.py:250-257`; `backend/apps/messaging/leasing.py:63-119`; `qr-bridge/src/providers/baileysProvider.ts:859-869` |
| P0-03 | QR dedup lookup/send/record is not atomic or crash-durable | Concurrent retries or a process death after send can duplicate a message | `qr-bridge/src/providers/baileysProvider.ts:770-850`; `qr-bridge/src/operationStore.ts:75-89,127-136` |
| P0-04 | QR callback delivery is fire-and-forget and Django can permanently strand a created event | Inbound messages and receipts can vanish with no retry or operator recovery | `qr-bridge/src/webhooks.ts:8-24`; `qr-bridge/src/providers/baileysProvider.ts:376-405`; `backend/apps/whatsapp/webhooks.py:60-74` |
| P0-05 | Tenant-controlled media URLs can make the QR bridge fetch internal URLs | SSRF can expose internal services/metadata and transmit fetched bytes to a recipient | `backend/core/media.py:31-85`; campaign/template/public API media fields; `backend/apps/whatsapp/providers/qr_bridge.py:200-240,439-449`; `qr-bridge/src/providers/baileysProvider.ts:1109-1122` |
| P0-06 | Delete/re-import resurrects an opted-out phone | A recipient who opted out can receive marketing again | `backend/apps/contacts/upsert.py:57-68`; `backend/apps/compliance/services.py:121-140` |
| P0-07 | CRM merge deletes the duplicate conversation with messages still attached | Customer conversation history is cascade-deleted | `backend/apps/crm/services.py:460-481`; `backend/apps/messaging/models.py:81-85` |
| P0-08 | QR decrypt/session storm is invisible to account health | The inbox and STOP/chatbot processing can be deaf while UI says connected | Runtime evidence plus `qr-bridge/src/providers/baileysProvider.ts:580-748`; `backend/apps/whatsapp/connections/manager.py:194-195` |
| P0-09 | Non-text sends use suffixed operation IDs but recovery/status query the base | Accepted media/interactive parts are reported unknown, orphan receipts, and can resend | `backend/apps/messaging/services.py:493-765`; `backend/apps/messaging/recovery.py:229-257`; `backend/apps/messaging/receivers.py:56-67` |

The live QR evidence behind P0-08 is especially important: over a recent 30-minute window the bridge logged 916 `Bad MAC`/session errors, 98 decrypt failures, and 44 missing-session errors while its authenticated health endpoint still said `CONNECTED` and reported zero forwarded, dropped, or undecryptable inbound messages. Django short-circuits on its stored `CONNECTED` value and does not surface the bridge counters (`backend/apps/whatsapp/connections/manager.py:194-195`). Whatever the protocol cause, the current health model cannot detect the effective outage.

Full reproduction, root-cause, exploitability, remediation, and regression criteria are in [CRITICAL_BUGS.md](CRITICAL_BUGS.md).

## QR/session engine assessment

### What works in code

- QR creation and polling, linked-device auth state, reconnect/backoff, generation guards, logout/replaced handling, text/media/poll sends, send timeouts, message leases, a bridge operation ledger, signed callbacks, and basic session status endpoints.
- Direct inbound text is mapped into the Django inbox; delivery/read status mapping exists.
- The bridge deliberately filters group/broadcast traffic instead of accidentally treating group participants as direct chats.
- A self-test demonstrated operation deduplication in the non-racing happy path. The native-flow self-test only validates payload construction; it explicitly does not prove that a real handset renders buttons or lists.

### What is not production-grade

- The bridge uses Baileys `useMultiFileAuthState` (`qr-bridge/src/authState.ts:47`). Baileys documents that helper as inefficient and not appropriate for production; its security guidance treats session credentials like SSH keys and recommends encryption at rest. See [Baileys session management](https://github.com/WhiskeySockets/docs/blob/main/authentication/session-management.mdx) and [Baileys security policy](https://github.com/WhiskeySockets/Baileys/security).
- Installed/locked Baileys is 6.7.24. That clears the specific critical advisory fixed in 6.7.22, and `npm audit --omit=dev` reported zero known npm vulnerabilities, but Baileys currently supports the v7 line; an unsupported protocol client remains an upgrade and breakage risk. See [GHSA-qvv5-jq5g-4cgg](https://github.com/advisories/GHSA-qvv5-jq5g-4cgg).
- `syncFullHistory` is false and history events are intentionally ignored. The product cannot truthfully promise WhatsApp chat backup/import or full multi-device history.
- Group messages are deliberately dropped. Status/stories, calls, communities, channels, presence/typing, reactions, edit/delete, forward, quoted replies, locations, contacts, stickers, voice-note semantics, and native catalog/commerce are absent or not wired end-to-end.
- Inbound image/video/document metadata reaches Django without downloaded bytes or a stable URL; the inbox stores a caption/placeholder. Audio and sticker inbound types degrade further.
- Buttons and lists are disabled for QR because observed handset rendering was unreliable. Template objects become ordinary text; they are not Meta-approved templates.
- Session health is process-local and incomplete. Protocol decrypt errors can occur before the counters that the API exposes.
- A single bridge and local auth volume are a single point of failure and prevent safe horizontal scaling.

The full parity table is in [WHATSAPP_FEATURE_MATRIX.md](WHATSAPP_FEATURE_MATRIX.md), and the non-negotiable platform constraints are in [WHATSAPP_REALITY_LIMITATIONS.md](WHATSAPP_REALITY_LIMITATIONS.md).

## Campaigns, rotation, consent, and abuse controls

The implementation has more safety controls than a typical bulk sender: subscribed-contact filtering, consent policy/evidence, opt-out keywords, unsubscribe affordances/fallbacks, quiet hours, hourly/daily account limits, warm-up, randomized delays and batches, shuffled round-robin account assignment, paused-account exclusion, failure-rate auto-pause, and audit records.

Those controls are useful product safeguards but do not authorize bulk or automated messaging over an unofficial linked-device protocol. They also cannot compensate for P0-01 through P0-03. Until repaired, schedule, retry, and rotation logic can amplify rather than contain harm.

Specific gaps include recipient-local timezone semantics, a stable per-recipient/account assignment policy for recovery, clear skipped-reason breakdowns, real-time progress, complaint/abuse-rate telemetry, account-quality signals, cross-campaign frequency caps, and controlled load evidence.

## Tenant, API, role, and data security

### Strengths

- DRF defaults to authenticated access; JWT authentication checks tenant/user state and suspension.
- API keys are hashed, scope-aware, revocable, and accompanied by request logging.
- Incoming provider/webhook paths use signatures and deduplication primitives.
- Core querysets are generally tenant-scoped, and explicit isolation tests cover contacts, groups, tags, WhatsApp accounts, templates, campaigns, messages, conversations, API keys, and webhooks.
- Upload handling uses randomized paths, size/type checks, and filename normalization.
- Audit, impersonation, entitlement, role, and credit-ledger concepts exist.

### Gaps and residual risk

- The media URL SSRF path is exploitable by an authenticated tenant and crosses into the bridge network context.
- `/media/` is served by nginx as an unauthenticated, indefinitely reusable bearer URL. This includes material that may be private, such as knowledge documents.
- Access, refresh, and impersonation credentials live in `localStorage`; the main SPA response lacks a strong CSP. A future XSS has a large account-takeover blast radius.
- QR auth credentials are plaintext JSON on a local volume and are not included in the backup procedure.
- The central tenant-isolation suite is strong evidence for ten resource families, not proof for every newer endpoint/model. An endpoint-by-endpoint authorization matrix and automated negative tests remain required.
- File-type validation is not malware scanning or content disarm/reconstruction.
- Python dependency CVEs were not assessed because `pip-audit` is not installed; `pip check` passed.

See [SECURITY_AUDIT.md](SECURITY_AUDIT.md).

## Billing, plans, users, reseller, and admin

Tenant plans, entitlements, credit reservations/settlements, transaction history, teams/invitations/roles, customer administration, impersonation, reseller/customer relationships, branding, plan views, and Razorpay-facing code are substantial. These are closer to a real SaaS than placeholders.

The sales boundary is still incomplete: project documentation acknowledges recurring subscription lifecycle/dunning and tax invoice limitations. Payment webhook handling must be proven against duplicate, delayed, reordered, and forged events; every credit mutation needs an immutable, reconcilable ledger invariant. Support impersonation tokens in browser storage deserve a hardened flow. Pricing/entitlement behavior needs contract tests for upgrade, downgrade, expiration, cancellation, refund, and concurrency.

## Inbox, chatbot, AI, CRM, and automation

There is meaningful implementation: multi-agent inbox concepts, SSE with polling fallback, assignments, notes/tags, automation workflows, keyword/rule chatbot behavior, AI provider integration, knowledge ingestion/retrieval, human handoff, CRM pipeline, tasks, and voice hooks.

The WhatsApp substrate limits all of them. Inbound media is not preserved; decrypt failures are invisible; a fresh user cannot initiate the promised test from the inbox; the chatbot campaign action receives the router-root object instead of a campaign array and can fail at `campaigns.map`; and no end-to-end evidence proves AI/chatbot replies survive bridge restart, callback failure, opt-out, quota exhaustion, or concurrent human takeover.

## Frontend and first-customer journey

The React app has broad route coverage, lazy loading, a responsive shell, and generally explicit loading/error/empty states. Its production build succeeds. The journey is still not sellable:

1. Pricing, signup, login, and onboarding exist.
2. QR connection can create an account, but cancel invokes the connected callback and can show a false success.
3. Contacts/import/consent UI exists.
4. Onboarding asks the customer to send a real test and opens Inbox, but a fresh inbox can only reply to an existing inbound conversation. There is no new-conversation composer.
5. Campaign creation exists, but a browser `datetime-local` value is sent without an offset and can be interpreted as UTC rather than the customer's wall time.
6. Selectors/lists commonly fetch only the first 20 API results; campaigns and templates lack complete paging.
7. Running campaign detail does not poll/stream updates, and progress omits skipped recipients.
8. Contact profile deterministically violates React's Rules of Hooks after loading (`frontend/src/pages/contacts/ContactProfile.jsx:337-373`) and can crash the route.
9. Automation/AI/billing pages exist but are not coherently introduced, and onboarding milestones do not line up with the UI steps.

## Performance and scale realism

No evidence supports a production claim at 100, 1,000, 10,000, or 100,000 QR-connected tenants.

- A documented development-host test reached a knee around 345 HTTP requests/second with roughly 60 workspaces and 886k messages, but it did not measure provider delivery or a recommended production host.
- A recommended two-vCPU Gunicorn deployment has about 40 request threads, while each SSE connection can occupy a thread for up to 600 seconds. Dozens of open inbox streams can force fallback or starve ordinary requests.
- One Celery worker with concurrency four consumes messaging, AI, imports, maintenance, and webhooks; bursty work can delay sends.
- One 512 MB QR bridge holds all live socket/session state and cannot safely share its auth store with a second process.
- PostgreSQL/Redis tuning, indexes, pagination, batching, and DB-backed jobs are positive, but the system has no measured QR session density, send throughput, reconnect storm, webhook storm, or restore-under-load result.

Practical envelope after P0/P1 remediation: a **controlled beta of about 10 low-activity tenants on one host**, with conservative per-account limits and explicit risk disclosure. One hundred is unproven. One thousand or more requires bridge sharding/session placement, managed or HA data services, queue separation, object storage, asynchronous real-time transport, and repeated load/failure tests.

## Reliability, observability, and disaster recovery

The repository includes structured logging, health/diagnostic endpoints, Celery task design, a durable scheduler, PostgreSQL backup/restore scripts, restore verification, and deploy-time checks. These are strong foundations.

Current operations still fail a commercial standard:

- `/health/ready/` checks database and Redis but not the QR bridge, worker availability, beat freshness, or effective message flow.
- Alert thresholds are documentation, not a deployed alerting stack. Sentry is optional. Compose has no explicit log rotation.
- Beat health checks the existence of a file, not a fresh heartbeat.
- Current backups are local plaintext PostgreSQL dumps. At audit time the newest was about six days old, versus a stated 24-hour database RPO; there was no verified off-host encrypted copy.
- PostgreSQL backup does not preserve customer media, knowledge uploads, QR auth state, application secrets/encryption keys, or certificates. Host loss can permanently lose files and force every QR account to scan again.
- Redis loss can drop broker tasks; reconciliation covers some DB-backed work, not every task class.
- Production settings correctly refuse unsafe email configuration, but the current developer override intentionally fails strict production checks. The repository's tracked override and deploy instructions need one unambiguous, automated production path.

## Test and validation results

| Check | Result | Meaning |
|---|---|---|
| Backend Django suite | **2,820 tests: 12 failures, 3 errors, 2 expected failures** | Full run took 2,377 seconds. Known failures include media-library uploads/API expectations (Windows temp permission/API contract) and two stale Celery route names; remaining failures require triage. Broad coverage is not a substitute for live WhatsApp tests |
| Frontend production build | **Pass** | 2,143 modules built successfully |
| Frontend lint | **Fail** | Rules-of-Hooks error at `ContactProfile.jsx:360`; warnings also remain |
| QR TypeScript build | **Pass** | Bridge compiles |
| QR operation dedup self-test | **Pass (5 cases; 4 wire sends)** | Happy-path replay protection only; does not test race/crash window |
| QR native-flow self-test | **Offline checks pass** | Script explicitly does not prove handset rendering |
| Frontend/bridge production npm audit | **0 known vulnerabilities** | Does not settle protocol or application security |
| Python dependency consistency | **Pass (`pip check`)** | Python CVE scan was unavailable |
| Audit regression tests | **Pass as 2 expected failures** | `backend/tests/test_audit_regressions.py` reproduces opt-out resurrection and CRM merge message loss; decorators must be removed when fixes land |
| Migration drift | **Pass** | No missing Django migrations reported |
| Compose config and nginx syntax | **Pass** | Base/production Compose render; nginx syntax valid |
| Current Docker services and HTTP smoke | **Healthy / 200** | Only proves the running dev stack responds |
| Strict production readiness | **Fail in current configuration** | Debug/HTTPS/cookies/CORS/email/frontend URL/backup findings; expected from dev override but blocks launch |
| Django deploy check | **389 warnings in active dev configuration** | Mostly schema-generation noise plus expected dev security warnings; must be triaged to a zero-actionable-warning release record |
| Live QR log sample | **Fail** | 916 session/Bad-MAC errors, 98 decrypt failures, 44 missing-session errors in 30 minutes while status remained connected |

Tests that should gate the first release are specified in [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md) and [CRITICAL_BUGS.md](CRITICAL_BUGS.md).

## Top ten actions, in order

1. Block future scheduled campaigns from stalled recovery; add the schedule/recovery interaction regression.
2. Make outbound operation identity stable for the full logical message and treat ambiguous status as manual/durable reconciliation, never a new send.
3. Replace QR dedup with an atomic, durable operation state machine that closes concurrent and post-send crash gaps.
4. Add a durable bridge callback outbox with bounded retries, idempotent reprocessing, dead-letter visibility, and a replay command.
5. Block SSRF at media ingestion and again at fetch time; allow only uploaded media or rigorously validated public egress.
6. Diagnose and repair the live Bad-MAC/decrypt storm; expose decrypt rate, last valid inbound, callback backlog, and bridge reachability in health/alerts.
7. Move QR credentials to an encrypted production store with fencing/session ownership, backup, restore, and forced-rescan UX.
8. Fix contact-profile crash, onboarding test-send, timezone conversion, pagination, live campaign progress, and QR cancel semantics.
9. Implement complete encrypted off-host recovery for PostgreSQL, media, QR auth, and key material; perform a clean-host restore drill.
10. Run real Android/iOS/WhatsApp/Business App compatibility and failure-injection suites, then a conservative private beta with explicit unofficial-integration terms.

## What breaks first under real customer usage?

The likely first visible failure is **inbox silence despite a green connected badge**: current live logs already show a decrypt/session storm that health counters miss. The most damaging silent failure is a future campaign sending early. Under retries or process interruption, duplicate sends are credible. Under a malicious or compromised tenant, the media URL fetch path is the most direct security exploit. Under host loss, QR credentials/media disappear even if PostgreSQL restores. Around a few dozen concurrent inbox users on a small host, long-lived SSE threads and the shared worker become capacity risks.

## What must be fixed before taking money?

Every P0 must be closed with a reproducible regression test and live-device proof. The live QR decrypt outage, contact crash, timezone handling, onboarding test-send, private media access, credential storage/backup, full-host recovery, bridge-aware readiness, and actionable alerting are also mandatory. Legal and sales copy must state that QR linkage is unofficial and can be interrupted or banned. A paid launch additionally needs support/SLA boundaries, refund/dunning behavior, privacy/retention operations, and a documented capacity limit.

Until those gates are satisfied, the honest positioning is **internal development or a risk-disclosed private evaluation**, not a production WhatsApp SaaS.

## Deliverables

- [WHATSAPP_FEATURE_MATRIX.md](WHATSAPP_FEATURE_MATRIX.md)
- [SELL_READINESS_REPORT.md](SELL_READINESS_REPORT.md)
- [CRITICAL_BUGS.md](CRITICAL_BUGS.md)
- [MISSING_FEATURES.md](MISSING_FEATURES.md)
- [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md)
- [SECURITY_AUDIT.md](SECURITY_AUDIT.md)
- [WHATSAPP_REALITY_LIMITATIONS.md](WHATSAPP_REALITY_LIMITATIONS.md)
- [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md)
