# Critical Bugs and Risk Register

**Release decision:** **BLOCKED**  
**Severity convention:** P0 = stop-ship/data, security, consent, or message-integrity failure; P1 = must fix before paid use; P2 = important production risk; P3 = normal defect/debt; P4 = polish/documentation.  
**Status convention:** Confirmed = demonstrated directly from reachable code/data/logs; Observed = present in the running environment; Unverified = plausible but needs a controlled reproducer.

This register is the canonical source for issue IDs used by the other audit documents. Estimates are engineering effort for implementation plus focused tests, not calendar promises.

## P0 stop-ship findings

### P0-01 — A future scheduled campaign can be dispatched early

- **Status:** Confirmed.
- **Severity:** P0.
- **Module:** Campaign launch / durable scheduler / stalled-campaign recovery.
- **Explanation:** Launching a future campaign creates queued recipient messages and a future `CAMPAIGN_DISPATCH` job. The stalled scanner separately regards old queued messages on any queued/running campaign as stalled, without excluding a future `scheduled_at` or a live future dispatch job. After the queue rows are about one hour old, it creates an immediate `CAMPAIGN_RESUME`, whose handler dispatches the campaign.
- **Reproduction:** Create an eligible campaign with `scheduled_at = now + 24h`; launch it; advance the scanner clock by more than its one-hour stale threshold but less than 24 hours; run `recover_stalled_campaigns`; execute due jobs. Observe a resume job and outbound task before `scheduled_at`.
- **Root cause:** Two individually reasonable state machines use the same `QUEUED` state with incompatible meanings. `find_stalled_campaigns()` has no schedule-aware predicate and recovery does not require the intended dispatch time to have passed.
- **Exploitability:** No malicious actor is required. Any customer scheduling more than about one hour ahead can trigger it during ordinary beat operation.
- **User/business risk:** Irreversible early marketing, quiet-hour and consent-context violations, spoiled announcements, complaints, account bans, refunds, and contractual loss.
- **Evidence:** `backend/apps/campaigns/services.py:246-304`; `backend/apps/scheduling/services.py:358-417`; `backend/apps/scheduling/handlers.py:81-114`; beat interval in `backend/config/settings/base.py:524-526`.
- **Fix:** Do not materialize dispatchable queued rows until due, or add an explicit `SCHEDULED` state. At minimum, exclude `scheduled_at > now`, require no live future dispatch, and make the resume handler refuse before the authoritative due time inside a transaction.
- **Regression test:** A future campaign remains undispatched across multiple stale scans; exactly one dispatch becomes eligible at/after `scheduled_at`; recovery and dispatch racing at the boundary still enqueue each recipient once.
- **Effort:** Medium, 2–4 engineering days including migration/state transition review and concurrency tests.
- **Sell blocker:** **Yes.**

### P0-02 — Ambiguous send recovery creates a new operation and can duplicate delivery

- **Status:** Confirmed.
- **Severity:** P0.
- **Module:** Message lease recovery / QR provider status reconciliation.
- **Explanation:** The QR bridge can correctly answer that an operation is known but ambiguous. Django recovery only treats `known && sent` as reconciled; every other result is requeued. A subsequent lease increments the attempt and generates a new send operation ID, so bridge deduplication for the original operation cannot prevent another wire send.
- **Reproduction:** Make a QR send exceed the provider timeout after the wire send may have occurred; persist bridge result `ambiguous`; let the Django lease expire; run recovery; claim again. Observe a new operation ID and a second bridge send attempt.
- **Root cause:** The recovery state machine collapses `unknown`, `known failed`, and `known ambiguous` into one retry branch, while operation identity is tied to an attempt rather than the logical message.
- **Exploitability:** Routine network latency, worker loss, or a provider response timeout is sufficient.
- **User/business risk:** Duplicate customer messages, duplicate charges/credits, repeated chatbot actions, spam reports, and loss of trust.
- **Evidence:** `qr-bridge/src/providers/baileysProvider.ts:840-869`; `backend/apps/messaging/recovery.py:250-257`; `backend/apps/messaging/leasing.py:63-119`.
- **Fix:** Give each logical outbound message one immutable provider operation key. An ambiguous operation must enter a durable `RECONCILIATION_REQUIRED` state, query provider/bridge evidence, and time out to a human-visible outcome; it must never become a fresh send automatically.
- **Regression test:** For `{known:true,sent:false,ambiguous:true}`, recovery performs zero send calls, preserves the original operation key, creates one reconciliation incident, and remains idempotent across repeated sweeps.
- **Effort:** Medium/large, 4–7 days including state migration, UI/operator workflow, and crash tests.
- **Sell blocker:** **Yes.**

