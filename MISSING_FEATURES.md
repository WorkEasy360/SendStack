# Missing and Incomplete Features

This is the product-gap register, distinct from [CRITICAL_BUGS.md](CRITICAL_BUGS.md), which describes defects in features that already exist. “Missing” means the repository has no end-to-end, supportable implementation for the requested behavior. “Partial” means a model or happy-path code exists but the customer contract is not complete.

## WhatsApp account and session features

| Capability | Status | Evidence/reality | Customer impact | Priority / prerequisite |
|---|---|---|---|---|
| Encrypted QR auth credentials | Missing | `useMultiFileAuthState` writes local JSON; no application encryption/KMS | Account takeover or forced rescans after host loss | P0; secure store before paid use |
| Durable session ownership/fencing | Missing | In-memory bridge sessions/local volume; no safe active-active | Replica split-brain and no HA | P1; auth store/sharding |
| Session migration/export/import | Missing | No operator workflow for moving a linked account | Host/bridge migration requires manual rescan | P1; credential lifecycle |
| Accurate degraded/decrypt health | Broken | `CONNECTED` can coexist with Bad-MAC/decrypt storm | Silent inbox outage | P0; telemetry/alerts |
| Full history synchronization | Missing | `syncFullHistory:false`; history events ignored | Existing chats do not appear/backfill | P2; protocol support and storage |
| Group inbox/send/admin features | Missing | Groups/broadcast JIDs deliberately dropped | Business teams cannot use group workflows | P2; explicit scope/legal review |
| Status/stories/channels/communities | Missing | No end-to-end model/provider/UI | No parity with app surfaces | P3; likely out of scope |
| Calls/presence/typing/recording | Missing | No lifecycle/model/provider support | Cannot promise conversational presence or calls | P3; likely out of scope |
| Reactions/replies/forward/edit/delete | Missing | No canonical provider operations | Agents lack common conversation controls | P2 |
| Locations/contact cards/live location | Missing | No message types/composer/provider | Common support/commerce interactions absent | P2 |
| Audio/voice notes/stickers/GIFs | Missing/partial | Low-level vocabulary is not wired to Django/UI/storage | Customers cannot send/receive common media | P2 |
| Inbound media download and rendering | Missing | Bridge retains caption/placeholder, not bytes/stable media | Agents cannot inspect customer files | P1; safe object storage/scanning |
| Native buttons/lists/URL buttons | Broken/unreliable | QR flags disable them after handset tests | Interactive flows degrade to text | P1; real-device matrix or scope out |
| Official template semantics | Not applicable in QR mode | QR “templates” are ordinary text; no Meta approval lifecycle | Sales copy can be misleading | P0 policy; label clearly |
| Poll vote ingestion/analytics | Partial | Outbound payload exists; vote lifecycle not fully mapped | Polls cannot be treated as complete | P1 |
| Delivery/read semantics | Partial | Status maps exist, but callbacks and multipart IDs can be lost | False/unknown campaign reports | P0 reliability |
| Verified account/business profile sync | Missing | No authoritative sync for Business App profile/catalog/hours | SaaS view can diverge from phone | P3 |

## Messaging integrity and transport capabilities

| Capability | Status | Gap | Priority |
|---|---|---|---:|
| Immutable logical outbound operation | Missing | Attempt/suffix IDs differ; recovery can query wrong operation | P0 |
| Atomic bridge idempotency | Missing | Lookup/send/remember race and post-send crash gap | P0 |
| Ambiguous-send reconciliation | Missing | Ambiguous results requeue as new sends | P0 |
| Durable bridge callback outbox | Missing | One fire-and-forget HTTP request, no retry/spool/dead letter | P0 |
| Durable Django callback processing state | Partial | `get_or_create` created flag doubles as processing gate | P0 |
| DB-to-Celery outbox for all send paths | Partial | Campaign/public API paths differ; inbox/automation can strand rows | P1 |
| Multipart send manifest/part state | Missing | One Message can produce many physical sends with no durable parts | P0/P1 |
| Provider capability contract per account | Partial | Base capabilities exist, but client/handset/version evidence is sparse | P1 |
| Provider message-ID reconciliation | Partial | Suffix operations and global IDs orphan receipts | P0 |
| Pause-all safety switch | Partial | Auto-pause exists, but a single operator kill switch and proof are not clear | P1 |
| Replay/dead-letter operator UI | Missing | Manual inbound replay exists; no unified event/send reconciliation console | P1 |

