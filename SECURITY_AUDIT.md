# Security Audit

**Security score:** **5/15 weighted points (38/100 posture)**  
**Decision:** **Not acceptable for paid production**  
**Scope:** web/API authentication, tenant authorization, roles, API keys, webhooks, QR bridge, media/files, secrets, payments/credits, browser, infrastructure, dependencies, audit/recovery.

## Threat model

The important principals are: anonymous internet user; authenticated customer; compromised customer user; reseller; reseller-managed customer; support/superadmin; malicious webhook sender; malicious media host; compromised browser script; compromised bridge/container; and an unreliable WhatsApp/Meta endpoint. Protected assets include phone numbers, contacts and consent evidence, message content/media, QR Signal credentials, access/refresh/API keys, AI knowledge documents, credits/payment records, tenant boundaries, and outbound messaging authority.

The highest-risk trust crossing is:

```text
tenant-supplied URL -> Django validation -> QR bridge network -> Baileys fetch -> WhatsApp recipient
```

That crossing currently permits SSRF and provides an exfiltration channel. The next most consequential crossings are bridge callbacks into Django (lossy), browser token storage (XSS blast radius), and support/reseller authorization (commercial privilege boundaries).

## Immediate security blockers

| Finding | Severity | Status | Impact | Required response |
|---|---|---|---|---|
| Tenant-controlled QR media URL can fetch internal/private destinations | **P0** | Confirmed | SSRF, metadata/internal-service access, byte exfiltration | Replace URL input with controlled media IDs; layered destination/redirect/DNS validation and egress firewall |
| Opt-out state disappears on contact delete/recreate | **P0** | Confirmed | Unlawful/unwanted contact after STOP; abuse enforcement | Durable tenant+phone suppression independent of contact lifecycle |
| QR callbacks can be forged only if secret leaks, but valid callbacks can be lost permanently | **P0 reliability/security** | Confirmed | Lost consent keywords, status and inbound audit trail | Durable outbox/inbox, processed state, retries, dead letter, replay |
| Private media and knowledge uploads are public bearer URLs | **P1** | Confirmed | Indefinite disclosure after URL leak | Authenticated/signed downloads; separate public outbound vs private storage |
| QR session credentials are plaintext files on a local volume | **P1** | Confirmed | Account takeover and forced rescan after theft/loss | Envelope encryption/KMS, least privilege, durable store, backup/rotation |
| Reseller can mint credits without a compensating debit | **P1** | Confirmed | Economic/authorization bypass | Platform-only mint, balance-conserving transfers, immutable ledger |
| Paid API entitlement is not enforced | **P1** | Confirmed | Feature/revenue control bypass | Gate key creation and execution; suspend/revoke on plan change |
| Team authorization is not ready for shared workspaces | **P1** | Confirmed/dormant | Low-role user can manage sensitive workspace operations if team path is enabled | Central RBAC matrix and negative endpoint tests |
| JWT, refresh and impersonation tokens are in `localStorage`; SPA lacks strong CSP | **P2** | Confirmed | Any XSS becomes account/support takeover | HttpOnly refresh, in-memory access token, CSP/security headers |

Full required fields and source lines are in [CRITICAL_BUGS.md](CRITICAL_BUGS.md).

## Authentication and session security

### Implemented well

- DRF defaults to authenticated access.
- Custom JWT authentication checks that user/tenant state is still valid and rejects suspended/inactive contexts.
- Refresh/logout/password-reset paths and impersonation concepts exist.
- API keys are stored hashed, have prefixes for lookup, scopes, revocation, last-used tracking, and request logs.
- Bridge API calls use a bearer secret and callbacks use HMAC signatures; payment/webhook verification and deduplication primitives are present.

### Required changes

- Move refresh credentials to `HttpOnly`, `Secure`, appropriately `SameSite` cookies and keep short-lived access credentials in memory where feasible. Do not leave original support-impersonation credentials in script-readable storage (`frontend/src/context/AuthContext.jsx:107-161`; `frontend/src/services/api.js:11-86`).
- Add a strict SPA CSP, `frame-ancestors`, `X-Content-Type-Options`, `Referrer-Policy`, and a controlled script/style policy. `/media` has restrictive headers, but normal SPA responses do not have a comparable policy (`nginx/locations.conf`).
- Rotate/expire bridge secrets, webhook secrets and encryption keys with key identifiers and overlap; document emergency revocation. Avoid one static global bridge credential as the only service boundary.
- Rate-limit login, reset, QR creation/status, key creation, impersonation, exports and destructive requests with fail-safe behavior and alerts.
- Bind support impersonation to reason/ticket, short TTL, explicit customer-visible audit, no refresh token, and step-up authentication.

## Tenant isolation and authorization