### P0-03 — QR operation deduplication has race and post-send crash gaps

- **Status:** Confirmed by code inspection; concurrency/crash reproducer still required.
- **Severity:** P0.
- **Module:** QR bridge outbound send / operation store.
- **Explanation:** Sending performs `lookup -> sendMessage -> remember` without a per-operation lock or atomic durable transition. Two concurrent requests can both miss and send. If the process dies after WhatsApp accepts the message but before `remember`, the next process has no record. Store persistence is deferred to the next event-loop turn and persistence errors are swallowed.
- **Reproduction:** Issue two concurrent POSTs with the same operation ID while delaying `sendMessage`; both pass lookup. Separately kill the bridge immediately after the provider promise resolves and before/deferred persistence; restart and replay the same operation.
- **Root cause:** A cache is being used as a transaction log. It has no atomic claim, write-ahead `SENDING` record, fsync/transaction boundary, or ownership fencing.
- **Exploitability:** Normal duplicate delivery from Celery/HTTP retries or a bridge/container restart can hit the gap.
- **User/business risk:** Duplicate sends with no authoritative audit trail; the ambiguity can never be reconstructed after host loss.
- **Evidence:** `qr-bridge/src/providers/baileysProvider.ts:770-850`; `qr-bridge/src/operationStore.ts:75-89,127-136`.
- **Fix:** Put operations in a transactional database/Redis primitive with unique operation ID and states `CLAIMED/SENDING/SENT/AMBIGUOUS/FAILED`; use atomic claim/fencing; write intent before network I/O; make replay return the durable terminal/ambiguous result. Design explicitly for the unavoidable send/ack uncertainty window.
- **Regression test:** 100 concurrent identical requests yield one mocked wire call; kill at every state transition and restart; no state permits an automatic second send after a possibly accepted first send.
- **Effort:** Large, 1–2 weeks.
- **Sell blocker:** **Yes.**

### P0-04 — Bridge callbacks can be permanently lost

- **Status:** Confirmed.
- **Severity:** P0.
- **Module:** QR inbound/status webhook delivery and Django ingestion.
- **Explanation:** The bridge performs one `fetch` with no timeout, retry queue, durable spool, or dead-letter path; it logs and discards errors/non-2xx. Event handlers do not await delivery. Django stores an event with `get_or_create` and only signals processing when `created` is true. If downstream synchronous processing fails after creation and the request returns 500, even a manual identical replay finds `created=false` and does not process it.
- **Reproduction:** Make Django return 500 or become unavailable during an inbound message/status callback. Restore it. Observe no automatic retry. For the second branch, throw in a receiver after event creation and replay the exact payload; observe the stored-but-unprocessed event is skipped.
- **Root cause:** At-most-once fire-and-forget transport combined with a dedup record that doubles as a processed marker.
- **Exploitability:** Any deploy, timeout, transient 5xx, DNS/network interruption, or receiver exception can trigger it.
- **User/business risk:** Inbound messages, delivery/read receipts, chatbot turns, consent keywords, and automation triggers disappear permanently while customers receive no alarm.
- **Evidence:** `qr-bridge/src/webhooks.ts:8-24`; `qr-bridge/src/providers/baileysProvider.ts:376-405`; `backend/apps/whatsapp/webhooks.py:60-74`.
- **Fix:** Use a durable bridge outbox with event IDs, bounded exponential retry/jitter, timeout, dead letter, backlog metrics, and replay tooling. In Django, separate `RECEIVED/PROCESSING/PROCESSED/FAILED`, claim transactionally, and allow failed events to be retried without recreating them.
- **Regression test:** Inject connection refusal, timeout, 429 and 500; restart both services; each event is eventually processed exactly once at the business-effect layer. Force a receiver failure after insert and prove replay succeeds.
- **Effort:** Large, 1–2 weeks.
- **Sell blocker:** **Yes.**

### P0-05 — Tenant media URLs enable server-side request forgery and data exfiltration

