# The public developer API

Operator and maintainer notes for `backend/apps/publicapi`. Customer-facing
reference lives at `/api/public/v1/docs`; this file is about how the thing is
built and what to do when it misbehaves.

## What it is

A façade. It owns authentication, scopes, rate limits, idempotency, the error
envelope, outbound webhooks and its own access log — and owns no messaging
logic at all. A message sent through the API lands in the same `Message` row,
goes through the same `apps.messaging.tasks.send_message_task`, is evaluated by
the same `apps.messaging.gating`, and debits the same credit ledger as a reply
typed into the inbox.

That is the property to defend when changing anything here. If a future change
makes the API send differently from the dashboard, the API is wrong.

```
External software
      │  Authorization: Bearer ss_live_…
      ▼
apps.publicapi              ← authentication, scopes, rate limits,
      │                        idempotency, validation, tenant isolation
      ▼
existing services           ← campaigns, templates, credits, contacts
      │
      ▼
apps.messaging.send_message_task
      │                     ← gating: consent, quiet hours, plan, limits
      ▼
apps.whatsapp.providers     ← QR bridge / Cloud API, chosen internally
      ▼
   WhatsApp
```

Inbound runs the other way: the QR bridge or Meta posts to the existing webhook
endpoints, `apps.inbox` records the message and resolves any button reply, and
`apps.publicapi.receivers` turns the resulting signal into a webhook delivery.

## Mount points

| Path | Auth | For |
|---|---|---|
| `/api/public/v1/…` | API key only | external software |
| `/api/v1/developer/…` | JWT only | the Settings → Developer screens |
| `/api/public/v1/docs` | none | the reference page |
| `/api/public/v1/openapi.json` | none | the OpenAPI 3.1 document |

The two authenticated prefixes are disjoint by design. An API key cannot reach
`/api/v1/developer/`, because a key able to mint another key would make
revoking one pointless.

**Why not `/api/v1/`.** That prefix is the SPA's, and `campaigns`, `templates`,
`contacts`, `media` and `reports` are all already taken there. To expose the
public API on a cosmetic `https://api.<domain>/v1/*`, map it in nginx:

```nginx
server {
    server_name api.example.com;
    location /v1/ { proxy_pass http://backend/api/public/v1/; }
}
```

No Python changes are needed, and the internal path stays greppable in logs.

## Keys

Shape: `ss_live_<8 hex prefix><32 hex secret>`, or `ss_test_` for a key the send
endpoints refuse.

Stored: the prefix in clear (indexed, and what a lookup runs on) plus a SHA-256
hash of the secret. **The full key exists once**, in the response that created
it, and no code path in this app can produce it again.

SHA-256 rather than Argon2 is deliberate and is explained at length in
`keys.py`: the secret is 128 bits from a CSPRNG, so a slow KDF buys nothing
against guessing while costing ~50 ms of CPU on *every API request*.

Three distinct refusals, because the fix differs for each:

| State | Code | Reversible |
|---|---|---|
| `is_active = False` | `DISABLED_API_KEY` | yes |
| `revoked_at` set | `REVOKED_API_KEY` | never |
| `expires_at` passed | `EXPIRED_API_KEY` | no, but nobody did it |

Rotation issues a new row and revokes the old one **immediately, with no grace
period**. Rotation is what somebody does when they think a key leaked; an hour
of overlap is an hour of access granted to whoever leaked it. Overlap is
achieved by creating a second key, migrating, then revoking the first.

## Rate limits

Three buckets, per key, fixed windows in Redis.

| Bucket | Charged by | Protects |
|---|---|---|
| `requests_per_minute` | every call | the web tier |
| `messages_per_minute` | sends; campaign starts by recipient count | the Celery workers |
| `campaigns_per_day` | campaign creation and starts | the customer, from a loop |

`0` on an `ApiKey` means **"use the deployment default"**, not "unlimited" —
the opposite of the plan-quota convention, because a forgotten rate limit must
not become no rate limit.

Redis is **not** the authority on whether a send may happen. `django_redis` runs
with `IGNORE_EXCEPTIONS`, so a Redis outage makes these counters no-op and
every request is allowed through — to the credit gate, the plan allowance and
the per-number limits, all of which live in PostgreSQL. Rate limiting failing
open degrades fairness; failing closed would take the platform down on a cache
blip.

