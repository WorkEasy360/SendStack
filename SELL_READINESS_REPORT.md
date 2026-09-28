# Sell-Readiness Report

**Decision:** **DO NOT SELL YET**  
**Audit snapshot:** 6 September 2026  
**Primary use case assessed:** ordinary WhatsApp and WhatsApp Business App numbers connected by QR/session  
**Weighted score:** **39/100**

## Executive answer

The repository is a serious SaaS foundation with a wide surface area, but the WhatsApp path does not yet provide the integrity guarantees a paying customer reasonably buys: a scheduled send must not run early, a retry must not duplicate an accepted send, inbound/status events must not disappear, a tenant must not turn a media URL into SSRF, and an opted-out person must remain suppressed across contact lifecycle operations.

Those guarantees currently fail in confirmed code paths. The live QR runtime also showed sustained Signal decryption errors while reporting `CONNECTED`. The product therefore cannot be sold as reliable WhatsApp infrastructure, even to a small customer, until the P0s are repaired and proven with crash/replay and real-handset tests.

## Weighted scorecard

| Rubric | Weight | Score | Rationale |
|---|---:|---:|---|
| Core WhatsApp capability | 20 | **7** | QR, text, common outbound media, polls, pairing, inbox and status primitives exist; groups/history/interactions/inbound media are absent and the live session was effectively deaf. |
| Reliability and delivery | 15 | **2** | Leases and recovery foundations are thoughtful, but early dispatch, ambiguous/multipart duplicate paths, bridge race/crash gaps, and lossy callbacks are stop-ship. |
| Security | 15 | **5** | Tenant/JWT/API-key/signature foundations are real; media SSRF, public private media, browser tokens, unencrypted QR state and incomplete verification dominate. |
| SaaS architecture | 10 | **6** | Tenant/team/entitlement/audit/public API foundations are broad; hierarchy invariants, exhaustive isolation and QR HA are incomplete. |
| Campaigns and automation | 10 | **4** | Consent, opt-out UI, limits, warm-up, rotation and workflows exist; scheduling/recovery interaction, timezone, duplicate workflow and quota semantics are unsafe. |
| AI, chatbot and inbox | 10 | **6** | Knowledge/AI/handoff/chatbot/inbox/SSE are substantive; delayed/AI idempotency, media, campaign action, and transport health are incomplete. |
| Billing, users and reseller | 5 | **2** | Plans/credits/checkout/teams exist, but reseller minting, API entitlement bypass, missing recurring/dunning/tax lifecycle and unreachable RBAC block a paid claim. |
| Performance and scale | 5 | **2** | Useful indexes/load tooling exist, but QR capacity, provider throughput, long campaigns, SSE occupancy, queue isolation and HA are unmeasured. |
| UX and onboarding | 5 | **2** | Broad responsive UI, but contact profile crash, dead-end test send, timezone ambiguity, pagination truncation, false QR cancel and stale progress hurt activation. |
| Testing, observability and deployment | 5 | **3** | Large backend estate, build/deploy/restore tooling and bridge self-tests exist; frontend lint fails, browser/live-device tests are absent, alerts are unwired and recovery is incomplete. |
| **Total** | **100** | **39** | **Below a paid-production threshold** |

The score is deliberately not a linearly averaged “feature count.” A product can have hundreds of screens and still fail the transaction/message integrity contract.

## Launch gate

| Gate | Status | Evidence | Required before passing |
|---|---|---|---|
| No early or duplicate paid sends | **RED** | P0-01 through P0-03 and multipart suffix mismatch | Immutable logical operation state, schedule-aware recovery, race/crash tests, ambiguous reconciliation |
| No lost inbound/status/consent events | **RED** | P0-04; observed decrypt storm | Durable callback outbox/inbox, replay, degraded health, clean-session E2E |
| No tenant-to-host SSRF | **RED** | P0-05 | Uploaded-media IDs/isolated downloader, DNS/redirect/egress tests |
| Suppression survives identity lifecycle | **RED** | P0-06 | Durable phone suppression registry and every-path regression |
| Customer data survives ordinary CRM actions | **RED** | P0-07 merge cascade | Conversation/message merge transaction and history-preservation test |
| QR status is truthful | **RED** | P0-08 logs vs `CONNECTED` | Decrypt/forward freshness signals, `DEGRADED/REAUTH_REQUIRED`, alerts |
| Production secrets/recovery | **RED** | Plain local QR auth; DB-only/stale backups | Encrypted session/media/key backups and clean-host restore drill |
| Tenant/RBAC/entitlements | **AMBER-RED** | API gate and reseller mint findings; partial isolation matrix | Route/action matrix, fail-closed entitlements, credit conservation |
| Onboarding activation | **RED** | Hook crash and test-send dead end | Browser E2E from signup to first consented receipt |
| Operational detection | **RED** | DB/Redis-only readiness; docs-only alerts | External probes, queue/beat/bridge/backups/disk alerts, log rotation |
| Dependency/build hygiene | **AMBER** | npm audits pass; Python CVE scan absent; pinnedness issues | Full scans/SBOM, pinned builds, supported Baileys upgrade plan |

## Latest verification evidence

The clean backend command completed with **2,820 tests, 12 failures, 3 errors and 2 expected failures** in 2,377 seconds. The targeted follow-up reproduced three media-library errors/two media upload failures (Windows temp-storage permission/API contract mismatch) and two queue-routing failures caused by the stale `apps.messaging.tasks.sweep_stuck_sends` route. `manage.py check` and `makemigrations --check --dry-run` pass. Frontend build and QR TypeScript build pass; frontend lint fails at the ContactProfile conditional hook. This is a failing release gate, not a green backend suite.