- **Status:** Confirmed.
- **Severity:** P0.
- **Module:** Campaign/template/public API media input -> Django QR provider -> bridge/Baileys fetch.
- **Explanation:** Authenticated tenants can save arbitrary HTTP(S) media URLs. The shared validation intentionally accepts internal hostnames. The QR adapter forwards external URLs; Baileys fetches them from the bridge network context and then sends the bytes to a chosen WhatsApp recipient.
- **Reproduction:** As a low-privilege tenant, create image/document media pointing to a controlled HTTP endpoint and observe the bridge request. In an isolated test network, point at a private RFC1918/loopback service and observe reachability. Do not test cloud metadata in a real environment.
- **Root cause:** URL syntax/type validation is confused with outbound destination authorization. The existing SSRF guard used by other outbound integrations is not applied to media, and there is no bridge-level egress allowlist.
- **Exploitability:** Directly exploitable by any account allowed to create a media campaign/template/API request; blind SSRF is enough, and sending the fetched bytes supplies an exfiltration channel.
- **User/business risk:** Access to internal admin services, cloud metadata/credentials, cross-service data, legal notification obligations, and full host/account compromise depending on network topology.
- **Evidence:** `backend/core/media.py:31-85`; `backend/apps/campaigns/models.py:64`; `backend/apps/templates_app/models.py:87`; `backend/apps/whatsapp/providers/qr_bridge.py:200-240,439-449`; `qr-bridge/src/providers/baileysProvider.ts:1109-1122`.
- **Fix:** Prefer server-controlled uploaded media IDs. Otherwise resolve and validate every redirect/hop, block loopback/link-local/private/reserved/IPv6-mapped ranges, prevent DNS rebinding, restrict ports/schemes/size/type, proxy through an isolated downloader, and enforce egress firewall rules at the bridge.
- **Regression test:** A matrix of IPv4/IPv6 private, decimal/hex, userinfo, redirect, DNS-rebind simulation, localhost aliases and metadata addresses is rejected at Django and fetch service; approved public fixtures still send.
- **Effort:** Medium, 3–5 days for application guards; larger for network isolation.
- **Sell blocker:** **Yes.**

### P0-06 — Deleting and re-importing an opted-out contact re-enables marketing

- **Status:** Confirmed.
- **Severity:** P0.
- **Module:** Contacts / consent evidence / message gating.
- **Explanation:** Hard delete is exposed. The append-only consent event deliberately retains tenant and phone, but upsert only looks for a live contact. A recreated contact gets `subscribed=True`, and gating checks the new row rather than retained suppression history. If `require_marketing_consent` is off, marketing is allowed.
- **Reproduction:** Create a contact, opt it out, delete it through the normal endpoint, import/upsert the same normalized phone without explicit opt-in, then evaluate campaign eligibility. Observe `subscribed=True` and allowed marketing.
- **Root cause:** Consent evidence and enforcement use different sources of truth. There is no tenant+phone suppression registry or restoration during contact creation.
- **Exploitability:** Ordinary cleanup/re-import is sufficient; an operator can also use it to bypass an opt-out.
- **User/business risk:** Messages sent after STOP/unsubscribe, complaints, regulatory exposure, WhatsApp enforcement, and demonstrably false compliance claims.
- **Evidence:** `backend/apps/contacts/views.py:159-165`; `backend/apps/compliance/models.py:153-175`; `backend/apps/contacts/upsert.py:57-68`; `backend/apps/contacts/models.py:87-88`; `backend/apps/compliance/services.py:121-140`; `backend/apps/messaging/gating.py:153-180`.
- **Fix:** Make suppression a durable tenant+normalized-phone entity independent of Contact. Creation/import must restore `subscribed=false`; only a recorded user-originated opt-in may clear it. Consider soft delete for auditable identities.
- **Regression test:** `opt-out -> delete -> every import/upsert/API-create path -> send gate` remains blocked; case/format-normalized phone variants are covered; an explicit verified opt-in is the only passing transition.
- **Effort:** Medium, 3–5 days including migration/backfill and all ingestion paths.
- **Sell blocker:** **Yes.**

### P0-07 — CRM contact merge can cascade-delete message history