## Campaigns, automation, and compliance

| Capability | Status | Gap | Priority |
|---|---|---|---:|
| Schedule-aware stalled recovery | Broken | Future campaign rows can be classified as stalled and dispatched early | P0 |
| Tenant/user timezone and DST | Missing | Offset-free browser datetime is interpreted against UTC | P1 |
| Idempotent concurrent launch | Partial | Launch state check/reservation/build are not one locked transition | P1 |
| Idempotent retry of failed physical parts | Missing | Retry can resend parts already accepted | P0/P1 |
| Durable tenant+phone suppression list | Missing | Opt-out evidence survives but enforcement does not after delete/reimport | P0 |
| Consent audit across all send paths | Partial | Main campaign gate exists; lifecycle/API/AI/workflow coverage needs proof | P0/P1 |
| Cross-campaign frequency caps | Missing | Per-account limits do not express recipient-level fatigue | P2 |
| Complaint/abuse-rate telemetry | Missing | Auto-pause is failure-oriented, not recipient complaint/outcome oriented | P1 |
| Health-aware account rotation | Missing | Shuffled round-robin ignores quota headroom/decrypt/callback health | P1/P2 |
| Physical-send-aware quotas | Missing | One logical message can emit many media/interactive sends | P1 |
| Schedule pause/resume explanation | Partial | UI/status can say queued/started ambiguously; skipped reasons are generic | P2 |
| Automation schedule/webhook/contact/campaign triggers | Missing | Current triggers are mostly manual/new-conversation/inbound/keyword/button | P2 |
| Rich workflow actions | Partial | Message/wait/condition/tag/AI/assign/end; webhooks, branching depth and integrations are limited | P2 |
| Active workflow-run uniqueness | Missing | Concurrent/redelivered inbound can create overlapping runs | P1 |
| Workflow credit compensation | Partial | Reservation can strand and a `None` send can advance a run | P2 |
| Recipient timezone/quiet-hour preview | Missing | Customer cannot see exact local execution plan before launch | P1 |
| Campaign cancellation of ETA tasks | Partial | DB state changes but already published countdown tasks remain | P1 |

## Inbox, chatbot, AI, and CRM

| Capability | Status | Gap | Priority |
|---|---|---|---:|
| Fresh outbound conversation composer | Missing | Onboarding test CTA opens an inbox that requires an existing thread | P1 |
| Inbound media viewer/download | Missing | Caption/placeholder only | P1 |
| Reliable human/AI handoff claim | Partial | Handoff exists; callback/decrypt/duplicate paths need proof | P1 |
| Chatbot delayed-delivery idempotency | Missing | Redelivered tasks can send/charge twice | P1 |
| Chatbot transient retry taxonomy | Broken | Broad engine exception catch returns error instead of task retry | P1 |
| AI inbound effect claim | Missing | Legacy AI task has no idempotent processed-message marker | P1 |
| AI monetary cost ledger | Missing | Usage records tokens/latency, not provider price/cost | P2 |
| AI model fallback | Missing | No provider/model failover policy | P2 |
| Indexed vector retrieval | Missing/partial | Up to 5,000 embeddings scanned in Python | P2 |
| Tool-safety policy versioning | Partial | Tenant read/write/send gates exist; strict knowledge-only behavior needs deployment proof | P2 |
| Campaign action selector | Broken | Chatbot builders receive router-root/non-array data and can call `.map` on it | P2 |
| CRM merge history preservation | Broken | Duplicate conversation delete cascades messages | P0 |
| CRM board totals beyond 2,000 deals | Broken | First 2,000 loaded and reported as total | P2 |
| Contact/deal/task relationship invariants | Partial | Serializer checks, not durable cross-table constraints | P2 |
| Notes/activity audit retention | Partial | Models exist; deletion/merge/tenant export semantics need contract tests | P2 |

## SaaS, users, roles, reseller, and billing