## Customer journey verdict

| Journey step | Verdict | What happens today | Required acceptance evidence |
|---|---|---|---|
| Discover/pricing/signup | Partial | Pages and registration exist; billing lifecycle claims exceed implementation | Plan/trial/entitlement contract tests and honest pricing copy |
| Connect number | Partial/high risk | QR pairing and status UI exist; cancel can call success; stale/crypto health can look connected | Android/iOS Business App matrix, disconnect/replace/rescan tests, truthful degraded state |
| Import contacts | Partial | Import, dedupe, groups/tags and consent are implemented | Delete/re-import suppression and 100k-row import failure/retry test |
| Send test | **Blocked** | Fresh onboarding opens an inbox with no conversation composer | Consent-aware test recipient/composer and receipt assertion |
| Create campaign | Partial | Targeting, templates, limits, scheduling exist; first 20 results/payload timezone are unsafe | Timezone/DST, pagination, schedule/recovery boundary and idempotent launch tests |
| Wait/watch | Partial | Detail and reports exist; live progress is stale and skips are misexplained | SSE/poll authoritative aggregates, reason breakdowns and cancellation tests |
| Receive/reply | Partial/high risk | Direct inbox/reply path exists; groups/media/history/decrypt/callbacks fail | Durable callback/replay and real media/reply/restart E2E |
| Automation/chatbot/AI | Partial | Rich builders and knowledge/AI code; duplicate/drop paths and campaign selector are broken | Redelivery/AI cost/idempotency, handoff, opt-out, quota and bridge failure tests |
| Billing/support | Partial | Checkout, plans, credits, teams and admin concepts exist | Payment webhook chaos, credit conservation, RBAC, recurring/dunning/tax disclosure |
| Recover/restore | **Blocked** | DB restore tools exist; media/session/keys are excluded and backups stale/local | Encrypted off-host, clean-host full restore and timed drill |

## Buyer and market fit

### Potentially suitable after a controlled beta

- A technically sophisticated customer with an informed operator, small number of low-volume linked accounts, known recipient consent, and acceptance of unofficial-session interruption.
- Internal tooling/evaluation where loss of a message is not safety-, legal-, or revenue-critical and no confidential data is used before private-media/credential fixes.

### Not suitable today

- Healthcare, finance, government, legal evidence, customer-support SLAs, high-volume marketing, cold outreach, regulated notifications, or any workflow where a duplicate, missed, early, or post-opt-out message has material consequences.
- Reseller scale, multi-host HA, “unlimited” plans, or customers who require official Meta support/compliance.

## Commercial claims that must be removed or qualified

Do not claim official WhatsApp integration, complete Business App parity, guaranteed delivery, guaranteed uptime, ban protection, full history, native templates/buttons/lists, group support, or a tested tenant/session capacity until the corresponding evidence exists. A linked-device QR connection is not authorization to operate a bulk automation service under WhatsApp policy.

The root README is also internally contradictory: it describes an official Cloud API-only posture while the repository contains the QR/Baileys core. Sales, legal, onboarding, docs and UI must name the selected mode and its limitations.

## Required proof package for a paid beta

1. All P0 findings in [CRITICAL_BUGS.md](CRITICAL_BUGS.md) closed with code review, regression tests and production-like evidence.
2. A real-device matrix for current Android/iOS WhatsApp Messenger and Business App versions: QR pair, reconnect, direct text/media/poll, status receipts, inbound media, logout/replacement, protocol/key refresh, and a forced restart at every send phase.
3. A bridge fault-injection report: concurrent duplicate requests, kill-after-provider-accept, callback outage/replay, corrupted auth metadata, Redis loss, worker loss, database failover and stale scheduler.
4. Encrypted off-host backup plus clean-host restore proving database, media, secrets/keys, session state or clearly documented rescan state.
5. Tenant/RBAC/API/credit/entitlement negative-test matrix covering every route and role.
6. Browser E2E for the first-customer journey and a zero-error lint/build/test release gate.
7. External alerting and a support runbook with pause-all, disconnect, rescan, replay, refund and incident escalation.
8. Legal/privacy review of the unofficial protocol, consent/retention, AI subprocessors, data processing and suspension/refund language.
9. A dated capacity report with tenant/session/message limits and an explicit beta cap.

## First paid-beta limits (only after all red gates turn green)

- Single host and single bridge failure domain, with a conservative session cap based on measured soak—not a guessed number.
- No group/broadcast/status/channel/cold-outreach use; no policy-evasion features.
- Conservative per-account and per-tenant throughput/frequency limits, visible to customer and operator.
- No guarantee of WhatsApp account continuity; an official-provider migration path.
- Manual review and pause on decrypt errors, callback backlog, abnormal failures, complaints, or account replacement.
- No regulated or safety-critical notifications until official-provider support is used and separately assessed.

## Final answer to “must fix before taking money?”

Yes: every P0; live QR decrypt/health mismatch; encrypted QR/media/key recovery; bridge-aware readiness and alerting; contact crash; test-send onboarding; timezone semantics; private media; entitlements/credit conservation; and a real-device/fault-injection release report. The product is not commercially ready merely because the React build, Docker health endpoints, and broad backend tests pass.