- **Status:** Confirmed.
- **Severity:** P0.
- **Module:** CRM contact merge / conversations / messages.
- **Explanation:** Merge changes `Message.contact` from duplicate to primary but does not move `Message.conversation`. If both contacts have a conversation on the same WhatsApp account, code deletes the duplicate conversation as an allegedly empty shell. `Message.conversation` uses `CASCADE`, so its messages are deleted.
- **Reproduction:** Create primary and duplicate contacts, same-account conversations for each, and one message in each. Merge duplicate into primary. Observe the duplicate-conversation message is gone.
- **Root cause:** The merge assumes updating a redundant `contact` foreign key also moved messages between conversations. The database relation remains on the soon-to-be-deleted conversation.
- **Exploitability:** Any authorized user using duplicate merge on common real-world data triggers it; no attacker required.
- **User/business risk:** Irrecoverable customer correspondence, missing audit/consent evidence, corrupted reports and legal retention failure.
- **Evidence:** `backend/apps/crm/services.py:460-481`; `backend/apps/messaging/models.py:81-85`; inadequate coverage at `backend/apps/crm/tests.py:531-537`.
- **Fix:** Transactionally move messages to the survivor conversation (handling uniqueness/order/provider IDs), reconcile metadata, then delete only after asserting zero dependents. Preserve an immutable merge audit record.
- **Regression test:** Messages in both conversations survive, preserve direction/content/provider IDs/timestamps, point at the surviving contact/conversation, and repeated/concurrent merge attempts are safe.
- **Effort:** Medium, 2–4 days plus data-repair query for previously affected rows where recoverable.
- **Sell blocker:** **Yes.**

### P0-08 — A real QR session can be effectively deaf while health reports connected

- **Status:** Observed in the running audit environment; upstream protocol cause not yet isolated.
- **Severity:** P0 operational release condition.
- **Module:** Baileys auth/decryption / bridge health / Django account status.
- **Explanation:** A recent 30-minute log sample contained 916 `Bad MAC`/session errors, 98 failures to decrypt with known sessions, and 44 missing-session errors. The authenticated bridge endpoint simultaneously reported `CONNECTED` and zero forwarded/dropped/undecryptable messages. These crypto errors occur before the `messages.upsert` counter path, and Django can return its stored connected status without live bridge validation.
- **Reproduction:** Use the affected linked session and receive messages while collecting bridge crypto logs, its session-health response, Django health/account status, and inbox arrivals. The status/counters do not reflect the error stream.
- **Root cause:** Immediate cause is an unhealthy/out-of-sync Signal session or Baileys/protocol incompatibility. Observability root cause is that effective health measures socket state, not successful decrypt/forward flow, and lower-level errors are not counted.
- **Exploitability:** Not an adversarial exploit; a protocol upgrade, credential corruption, multi-device key churn, or stale session can produce it.
- **User/business risk:** Silent inbound message loss, missed STOP keywords/support requests, broken chatbot/automation, and a misleading green status.
- **Evidence:** Runtime log aggregation during this audit; counter logic `qr-bridge/src/providers/baileysProvider.ts:580-748`; Django adapter/status logic `backend/apps/whatsapp/providers/qr_bridge.py:371-410`; short circuit `backend/apps/whatsapp/connections/manager.py:194-195`.
- **Fix:** First isolate with a clean rescan and supported Baileys version on both WhatsApp clients. Instrument crypto/decrypt events, last valid inbound/outbound/status callback, callback backlog, auth-write failures, and reconnect cause. Add health states `DEGRADED/REAUTH_REQUIRED`; alert and pause automations/campaigns when evidence is stale or error rate crosses threshold.
- **Regression test:** Corrupt/stale auth and injected decrypt errors turn health degraded, surface a customer action, stop unsafe automation, and recover through a documented rescan without silently dropping events.
- **Effort:** Investigation 2–5 days; durable fix/telemetry likely 1–2 weeks.
- **Sell blocker:** **Yes.**

### P0-09 — Non-text QR sends cannot be correlated correctly during recovery

