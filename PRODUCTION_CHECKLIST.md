# Production Readiness Checklist

Use this as a release gate, not as a list to tick optimistically. Every checked item needs an artifact: command output, test report, screenshot, dashboard, restore log, or signed review. A P0 or a red gate means **no paid production traffic**.

## Release decision

- [ ] Product mode is named in UI, terms, pricing, onboarding and support: QR/linked-device versus official Cloud API.
- [ ] Legal/privacy counsel has reviewed the unofficial QR protocol, WhatsApp policy/terms, consent, retention, subprocessors, refunds and suspension language.
- [ ] Current weighted score is at least the team-approved threshold and no P0 remains open.
- [ ] Rollback version, database migration rollback/forward plan and incident owner are recorded.
- [ ] Release candidate is immutable and tagged; dirty working-tree changes are not an artifact.

## P0 technical gates

- [ ] Future campaigns are not classified as stalled before their authoritative due time; scheduler/recovery race test passes.
- [ ] A logical outbound message has one immutable operation identity across attempts and every physical media/interactive part has a durable manifest.
- [ ] Ambiguous provider/bridge outcomes become reconciliation-required; no automatic new send occurs.
- [ ] Bridge dedup has atomic claim/fencing and durable state; concurrent duplicates and kill-after-accept tests produce at most one wire attempt or an explicit ambiguous state.
- [ ] Bridge callback outbox retries timeout/5xx/429, persists across restart, exposes dead letters and can replay safely.
- [ ] Django event ingestion separates receipt from processing and reprocesses failed events without losing idempotency.
- [ ] Media input cannot make the bridge fetch private/loopback/link-local/reserved destinations, through redirects or DNS rebinding; network egress rules enforce the same.
- [ ] Suppression is tenant+normalized-phone durable and survives delete, re-import, API upsert, CRM merge, AI, chatbot, automation, inbox and campaign paths.
- [ ] CRM merge moves/reconciles conversations/messages without cascading history deletion; audit records are retained.
- [ ] QR decrypt/forward health catches the observed Bad-MAC/decrypt condition, pauses unsafe automation, shows `DEGRADED/REAUTH_REQUIRED`, and alerts an operator.

## QR/session release gates

- [ ] Supported Baileys/Node line is selected, pinned and tested; upgrade/rollback procedure exists. `npm audit` is not treated as protocol support.
- [ ] QR auth state is encrypted at rest, transactionally written, access-controlled, versioned, backed up and separately key-escrowed.
- [ ] Session ownership has a fencing/lease mechanism; a second bridge cannot run the same account concurrently.
- [ ] Pair, refresh, cancel, disconnect, logout, replacement, phone offline, phone app upgrade, bridge restart and host loss have distinct UI states.
- [ ] A clean rescan can recover from stale/undecryptable credentials without leaving the old session active.
- [ ] Real Android/iOS tests cover WhatsApp Messenger and Business App, at least one current Desktop/Web client, text, image, video, document, poll, status receipt, inbound media and unsupported interactive types.
- [ ] Tests record device OS/app version, bridge version, locale, result and raw provider correlation IDs without leaking message content/secrets.
- [ ] Customer copy never promises group/history/calls/status/templates/buttons/list parity unless separately demonstrated.

## Messaging and campaign gates

- [ ] Every send path passes the same consent/opt-out gate and records the policy decision.
- [ ] Campaign launch is transactionally idempotent; duplicate browser clicks/API retries create no duplicate recipients/reservations.
- [ ] Recipient/account assignment is stable across retry; rotation uses health and quota headroom, not only round-robin.
- [ ] Logical and physical sends have separate quota accounting; attachment/interactive parts cannot evade account/tenant limits.
- [ ] Quiet hours and schedule interpretation use tenant/recipient IANA timezone with DST tests and a visible exact next-send preview.
- [ ] Pause/cancel blocks new claims and makes already queued countdowns harmless; accepted messages remain accurately reported.
- [ ] Campaign progress includes sent, delivered, failed, skipped, ambiguous and pending with reason breakdowns and live refresh.
- [ ] Failure-rate, complaint, opt-out and callback/decrypt health thresholds pause safely and alert.
- [ ] Rate/frequency limits are documented as safeguards, not as WhatsApp-policy authorization.

## SaaS, tenant, role, and API gates

- [ ] Route/action inventory maps every endpoint to tenant derivation, role, object ownership, scopes, rate limit and audit event.
- [ ] Automated negative tests cover sibling tenants, reseller ancestors, unrelated resellers, platform admins, suspended users, object enumeration and nested cross-tenant IDs.
- [ ] Team membership is reachable and has explicit owner/admin/manager/agent/read-only permissions; no low-role key/export/delete capability remains.
- [ ] Tenant hierarchy rejects illegal parent kind, self/cycle, depth and duplicate-root states.
- [ ] `api_access` is enforced at key creation, key use and every public API route; downgrade/revocation is immediate and tested.
- [ ] Missing/failed plan attachment fails closed to a minimal safe entitlement set.
- [ ] API idempotency, pagination, versioning, error schemas, webhook signature/replay windows and request-size limits are tested against the published spec.
- [ ] Redis outage does not turn API throttling into unlimited access; local emergency limiter or fail-closed behavior is verified.

## Billing, credits, reseller, and privacy gates