| Capability | Status | Gap | Priority |
|---|---|---|---:|
| Enforced paid API entitlement | Broken | Permission exists but is not mounted on key/public API paths | P1 |
| Fail-closed missing-plan entitlement | Broken | No plan/subscription returns every feature enabled | P1 |
| Real shared-workspace membership/RBAC | Partial | Managed-user path makes new tenant; dormant team paths lack manager permissions | P1 |
| Legal tenant hierarchy invariants | Missing | Parent kind/cycle/depth/single-root constraints absent | P2 |
| Credit-conserving reseller transfer | Broken | Reseller can mint with `deduct=false` | P1 |
| Immutable credit ledger/reconciliation | Partial | Reservations/locks exist; all grant paths and conservation need proof | P1 |
| Recurring subscriptions | Missing/partial | One-off checkout/trial/grace code exists; recurring lifecycle/dunning is documented as absent | P1 commercial |
| Dunning/retry/recovery | Missing | No complete failed-payment customer workflow | P1 |
| Tax/GST invoices | Missing/partial | INR-focused billing; tax invoice/credit-note completeness absent | P1 commercial |
| Refund/chargeback/proration | Partial | Processor hooks exist; full state/ledger contract not proven | P1 |
| Reseller revenue share/commission | Missing | Docs explicitly do not provide it | P2 |
| Customer export/deletion with all objects | Partial | Tenant export/delete routes exist; media/QR/AI/backup retention needs proof | P1 privacy |
| Plan feature schema validation | Missing | Unknown feature names can fail open | P2 |
| API resource completeness/versioning | Partial | Substantial API, but contact CRUD and route/spec alignment gaps | P2 |

## Security, privacy, and operations

| Capability | Status | Gap | Priority |
|---|---|---|---:|
| Media URL SSRF defense | Broken | Internal/private URLs accepted and fetched by bridge | P0 |
| Authenticated private media downloads | Missing | nginx bearer URLs are public/long-lived | P1 |
| Malware scanning/CDR | Missing | Extension/MIME/magic checks only | P2 |
| Strong SPA CSP/security headers | Partial | Media is restrictive; normal SPA responses lack equivalent policy | P2 |
| HttpOnly refresh/session design | Missing | JWT/refresh/impersonation data is in localStorage | P2 |
| Endpoint-by-endpoint tenant matrix | Partial | Ten resource families covered; many newer surfaces unproven | P1 |
| Python CVE/SBOM/provenance gate | Missing | `pip-audit` and supply-chain evidence absent | P1 |
| Durable alerting stack | Missing | Operations doc lists alerts; Compose has no monitor/exporter wiring | P1 |
| Bridge-aware readiness | Missing | Ready checks DB/Redis, not QR/worker/beat/effective flow | P1 |
| Beat freshness heartbeat | Missing | File existence can remain healthy while scheduler is wedged | P2 |
| Explicit log rotation/retention | Missing | Compose has no log limits/rotation stanza | P2 |
| Encrypted off-host full backup | Missing | DB-only local plaintext dumps; media/session/keys/certs excluded | P1 |
| Restore drill for media/session/secrets | Missing | No timed clean-host full recovery evidence | P1 |
| Queue durability/reconciliation | Partial | Redis loss can drop tasks; not every DB-backed path reconciles | P1 |
| Session/bridge sharding | Missing | One 512MB process/local volume for all connected accounts | P1 scale |

## Frontend and developer-experience gaps

| Capability | Status | Impact | Priority |
|---|---|---|---:|
| Contact profile route stability | Broken | Rules-of-Hooks crash after loading | P1 |
| Test-send activation flow | Broken | New customer cannot initiate first outbound test | P1 |
| Accurate timezone UI | Missing | Customer-selected wall time can shift | P1 |
| API pagination components | Missing | Lists/selectors silently stop at 20 | P2 |
| Live campaign progress | Partial | No continuous refresh; skipped recipients omitted | P2 |
| Correct skip reason UX | Broken | All skips are explained as missing device | P2 |
| QR cancel/error semantics | Broken | Cancel can toast “connected”; poll errors spin forever | P2 |
| Frontend automated test gate | Missing | No Vitest/RTL/Playwright config or scripts | P1 |
| Route error boundary | Missing | Chunk/runtime error can blank the app | P3 |
| Keyboard/focus accessibility | Partial | Modal focus trap and table keyboard semantics absent | P3 |
| Consistent README/contributor guide | Missing | Frontend README is template; architecture docs contradict code | P3 |

## Explicit non-goals / not worth promising

Some app features are not just unfinished; they are poor targets for this QR SaaS scope. Calls, communities, channels, status/stories, full history backup, account-ban avoidance, stealth automation, and guaranteed parity with every first-party client should be labelled out of scope unless a separately authorized, supported provider can deliver them. See [WHATSAPP_REALITY_LIMITATIONS.md](WHATSAPP_REALITY_LIMITATIONS.md).