- **Status:** Confirmed.
- **Severity:** P0.
- **Module:** Multipart/media/interactive send operation identity, recovery and receipts.
- **Explanation:** A `MessageSendAttempt` stores an unsuffixed base operation key. Physical media, buttons, lists and poll calls append `:a1`, `:main` or `:intro`. Recovery asks the bridge only for the base key, so an accepted physical send is returned as unknown. The receipt fallback also exact-matches the base, orphaning an early receipt carrying the suffixed key.
- **Reproduction:** Send a media message, allow bridge acceptance under `base:a1`, kill the worker before its DB update, then run expired-lease recovery. The query for `base` is unknown and the logical message is requeued with a new attempt; replay can send the media again.
- **Root cause:** One logical database row can generate multiple provider operations, but there is no persisted operation/part manifest. Recovery assumes one row equals one provider call.
- **Exploitability:** Any worker crash, soft-time-limit, lost response or early receipt on a non-text send is enough.
- **User/business risk:** Duplicate media/menu/poll content, orphaned delivery evidence, incorrect credits and an unreconcilable audit trail.
- **Evidence:** `backend/apps/messaging/leasing.py:63-74`; `backend/apps/messaging/services.py:493-504,549-557,616-623,685-693,758-765`; `backend/apps/messaging/recovery.py:229-257`; `backend/apps/messaging/receivers.py:56-67`.
- **Fix:** Persist one `MessageSendPart`/operation manifest per physical provider call, with stable IDs, order, payload hash and state. Recovery queries every submitted part and resumes only provably unsent parts; ambiguous parts stop the message for reconciliation.
- **Regression test:** Record acceptance only for `base:a1`, simulate death before DB update, run recovery, and prove it queries the physical ID, records the part sent, and makes zero duplicate calls. Repeat for multi-attachment, buttons, lists and polls with out-of-order receipts.
- **Effort:** Large, 1–2 weeks together with P0-02/P0-03.
- **Sell blocker:** **Yes.**

## P1 pre-sale blockers

The table retains the same required fields in compact form.