The code generally derives tenant from the authenticated request and scopes core querysets. A central isolation suite covers ten families: contacts, groups, tags, WhatsApp accounts/devices, templates, campaigns, messages, conversations, API keys, and webhooks. Managed-user queryset/can-manage logic also shows care.

That evidence must not be overstated. The matrix permits 403/404/405 and does not enumerate AI, chatbot, knowledge, CRM, billing, credits, subscriptions, media library, voice, reports, support, audit, reseller, exports, scheduled jobs, or every custom action. Cross-table tenant consistency often lives only in serializers; admin/tasks/fixtures/future code can persist mismatched graphs.

Required release evidence:

1. Inventory every API route and custom action with allowed roles and tenant relationship.
2. For each object route, test own tenant, sibling tenant, reseller ancestor, unrelated reseller, platform admin, suspended user, and object-ID enumeration.
3. For nested relationships, reject cross-tenant IDs at serializer/service and add a database-level invariant where practical.
4. Test list filters, exports, reports, counts, search suggestions, files and error messages for existence leakage.
5. Test background tasks with immutable tenant IDs and re-fetch/scoping rather than trusting serialized model IDs.
6. Make workspace membership real before exposing invitations: user role, tenant kind, membership role and reseller authority need one explicit policy.

Tenant hierarchy itself needs invariants: legal parent-kind transitions, one platform root if that is the design, no self-parent/cycles, maximum depth, and cycle-safe traversal (`backend/apps/tenants/models.py:7-42`).

## API keys, public API, webhooks, and throttling

Positive controls include hashed/revocable keys, scopes, per-key throttles, request idempotency, HMAC outgoing webhooks, callback signatures, and dedup records. The public API is substantive rather than a placeholder.

Material gaps:

- `api_access` entitlement is disconnected from both developer-key CRUD and public endpoint execution.
- Throttling deliberately fails open when Redis is unavailable (`backend/apps/publicapi/throttling.py:14-26`). A cache outage can become an unlimited-request event precisely when the platform is degraded.
- QR callback transport has no durable retry. `get_or_create` is receipt deduplication but there is no separately retryable processing state.
- API completeness/versioning is partial; contact detail is read-only after create/upsert. Avoid promising full CRUD parity.
- OpenAPI generation produces hundreds of warnings in the active check. The hand-maintained/public spec may still work, but CI must prove documented authorization, examples, error schemas and actual routes match.

Required controls: fail-closed or bounded local emergency throttling, per-tenant global limits, request size limits, idempotency retention policy, signature timestamp/replay window, webhook secret rotation, delivery attempts UI, dead letters, replay, and abuse-rate alerts.

## Media, uploads, and SSRF

`backend/core/media.py` validates supported schemes and media shape, but permits internal destinations. URLField is not an SSRF defense. The bridge ultimately gives the URL to Baileys for a server-side fetch.

The safe architecture is:

1. Client uploads to a tenant-scoped quarantine object.
2. Server verifies extension, MIME sniff, decoded type, size/dimensions/duration, archive limits, and malware result.
3. Approved object receives an immutable ID and private storage key.
4. QR bridge receives bytes or a short-lived, single-purpose URL from a dedicated egress-controlled media service.
5. Recipient-public media and private knowledge documents use different buckets/policies.
6. Downloads use authorization or short-lived signatures; logs never include signed query strings.

If arbitrary external URLs remain, validate resolved addresses on every redirect and connection, block loopback/private/link-local/reserved/multicast/IPv6-mapped ranges and nonstandard ports, constrain response size/time/type, resist DNS rebinding, and place bridge egress behind network controls. Tests must include alternate numeric encodings, userinfo, redirects, mixed DNS answers and URL parser discrepancies.

There is filename/type/size normalization, but no antivirus or content-disarm pipeline. Treat knowledge documents as especially sensitive; they should never be reachable merely by possessing a permanent `/media` URL.

## QR credential and bridge security

