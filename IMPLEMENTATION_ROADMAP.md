# Implementation Roadmap

This roadmap is ordered by customer harm and dependency, not by the most visible UI feature. It assumes the team keeps the QR/linked-device product as the primary path and uses Cloud API only as a benchmark/optional supported mode. No production implementation is made by this audit.

## Release stages

```text
Stage 0: Freeze risk and instrument
        ↓
Stage 1: Restore message/data/security invariants
        ↓
Stage 2: Make the QR/session service supportable
        ↓
Stage 3: Complete the first-customer journey and SaaS controls
        ↓
Stage 4: Prove recovery, capacity and private beta
        ↓
Stage 5: Scale only after measured evidence
```

Each stage ends with an objective go/no-go review. Passing a stage does not erase WhatsApp policy or account-enforcement risk.

## Stage 0 — Freeze and observe (1–3 days)

**Outcome:** no new paid traffic, no feature claims that exceed evidence, and enough telemetry to distinguish a protocol outage from a database/UI outage.

| Work | Owner | Exit evidence |
|---|---|---|
| Freeze campaign/automation sends for affected QR accounts when decrypt/callback health is stale | Messaging + SRE | Kill/pause switch tested and audited |
| Add dashboards/log fields for session ID hash, account ID, decrypt errors, last valid inbound, last callback, callback backlog, operation ID, physical part ID and ambiguous count (never raw secrets/content) | QR + SRE | Synthetic alert fires in staging |
| Correct sales/README/onboarding language to “QR/linked-device, unofficial, best effort” | Product/legal | Reviewed copy and customer acknowledgement |
| Capture current runtime evidence and affected-session state without exposing identifiers | SRE | Incident record and preserved logs |
| Lock new reseller/public-API entitlements and credit grants behind an explicit feature flag | Billing/platform | Denied-path test and audit event |

## Stage 1 — Message, consent, and security invariants (2–4 weeks)

**Outcome:** the system never knowingly sends early/duplicate after an ambiguous result, cannot use a tenant URL for SSRF, cannot resurrect an opt-out, and cannot silently delete message history.

### 1A. Campaign timing and operation identity

1. Introduce authoritative `SCHEDULED`/`DISPATCHING`/`RUNNING` states or defer recipient materialization until due.
2. Add a due-time predicate to stalled recovery and a handler-side authoritative due check.
3. Create a logical outbound operation key stable for the Message and a durable physical-part manifest for media/buttons/lists/polls.
4. Implement atomic operation claim/fencing and `SENT/FAILED/AMBIGUOUS/RECONCILIATION_REQUIRED` states.
5. Make retry/recovery query all physical part IDs and never create a new wire operation for an ambiguous result.
6. Add concurrent, kill-after-provider-accept, delayed receipt, worker redelivery and bridge restart tests.

**Exit:** 100 concurrent same-operation requests make one wire call in a fake provider; kill matrix never silently re-sends; a future campaign remains quiet through stale scans; media/interactive receipts reconcile.

### 1B. Durable events

1. Add a bridge callback outbox with timeout, retry/jitter, signed event ID, dead letter and replay.
2. Add Django event processing state distinct from deduplication (`RECEIVED`, `PROCESSING`, `PROCESSED`, `FAILED`).
3. Re-run failed receivers without creating duplicate business effects.
4. Store connection lifecycle events and expose backlog/age.

**Exit:** callback outage, Django 5xx, process restart and receiver exception tests eventually produce one business effect and one auditable terminal state.

### 1C. SSRF and media boundary

1. Make uploaded media IDs the default campaign/template/API input.
2. If remote URLs remain, centralize DNS/redirect/private-range validation and apply it at the bridge fetch boundary too.
3. Put media downloading in a restricted worker/egress network; constrain time/size/type and scan/quarantine uploads.
4. Split private knowledge/media storage from recipient-public outbound assets.

**Exit:** URL abuse matrix and network egress test pass; no internal destination is fetched; approved public fixture still works.

### 1D. Consent and data integrity