| ID | Status / module | Explanation and root cause | Reproduction / exploitability | User or business risk | Recommended fix and regression test | Effort | Blocks sale? |
|---|---|---|---|---|---|---:|:---:|
| P1-01 | Confirmed; reseller credits | Any reseller can call the credit API with caller-controlled `deduct=false`; service mints credits without debiting an actor. Behavior is explicitly tested. | POST a grant as reseller repeatedly. No exploit beyond valid reseller auth. | Unlimited economic value/sends; prepaid model and revenue reconciliation fail. | Make mint platform-only; reseller transfer must lock/debit reseller balance and ledger both sides. Test privilege matrix, concurrency and conservation of credits. | 3–5d | Yes |
| P1-02 | Confirmed; entitlements/public API | `api_access` exists but is not mounted on key creation or public API execution. Plans with it disabled can still create/use keys. | Use a tenant on a plan with `api_access=false`; create key and call endpoint. | Paid feature bypass, plan mis-selling and uncontrolled load. | Enforce entitlement at key CRUD and every request; revoke/suspend on downgrade. Contract tests for all plan transitions. | 2–4d | Yes |
| P1-03 | Confirmed; entitlement defaults | No plan/subscription returns every feature enabled; signup can survive failed trial attachment. | Remove/mis-seed trial plan or force `TrialUnavailable`; inspect entitlements. | Accidental premium/unlimited accounts during deploy/data fault. | Fail closed to a minimal explicit state; make registration+trial atomic or block app until resolved. Test missing-plan paths. | 2–4d | Yes |
| P1-04 | Confirmed; team/RBAC | Managed-user flow creates a separate tenant; team subsystem is unreachable. If enabled, any authenticated tenant user can mutate team roles/policies, manage keys, export or request deletion. | Enable/add same-tenant member and exercise endpoints as low role. | Privilege escalation, data export/deletion and misleading team SKU. | Define workspace membership roles and central permissions; enforce object/action matrix; negative tests per role. | 1–2w | Yes before teams |
| P1-05 | Confirmed; workflow engine | Active `(workflow, contact)` has no uniqueness; start is read-then-create and auto-trigger uses `force=True`. | Deliver the same inbound event concurrently. | Duplicate workflow runs, messages and charges. | Conditional unique active-run constraint plus transactional claim/idempotent event key. Concurrency test. | 3–5d | Yes |
| P1-06 | Confirmed; chatbot tasks | Delayed replies have no persistent per-delivery claim; redelivery sends again. Engine catches most transient errors, preventing advertised task retry. | Invoke same delayed task twice; inject transient provider/DB error. | Duplicate or silently dropped bot turns and credit errors. | Delivery table/idempotency key, atomic claim, explicit retry taxonomy, durable failure state. Test broker redelivery/failures. | 4–7d | Yes |
| P1-07 | Confirmed; AI reply task | Legacy AI reply has no inbound claim/idempotency and fresh message per invocation; concurrent counter update is stale. | Invoke same inbound task twice/concurrently. | Duplicate AI replies/charges and wrong quotas. | Unique inbound-effect key, locked claim, atomic counters, explicit retry/reconcile. Test redelivery and crash points. | 3–5d | Yes if enabled |
| P1-08 | Confirmed; frontend | `ContactProfile` calls a hook only after early loading/error returns. Successful fetch changes hook count. | Open `/contacts/:id`; first render loads, second renders extra hook. | Deterministic route crash; support and CRM unusable. | Move all hooks before returns and guard inside. Add RTL and Playwright route test. | <1d | Yes |
| P1-09 | Confirmed; onboarding/inbox | Onboarding sends a new user to Inbox for a test, but Inbox only replies to an existing conversation. | Fresh tenant with no inbound conversation follows test-send CTA. | Activation funnel dead end; QR success cannot be proven. | Dedicated consent-aware test composer or seed/test workflow; E2E from signup through receipt. | 2–4d | Yes |
| P1-10 | Confirmed; timezones | Browser submits offset-free `datetime-local`; server timezone is UTC and no tenant IANA conversion exists. | In IST choose 09:00 and inspect payload/execution. | Sends at wrong time; quiet-hour/compliance breaches. | Store IANA timezone, submit offset/UTC, display interpretation; DST/non-UTC tests. | 2–4d | Yes |
| P1-11 | Confirmed; media access | nginx serves `/media` without auth or expiry, including potentially private knowledge uploads. | Open copied media URL in incognito. | Persistent bearer-link disclosure; enterprise privacy failure. | Separate provider-public assets from private documents; authenticated/signed expiring downloads and object storage policy. | 4–8d | Yes |
| P1-12 | Confirmed; DR | PostgreSQL backup excludes media, QR auth, `.env`/encryption keys and certs. Current local plaintext backup was ~6 days old. | Restore on a clean host using only documented backups. | Lost files, forced rescans, undecryptable data, RPO breach. | Encrypted off-host full-system backup, key escrow, automated freshness check, quarterly clean-host drill. | 1–2w | Yes |
| P1-13 | Confirmed; health/alerts | Readiness checks DB/Redis, not live QR/worker/beat/message flow. Alert conditions are docs, not deployed monitors. | Stop bridge or wedge beat; `/health/ready/` remains 200. | Long invisible outage and false SLA evidence. | Component/degraded health, synthetic send/receive canary, external alerts, fresh heartbeat and log limits. | 1w | Yes |
| P1-14 | Confirmed; QR credentials/scale | Unsupported production helper stores plaintext JSON on one local volume; single in-memory bridge cannot be horizontally scaled safely. | Inspect auth volume/deployment; lose host or run two instances. | Credential theft, forced rescans, split-brain sessions and SPOF. | Encrypted DB/KMS-backed auth store, per-session ownership/fencing, migration, backup/restore; test failover. | 2–4w | Yes |
| P1-15 | Confirmed; inbound media | Bridge maps type/caption but does not provide downloaded content/stable URL; inbox stores placeholder. | Receive image/video/document/audio/sticker and inspect stored/rendered message. | Agents cannot see customer evidence/files; automation/AI lacks content. | Controlled download, scanning, encrypted object storage, metadata and UI; type/size/failure tests. | 1–2w | Yes for support claims |
| P1-16 | Confirmed; production path | Current override intentionally selects development. Strict check fails unsafe settings; deploy script has special handling/refusal around the tracked override. | Run strict production check with current environment / follow deploy path without manual correction. | Misdeployment can expose debug/insecure cookies/CORS/email behavior. | One CI-tested immutable production command, environment validation, secret injection, staging smoke, rollback. | 2–4d | Yes |
| P1-17 | Confirmed; commercial/legal | Core QR transport is unofficial reverse-engineered WhatsApp Web automation. Current root README still claims official Cloud API-only architecture. | Compare code/dependency with product/docs and WhatsApp/Baileys policies. | Customer deception, account bans, service interruption, refund/processor/legal risk. | Correct all marketing/contracts, explicit risk acceptance, supported-provider option, prohibition/abuse process; counsel review. | 1–2w + legal | Yes |
| P1-18 | Confirmed; test evidence | No frontend test framework/E2E; live QR button/list rendering and recovery are not proven; lint currently fails. | Inspect scripts/test files and run lint/self-tests. | Regressions reach customers in the highest-risk journeys. | CI gate lint/build/backend/bridge, browser E2E, real-device matrix, chaos suite. | 2–4w | Yes |