- [ ] Credit conservation invariant passes under concurrent send, release, retry, worker death, refund and duplicate API request.
- [ ] Only authorized platform code can mint credits; reseller transfer debits source and credits destination atomically and audibly.
- [ ] Payment webhooks handle forged, duplicate, delayed, reordered, partial and processor-retry events.
- [ ] Trial, expiry, upgrade, downgrade, cancellation, dunning, refund/chargeback, proration and tax invoice behavior is implemented or explicitly excluded from sales.
- [ ] Data map covers contacts, messages, media, knowledge files, embeddings, QR keys, logs, backups, AI providers and payment records.
- [ ] Access/export/correction/deletion/retention workflows are tested without erasing legally required suppression/audit evidence.
- [ ] AI providers receive only the data and retention period disclosed to the customer; tool permissions are tenant-scoped and auditable.

## Browser and frontend gates

- [ ] `npm run lint` is clean; the ContactProfile Rules-of-Hooks error is fixed.
- [ ] `npm run build` passes from a clean checkout with locked dependencies.
- [ ] Browser E2E covers signup/login, QR pairing/cancel/error, contact import/opt-out, test send, inbox reply, campaign schedule/pause/progress, billing and logout.
- [ ] Transient `/auth/me/` network/5xx does not erase a valid session; terminal 401 does.
- [ ] Lists and selectors paginate/search beyond 20 results; campaign/template/account selection is complete.
- [ ] UI displays timezone, consent, ambiguous/degraded status, skipped reason and unsupported WhatsApp capability honestly.
- [ ] React route error boundary, focus trap/restore, keyboard table interaction and screen-reader labels are tested.
- [ ] Main SPA has CSP, frame policy, MIME/referrer/security headers; tokens are not exposed in script-readable storage where the design can avoid it.

## Infrastructure and supply-chain gates

- [ ] Production Compose/Helm/Kubernetes path cannot accidentally include the development override.
- [ ] Debug is false; HTTPS/HSTS/cookie/CORS/CSRF/allowed-host/email/frontend-origin settings pass strict production checks.
- [ ] Images run as non-root with pinned base/image versions, deterministic `npm ci`/lockfiles, resource reservations and limits.
- [ ] PostgreSQL is private, encrypted/backed up, migration/connection pool limits are defined, and restore verification runs automatically.
- [ ] Redis has an explicit durability policy or every DB-backed task has reconciliation; queue loss is an accepted documented behavior only where safe.
- [ ] Worker queues are separated for messages, webhooks, AI/imports and maintenance; queue depth/age alerts exist.
- [ ] Beat has a durable heartbeat/last-run timestamp; deploy gate checks every consumed queue, including webhooks.
- [ ] Logs have rotation/retention, centralized access control and redaction tests for tokens, phones, message content, signed URLs and provider bodies.
- [ ] Python, Node, frontend, bridge, OS/container and GitHub Actions security scans are clean or have time-bounded approved exceptions; SBOM/provenance is stored.

## Backup, disaster recovery, and operations gates

- [ ] Backup includes PostgreSQL, media/object storage, knowledge files, QR auth state, encryption keys/escrow references, configuration and certificates as appropriate.
- [ ] Backups are encrypted, off-host, access-controlled, freshness-monitored and retention-tested; plaintext local dumps are not the only copy.
- [ ] RPO/RTO are stated separately for database, media, sessions, queues and secrets.
- [ ] Clean-host restore drill completes within the published RTO and proves login, tenant authorization, media access, scheduled jobs, message history, credits and QR recovery/rescan state.
- [ ] External synthetic probes check public health, DB/Redis, worker/beat, bridge reachability, callback backlog, decrypt freshness, queue age, backups and disk.
- [ ] On-call runbooks cover pause-all, bridge outage, decrypt storm, callback replay, duplicate/ambiguous send, leaked media URL, key compromise, payment incident and WhatsApp account suspension.
- [ ] Incident response has severity, owner, customer communication, evidence preservation, regulator/privacy escalation and postmortem templates.

## Verification commands (run from a clean release candidate)

These are a baseline; adapt paths to the deployment environment and retain the outputs.

```text
# Backend
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
python manage.py test apps core tests --keepdb --noinput --verbosity 1
python -m pip check
python -m pip_audit

# Frontend
npm ci
npm run lint
npm run build
npm run test -- --run   # once the frontend test gate exists

# QR bridge
npm ci
npm run build
npm audit --omit=dev
npx tsx operation-dedup-selftest.ts
npx tsx nativeflow-selftest.ts

# Deployment/configuration
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
nginx -t
./scripts/backup_postgres.sh --verify
# then execute the documented clean-host restore drill and synthetic send/receive
```

The current audit found: frontend build passes; frontend lint fails on `frontend/src/pages/contacts/ContactProfile.jsx:360`; QR build and offline self-tests pass; npm audits report zero known findings; `pip check` passes; migration check passes; strict production/readiness and live QR health gates do not pass. The full backend run completed with **2,820 tests, 12 failures, 3 errors and 2 expected failures** in 2,377 seconds. The media-library failures include Windows temp-storage permission/API response mismatches; queue-routing failures name the removed `apps.messaging.tasks.sweep_stuck_sends`; remaining failures must be triaged before release. Do not record the suite as a pass.

## Go/no-go sign-off

- [ ] Engineering owner signed every P0/P1 closure.
- [ ] Security owner signed SSRF, browser, tenant, key, file and dependency evidence.
- [ ] Operations owner signed backup/restore, alerting, capacity and incident drills.
- [ ] Product/legal owner signed WhatsApp reality disclosures and commercial scope.
- [ ] Support owner has customer troubleshooting, rescan, pause, replay, refund and suspension scripts.
- [ ] Release manager attached the dated test/device/load/restore reports.

Until all mandatory boxes are checked, use development or trusted private evaluation only. See [SELL_READINESS_REPORT.md](SELL_READINESS_REPORT.md) for the current decision and [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md) for sequencing.