The bridge uses Baileys `useMultiFileAuthState` in `qr-bridge/src/authState.ts:47`. The project documentation warns that this helper is not a production auth store, and Baileys treats these credentials as high-value secrets. See [Baileys session-management guidance](https://github.com/WhiskeySockets/docs/blob/main/authentication/session-management.mdx) and [security policy](https://github.com/WhiskeySockets/Baileys/security).

Required design:

- Encrypted, transactional credential/key store; envelope keys in an external secret/KMS system.
- Strict per-session tenant/account ownership; no session ID enumeration.
- One active owner lease with fencing token so two bridge instances cannot run the same auth state.
- Atomic credential revision writes; rollback/corruption detection; no partial JSON sets.
- Key rotation, break-glass revoke/logout, host-loss restore and mandatory-rescan states.
- Never log QR content, credentials, bearer tokens, message content or signed URLs.
- Network isolation: only Django reaches management API; bridge reaches only required WhatsApp endpoints plus controlled media/callback destinations.
- Supported dependency line and a staged protocol-compatibility upgrade process.

Installed Baileys 6.7.24 is above the fix for the named critical v6 advisory, and bridge production `npm audit` found zero known findings. That does not make the reverse-engineered protocol supported, nor does it protect against a server-side protocol change. Baileys currently identifies v7 as supported; see [Baileys security versions](https://github.com/WhiskeySockets/Baileys/security) and [GHSA-qvv5-jq5g-4cgg](https://github.com/advisories/GHSA-qvv5-jq5g-4cgg).

## Consent, privacy, and abuse security

Consent evidence, STOP/opt-out handling, quiet hours, warm-up, account limits and auto-pause are valuable. P0-06 proves the enforcement source is not durable across identity deletion. Fixing the one path is insufficient: normalize phone identity once and make a suppression registry mandatory in every send gate—campaign, inbox initiated send, automation, chatbot, AI, API and future features.

Privacy operations also need verified access/export/correction/deletion/retention flows for database rows, logs, backups, objects, QR state, embeddings and third-party AI providers. Deletion must not erase evidence legally required to prove suppression. Define purpose, retention and lawful basis by data class; do not send private knowledge/message content to an AI provider without configured disclosure, minimization and contractual controls.

## Credits, billing, reseller, and fraud controls

Row locks, reservations, consume/release mechanics, provider payment verification, webhook records and reconciliation are good foundations. The credit system nevertheless violates conservation: a reseller can choose a mint path. Establish invariants:

```text
platform mint authority is explicit and audited
reseller transfer = reseller debit + customer credit in one transaction
reserved >= 0
available >= 0
reserved <= total funded balance
every mutation has one immutable ledger cause/idempotency key
```

Test duplicate/reordered/delayed/forged payment webhooks, parallel checkout settlement, refund/chargeback, downgrade, expiry, credit reservation worker death, and reconciliation against processor totals. Recurring billing, dunning, proration, refunds, tax invoices and reseller revenue share are product gaps and must not be implied in contracts.

## Dependency and supply-chain status

- Frontend `npm audit --omit=dev`: zero known findings at audit time.
- QR bridge `npm audit --omit=dev`: zero known findings at audit time.
- `pip check`: passed dependency consistency.
- Python CVE audit: not performed because `pip-audit` was unavailable; this remains an open release item.
- Docker images, OS packages, GitHub Actions/CI permissions and SBOM/signing were not proven by the checks above.
- QR Dockerfile uses `npm install` instead of deterministic `npm ci`; `certbot:latest` is unpinned.

Release gate: locked hashes where supported, Renovate/Dependabot policy, OS/container scan, Python/Node audit, SBOM, secrets scan, signed images/provenance, pinned production images, and a time-bounded patch SLA. Treat an unsupported Baileys major line as a release failure even if npm CVEs are zero.

## Secrets and logging

No committed `.env` secret was identified in the audit status, and production configuration contains useful guards. Remaining work:

- Inventory every secret, owner, store, rotation period and dependent service.
- Store production secrets outside the repository/Compose file; require length/uniqueness checks.
- Back up encryption material separately from encrypted data with dual-control recovery.
- Add structured redaction tests for authorization headers, API keys, QR values, phones, content, signed URLs and provider responses.
- Configure Docker log rotation/central collection, access controls, tamper-resistant audit retention and alerting.
- Do not log raw third-party errors blindly; provider bodies can contain customer data or tokens.

## Security acceptance checklist

- [ ] P0-05 SSRF suite passes at application and network layers.
- [ ] P0-06 suppression cannot be bypassed through any identity lifecycle/API/import path.
- [ ] Every route/action has an automated tenant/role matrix.
- [ ] Credit conservation and payment idempotency hold under concurrency.
- [ ] Private media/knowledge URLs expire and require authorization.
- [ ] Browser refresh/impersonation credentials are not script-readable; CSP blocks unauthorized script.
- [ ] QR credentials are encrypted, owned/fenced, restorable and revocable.
- [ ] Incoming/outgoing webhook deliveries are durable, replay-safe and observable.
- [ ] Dependency/container/SBOM/secrets scans are clean or have approved time-bounded exceptions.
- [ ] Clean-host restore preserves authorization, media, sessions or explicit reauth state, and audit trails.
- [ ] External penetration test covers IDOR, role escalation, SSRF, file processing, webhook replay and business-logic abuse.
- [ ] Legal/privacy review approves disclosures, retention, subprocessors and the unofficial QR integration positioning.

Until all P0 and relevant P1 items pass, the platform should be reachable only by trusted development/evaluation tenants with no valuable production data.