References for P1-01 through P1-07: `backend/apps/credits/{serializers.py,views.py,services.py,tests.py}`; `backend/apps/subscriptions/{permissions.py,services.py}`; `backend/apps/publicapi/views/{dashboard.py,base.py}`; `backend/apps/accounts/registration.py`; `backend/apps/teams`; `backend/apps/automation/models.py:180-214,services.py:817-853,1123-1137`; `backend/apps/chatbot/tasks.py:69-131,outbound.py:216-230,engine.py:236-247`; `backend/apps/ai/tasks.py:9-38,services.py:853-1012`.

## P2 important risks

| ID | Status / module | Explanation/root cause | Reproduction/exploitability and risk | Fix/test | Effort | Blocks sale? |
|---|---|---|---|---|---:|:---:|
| P2-01 | Confirmed; tenant hierarchy | No database rules prevent illegal parent kinds, self/cycles, multiple platform roots; descendant traversal lacks a visited set. | Persist a cycle via ORM/admin and traverse: potential infinite loop and authorization ambiguity. | DB/service invariants, cycle-safe traversal, migration audit and adversarial hierarchy tests. | 3–5d | Before reseller scale |
| P2-02 | Confirmed; automation credits/state | Credit reservation commits before render/message creation; exceptions strand reserves. A `None` send can still let workflow advance/completed. | Inject render/persistence/no-account failure: false success and credit leakage. | One saga/state machine with compensation and invariant reconciler; failure-path tests. | 4–7d | Before automation SKU |
| P2-03 | Confirmed; cross-table tenant integrity | Many same-tenant relationships are serializer-only, so tasks/admin/future bulk code can create cross-tenant graphs. | ORM-create mismatched tenant/contact/tag/deal/team member. | Composite keys/triggers or model clean/service enforcement plus periodic integrity scan. | 1–3w | Conditional |
| P2-04 | Confirmed; CRM metrics | Board loads first 2,000 deals and reports that subset as totals; task can mismatch contact and deal contact. | Create >2,000 deals; totals silently undercount. | SQL aggregation/pagination and consistency validation; boundary test at 2,001. | 2–4d | No |
| P2-05 | Confirmed; plans | Unknown feature names fail open; nullable-owner uniqueness can allow duplicate platform plan names; default creation races. | Typo a feature or concurrently seed defaults. | Closed feature enum/schema, conditional unique constraints, atomic `get_or_create`/locks. | 3–5d | Before plan expansion |
| P2-06 | Confirmed; AI scale/cost | Usage lacks monetary/provider cost; embeddings scan up to 5,000 rows in Python; no model fallback; tool mode can weaken knowledge-only behavior. | Large KB/cost reconciliation/provider outage. | Cost ledger, vector index/service, explicit fallback/safety policies; load and policy tests. | 2–4w | Before AI scale |
| P2-07 | Confirmed; public API | Contact API is incomplete and throttle deliberately fails open when Redis is unavailable. | Stop Redis and send high rate; update/delete contacts unavailable. | Fail closed or local emergency limiter; complete/version API and outage tests. | 3–7d | Before public API sale |
| P2-08 | Confirmed; pagination | Multiple pages/selectors keep only DRF's first 20 results. | Create 21+ campaigns/templates/accounts/tags; later items disappear. | Shared paged query/async selectors and UI pagination; 21/1000-row tests. | 3–5d | Yes for normal use |
| P2-09 | Confirmed; campaign progress | Detail does not poll/SSE; skipped recipients omitted from percent; skip explanation assumes no device. | Run campaign with skips and leave detail open. | Stream/poll authoritative aggregates and show server reason breakdown; UI tests. | 2–4d | No |
| P2-10 | Confirmed; auth UX | Any `/auth/me/` exception deletes all tokens, including transient network/5xx. | Reload offline or force 502. | Clear only on terminal auth response; retry/offline state; tests. | <1d | No |
| P2-11 | Confirmed; QR wizard | Cancel calls the connected callback and leaves a pending account; polling errors are swallowed. | Start scan then cancel / break bridge network. | Distinct callbacks, pending cleanup/state, retry/error UI; E2E. | 1–2d | No |
| P2-12 | Confirmed; chatbot campaign UI | Client loads API router root/non-array and later calls `.map`; failure may appear empty or throw. | Open campaign-action builder. | Correct list URL, paginate, validate response and show error; component test. | <1d | No |
| P2-13 | Confirmed; workers/broker | One concurrency-4 worker handles all queues; Redis durability is not explicit; deploy queue gate omits `webhooks`. | Saturate AI/import or lose Redis/consumer. | Split critical queues, AOF/durable reconciliation, queue-depth alarms, gate every queue. | 3–7d | At meaningful load |
| P2-14 | Confirmed; browser security | JWT/refresh/impersonation tokens are in `localStorage`; main SPA lacks strong CSP/security headers. | Any future script injection can steal long-lived credentials. | HttpOnly/Secure/SameSite refresh flow, short-lived access in memory, CSP/nonces, security headers and XSS tests. | 1–2w | Before enterprise |
| P2-15 | Confirmed; uploads | Extension/magic/size checks exist, but no malware scan/CDR; public/provider media and private docs are mixed. | Upload benign test signature/eicar only in isolated CI. | Quarantine, scan, safe rendering, separate storage classes and audit. | 1w | Before enterprise |
| P2-16 | Confirmed; capacity | Real QR session density, provider throughput, reconnect storm and 1k+ tenancy are unmeasured; SSE can occupy small-host Gunicorn threads. | Controlled load/soak not currently recorded. | Capacity model, ASGI/dedicated realtime, sharded bridge, queue isolation, soak/chaos results. | 4–8w | Before scale claims |