## Webhooks

At-least-once. Signature over `"{unix timestamp}.{raw body}"`, HMAC-SHA256,
sent as `X-SendStack-Signature: sha256=<hex>`.

**The timestamp is inside the signed string, not merely beside it.** If it were
only a header, a replayed delivery could have its timestamp rewritten to *now*
and would still verify, making the five-minute replay window decorative.

Retry ladder: 5 attempts, jittered exponential backoff from 10 s. Any 2xx is
success. A 4xx other than 408/429 is terminal — a receiver answering 404 will
answer 404 again, and retrying hides a wrong URL behind five identical
rejections. An endpoint that fails 20 deliveries in a row is disabled, visibly.

The URL is checked against `core.outbound_urls` (the same SSRF guard the AI
agent's `base_url` uses) **twice**: when it is saved, and again immediately
before each POST. The second check is what catches a hostname that resolved
publicly last week and resolves to `127.0.0.1` today. Redirects are never
followed, for the same reason.

`button.clicked` is built from the `Message` row the existing inbound path
already resolved. There is one button parser in this codebase and it is in
`apps.automation.services`; this app reads its result. A tap on a Cloud API
button and a "2" typed at a QR-linked number therefore reach an integration
identically, with `interaction_type` distinguishing them.

## Signals added to existing apps

Three additive emissions were added so this app could observe the pipeline
without polling. Nothing else about them changed.

| Signal | Fired from | Why not `post_save` |
|---|---|---|
| `messaging.signals.message_status_changed` | `messaging.receivers`, `messaging.services.mark_skipped`, `messaging.tasks` | `mark_skipped` writes with `.update()`, which fires no model signal |
| `campaigns.signals.campaign_status_changed` | `campaigns.services.launch`/`cancel`, `messaging.services.refresh_campaign_counters` | completion is a `.update()` guarded on `status=RUNNING`, so two workers finishing the last two messages produce one event |
| — (uses `pre_save`/`post_save`) | `apps.whatsapp.WhatsAppAccount` | every status write already goes through `save()`, and there are twenty-odd of them |

## Operations

**Beat tasks**

| Task | Default | What breaks without it |
|---|---|---|
| `publicapi.tasks.sweep_pending_webhooks` | 60 s | deliveries written inside an open transaction are never sent — delivery drops to best-effort |
| `publicapi.tasks.prune_public_api_data` | 6 h | `ApiRequestLog`, `WebhookDelivery` and expired idempotency rows grow without bound |

**Turning webhooks off.** `PUBLIC_API_WEBHOOKS_ENABLED=false` makes emission a
no-op and changes nothing else. Use it when egress is down and the delivery
queue would otherwise fill with doomed retries.

**A customer says their integration stopped working.** Ask for the key prefix
(`ss_live_7f3a91c2…` — safe to share) and read `ApiRequestLog` for it:

```python
ApiRequestLog.objects.filter(key_prefix="7f3a91c2").order_by("-created_at")[:50]
```

`error_code` is the fixed vocabulary from `errors.py`; `request_id` correlates
with the application log. If they quote an `X-Request-Id` from a response, grep
for that.

**A customer says webhooks stopped.** Check the endpoint's
`consecutive_failures` and `auto_disabled_at`. An auto-disabled endpoint is
re-enabled from the dashboard, which also clears the breaker.

## What this app must never do

- store a raw API key or return one twice;
- log the `Authorization` header, a full key, a webhook secret, or any provider
  credential;
- expose `access_token_encrypted`, `app_secret_encrypted`,
  `webhook_verify_token`, `provider_session_id`, Baileys auth state or Signal
  keys — `serializers.serialize_account` is a hand-written allowlist precisely
  so the next credential column added to `WhatsAppAccount` is not published by
  accident;
- resolve an object without a tenant filter *in the query*;
- grow its own send path, credit arithmetic, consent check or template
  renderer.

## Tests

```
manage.py test apps.publicapi
```

`tests.py` keys/auth/scopes/isolation · `tests_sending.py` send, templates,
campaigns, idempotency, rate limits · `tests_webhooks.py` signing, emission,
delivery, retries · `tests_dashboard.py` the Settings → Developer endpoints.