1. Create a tenant+normalized-phone suppression registry backed by append-only evidence.
2. Restore suppression on every contact create/upsert/import/API/CRM merge path; only deliberate verified opt-in clears it.
3. Repair CRM merge transaction to move/reconcile messages before deleting a conversation.
4. Add data-repair reports for previously affected contacts/merges.

**Exit:** delete/re-import remains blocked after STOP, and merge preserves every message/provider ID in adversarial tests.

## Stage 2 — Make QR/session service supportable (2–5 weeks)

**Outcome:** a QR account has truthful lifecycle state, recoverable credentials, controlled ownership and a documented rescan path.

| Workstream | Design direction | Exit evidence |
|---|---|---|
| Auth store | Encrypted transactional DB/KMS-backed credential revisions; no plaintext multi-file helper in production | Key rotation, corrupt-write, restore and permission tests |
| Ownership | Per-session lease/fencing token and one active socket owner | Two bridge instances cannot send the same session; migration game day |
| Health | `CONNECTED`, `DEGRADED`, `REAUTH_REQUIRED`, `DISCONNECTED`; last-valid-message/callback freshness and decrypt counters | Inject Bad-MAC/stale socket; UI pauses unsafe sends and alert fires |
| Lifecycle | Pair, cancel, logout, replaced, phone offline, bridge restart and rescan are distinct | Android/iOS Business App matrix with screenshots/logs |
| Callback/event | Durable outbox from Stage 1 integrated with connection state | Disconnect/connected ordering and replay tests |
| Dependency | Supported/pinned Baileys line, deterministic `npm ci`, staged upgrade and rollback | Compatibility report and clean rebuild |
| Scale shape | Define session placement/sharding boundary even if private beta remains single-host | Documented cap and no unsafe second replica |

Do not use this stage to add more WhatsApp message types before the transport is truthful and recoverable.

## Stage 3 — Sellable first-customer journey and SaaS controls (2–4 weeks)

**Outcome:** a customer can join, connect, send a consented test, schedule in their timezone, see honest progress, and administer their workspace without deterministic failures.

### Activation and UI

- Move all ContactProfile hooks above conditional returns; add route E2E.
- Add a dedicated consent-aware new-conversation/test composer or a server-mediated test recipient flow.
- Correct QR cancel/error callbacks and polling retry UI.
- Store/display tenant IANA timezone; submit UTC/offset and test DST.
- Add shared paginated/searchable selectors and list pagination; fix chatbot campaign endpoint/response validation.
- Add campaign polling/SSE, terminal state math including skipped/ambiguous, and server reason breakdown.
- Add route error boundary, focus semantics and keyboard interaction where required.

### Tenant, role, entitlement, and money

- Publish a route/action RBAC matrix and enforce it centrally.
- Make `api_access` a real entitlement at key creation and request execution.
- Fail closed on missing/invalid plan data.
- Make reseller transfer debit the source and credit the target atomically; platform-only mint path with immutable audit.
- Decide and document recurring billing, dunning, tax, refunds, proration and reseller commission scope before pricing.
- Add billing/credit/entitlement contract and concurrency tests.

**Exit:** browser journey passes from clean tenant; low-role/sibling-tenant/plan-disabled/credit-conservation tests pass; no lint errors; commercial copy matches implemented billing.

## Stage 4 — Recovery, operations, and private beta (3–6 weeks)

**Outcome:** a small, explicitly risk-disclosed group can use the product with an operator who can see and recover failures.

### Recovery package

- Encrypted off-host backups for PostgreSQL, media/knowledge objects, QR auth state, encryption keys/escrow references, configuration and certificates.
- Freshness/RPO monitoring; no local plaintext dump as the only copy.
- Clean-host restore preserving data and proving explicit QR reauth/rescan behavior where session restoration is impossible.
- Redis-loss reconciliation for every task class; queue age/worker/beat freshness alerts.

### Observability and support