## P3/P4 backlog

| ID | Severity | Status/module | Problem and risk | Fix/test | Effort | Sell blocker? |
|---|---|---|---|---|---:|:---:|
| P3-01 | P3 | Confirmed; chatbot versions | Read-highest/create and publish lack complete locking; concurrent edits can collide. | Lock chatbot/version allocation; concurrency test. | 1–2d | No |
| P3-02 | P3 | Confirmed; team canned replies | Stale Python `use_count + 1` loses concurrent increments. | Database `F()` update; concurrency test. | <1d | No |
| P3-03 | P3 | Confirmed; errors/accessibility | No route error boundary; modal lacks focus trap/restore; clickable table rows lack keyboard semantics. | Error boundary and WCAG interaction tests. | 2–4d | No |
| P3-04 | P3 | Confirmed; Docker reproducibility | QR image uses `npm install` despite lockfile; certbot is `latest`; only memory limits despite docs. | `npm ci`, pin image digests/versions, document/test resources. | <1d | No |
| P3-05 | P3 | Confirmed; docs | Root README says official Cloud API/no unofficial client; frontend README is Vite template; DR scheduler guidance contradicts durable scheduler and could prompt duplicate dispatch. | Rewrite and release-review docs; runbook game day. | 2–4d | P1 for false architecture/DR advice |
| P3-06 | P3 | Confirmed; schema quality | Active deploy check emits hundreds of warnings, largely serializer/auth schema generation. | Classify/suppress intentionally and fix the rest; archive clean release output. | 3–7d | No |
| P3-07 | P3 | Confirmed; onboarding | Page says five steps but renders six while backend has eight milestones; completion/action mismatch. | One authoritative milestone definition plus E2E. | 1–2d | No |
| P3-08 | P3 | Confirmed; backup policy | Off-host plaintext backup is only a warning rather than a strict production failure. | Require encryption and restore-key validation. | <1d | No after P1-12 |
| P4-01 | P4 | Confirmed; compose/docs | Header claims reservations+limits although only limits are present; capacity assumptions unclear. | Correct comment or add reservations/CPU policy; config test. | <1d | No |

## Required release regression suite

Two reproducible audit tests are now checked in at `backend/tests/test_audit_regressions.py` (currently expected failures): opt-out suppression across delete/re-import and CRM merge message preservation. Promote them to ordinary gates after the corresponding fixes land.

The following are acceptance tests, not optional recommendations:

1. Schedule/recovery boundary and concurrency test for P0-01.
2. Immutable operation identity and ambiguous reconciliation test for P0-02.
3. Concurrent duplicate plus process-kill matrix for P0-03.
4. Callback outage/restart/replay/exactly-once-effects suite for P0-04.
5. SSRF normalization, redirect, DNS, IPv4/IPv6 and egress tests for P0-05.
6. Opt-out/delete/re-import test across UI/API/import paths for P0-06.
7. Contact merge preservation and concurrency test for P0-07.
8. Real-device decrypt degradation/rescan/health alert test for P0-08.
9. Credit conservation and role/entitlement authorization matrices.
10. Browser E2E: signup -> QR -> consented test send -> receive -> reply -> schedule -> progress -> opt out.

No P0 may be closed from a unit test alone. Each requires a production-like integration result, a monitoring signal, a rollback/recovery procedure, and sign-off by someone other than the implementer.