- External monitors for public readiness, DB/Redis, worker/beat, bridge, callback backlog, decrypt freshness, queue age, backups and disk.
- Structured redaction and log rotation/central retention.
- Operator controls: pause tenant/account/all, quarantine ambiguous sends, replay callbacks, force rescan, disable AI/automation, refund/credit correction.
- Incident runbooks for duplicate/early send, missing inbound, SSRF/key leak, WhatsApp suspension and data restore.

### Private-beta envelope

- Start with a dated, measured cap of low-activity tenants and accounts; no guessed “unlimited” plan.
- Require consent evidence and prohibit cold outreach, group/broadcast automation, policy evasion, regulated notifications and safety-critical use.
- Review every incident and complaint; pause automatically on decrypt/callback/complaint thresholds.
- Publish status semantics and unofficial-integration disclosure; collect explicit risk acceptance.

**Exit:** 30-day soak or equivalent fault-injection evidence, one clean restore drill, zero open P0s, no unexplained duplicate/early sends, and support can diagnose/rescan/replay without database surgery.

## Stage 5 — Scale and advanced parity only from evidence (ongoing)

Do not start this stage because a roadmap says so. Start it only after Stage 4 measurements show the bottleneck and customer demand justifies the complexity.

| Area | Required investment | Evidence before claim |
|---|---|---|
| QR sharding | Session placement service, bridge workers, ownership/fencing, migration and regional egress | 100-session soak, failover and migration report |
| Queueing | Dedicated message/webhook/AI/import workers, backpressure, durable broker or reconciliation | Queue-age SLO under burst and worker loss |
| Realtime | ASGI/pub-sub or dedicated SSE tier, connection limits/backpressure | Concurrent-stream and reconnect test on production topology |
| Data/media | Managed HA Postgres/Redis, object storage, lifecycle/encryption/CDN policy | Restore/region-loss and 1M+ message load test |
| Provider parity | Real-device matrix for each added type, inbound/outbound/status/retry support | Versioned capability catalog and rollback plan |
| Official mode | Optional Cloud API adapter with explicit mode separation | Contract tests, official docs/policy review and migration UX |

Groups, status, calls, communities, history backup, template approval, and other first-party parity should be separate product decisions. Do not let a low-level Baileys type or a passing mock promote an unsupported feature to a sales promise.

## Test plan by risk

| Test layer | Required cases |
|---|---|
| Unit/property | Timezone/DST, phone normalization/suppression, URL/IP normalization, operation state transitions, quota/credit invariants |
| Django transaction/concurrency | Schedule recovery boundary, launch/retry idempotency, merge preservation, workflow run uniqueness, credit transfer, API entitlement, callback reprocessing |
| Bridge unit | Concurrent operation claim, crash-before-flush, malformed auth metadata isolation, timeout/abort, callback retry/backoff |
| Integration | Django↔bridge signatures, event outbox, provider IDs/physical parts, Redis/DB failure and replay |
| Browser E2E | Signup→QR→test→inbox→campaign→pause/progress→opt-out, role/plan/pagination/error states |
| Real device | Android/iOS Messenger/Business App versions, reconnect/replacement, media/polls/unsupported interactions, inbound media and receipts |
| Chaos/restore | Kill worker/bridge after each send phase, callback outage, Redis loss, DB failover, corrupt auth, clean-host restore |
| Security | SSRF matrix, IDOR/tenant/RBAC, XSS/CSP, token theft, file malware, webhook replay, key rotation, rate-limit outage |
| Load/soak | Sessions, direct sends, 1k/10k campaign behavior, SSE, reconnect storm, callback burst, queue age and DB aggregates |

## Definition of done for a paid production release

The release is done only when [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md) is signed, [CRITICAL_BUGS.md](CRITICAL_BUGS.md) has no P0/P1 blockers relevant to enabled features, [SECURITY_AUDIT.md](SECURITY_AUDIT.md) has an accepted residual-risk register, and the [SELL_READINESS_REPORT.md](SELL_READINESS_REPORT.md) score is recalculated from dated evidence—not roadmap intent.

