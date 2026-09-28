# Security notes

What this deployment holds, where it holds it, and the decisions behind how it
is protected. Written for whoever operates it, and for whoever picks up the next
phase of hardening.

---

## Secrets

| Secret | Where it lives | Rotate by |
| --- | --- | --- |
| `SECRET_KEY` | `.env`, `backend/.env` | `python scripts/rotate_secrets.py --only SECRET_KEY` |
| `JWT_SECRET` | `.env`, `backend/.env` | `scripts/rotate_secrets.py` — every session ends |
| `DB_PASSWORD` | `.env`, `backend/.env` | `scripts/rotate_secrets.py`, **then** `ALTER ROLE` (the script prints the command) |
| `REDIS_PASSWORD` | `.env`, `backend/.env` | `scripts/rotate_secrets.py` |
| `QR_PROVIDER_API_KEY` / `..._WEBHOOK_SECRET` | `.env`, `backend/.env`, `qr-bridge/.env` | `scripts/rotate_secrets.py` — all three copies must match |
| `WHATSAPP_TOKEN_ENCRYPTION_KEY` | `.env`, `backend/.env` | **Not** a file edit — see below |
| Provider keys (`OPENAI_API_KEY`, SMTP, Twilio) | `.env`, or per tenant, encrypted | The provider's own console |

`WHATSAPP_TOKEN_ENCRYPTION_KEY` is the Fernet key that stored WhatsApp provider
tokens and tenants' own AI API keys are encrypted **with**. Replacing the value
does not re-encrypt anything; it makes every stored credential permanently
unreadable. Rotating it is a migration — decrypt with the old key, re-encrypt
with the new, in one pass — and `scripts/rotate_secrets.py` refuses to touch it
for that reason.

### Rules

* No `.env`, `.env.*`, `*.bak`, `*.pem` or `*.key` may be committed or reach a
  Docker build context. `.gitignore`, `.dockerignore` and `backend/.dockerignore`
  all enforce this, and `backend/tests/test_deployment_security.py` asserts that
  they do. `.env.example` is the one tracked environment file and holds no values.
* A build context is not undoable. Anything present when an image is built is
  in a layer, and deleting the file afterwards does not remove it from the image.
* Nothing may print a secret. `scripts/rotate_secrets.py` reports only which
  names it changed and in which files.

---

## Network exposure

Only nginx publishes a port on all interfaces. Everything else is either
loopback-only or reachable solely over the internal Docker network:

| Service | Published | Reached internally as |
| --- | --- | --- |
| nginx | `${HTTP_PORT:-80}:80` | — |
| postgres | `127.0.0.1:5432` (developer convenience) | `postgres:5432` |
| redis | nothing | `redis:6379` |
| qr-bridge | `127.0.0.1:4001` (developer convenience) | `qr-bridge:4001` |
| backend / celery | nothing | `backend:8000` |

`"5432:5432"` in Docker means `0.0.0.0:5432`. On an internet-facing host that
is a database anybody may try passwords against; the two loopback bindings above
exist only so a developer can run `manage.py test` or point a `runserver`
outside Docker at the bridge, and can be deleted outright in production.

Redis is additionally password-protected (`--requirepass`). The application
splices `REDIS_PASSWORD` into the connection URL itself, URL-quoting it, so a
generated password containing `@`, `:` or `/` cannot silently produce a URL
pointing somewhere else.

---

## Uploaded files

`/media/` is served straight off disk by nginx, which means it never passes
through Django's `SecurityMiddleware`. An uploaded file a browser is willing to
treat as a document, served from this origin, is stored XSS against every
logged-in user. Two independent layers stop that:

1. **`core/uploads.py`** — an extension allowlist, plus a signature check on the
   file's leading bytes. The browser's `Content-Type` and the name on the file
   are both chosen by the uploader and decide nothing. `.html`, `.svg`, `.js`
   and everything else not on the list are refused, and so is a `.png` whose
   bytes are HTML. The stored path is a fresh UUID plus the extension the
   validator derived, so nothing an uploader writes reaches the filesystem.
2. **`nginx/nginx.conf`** — `X-Content-Type-Options: nosniff`,
   `Content-Security-Policy: default-src 'none'; sandbox`, `Referrer-Policy`,
   `X-Frame-Options`, and `Content-Disposition: attachment` on everything except
   an explicit list of image/video/audio extensions, which are served `inline`
   because the inbox renders them.

Both, because either alone is a single point of failure — and because the media
volume still contains files uploaded before layer 1 existed.

---

## Authentication

* Login, password-reset request and password-reset confirm all use
  `ScopedRateThrottle` with the `auth` scope (10/min by default). Before this,
  `THROTTLE_RATE_AUTH` was configured and nothing used it: login fell back to
  the anonymous rate.
* `core/lockout.py` counts failed attempts **per account**, not per client, so
  rotating IP addresses does not reset the budget. An unknown username is
  counted exactly like a real one — otherwise the lockout would itself be an
  account-existence oracle. It fails open when Redis is down, on the grounds
  that locking every account out of a running system is a worse outcome than a
  temporarily weaker rate limit.
* The Django admin shares that counter (`core/middleware.AdminSecurityMiddleware`)
  so the two login forms are not two separate budgets, and nginx rate-limits
  `/admin/` at the edge. `ADMIN_ALLOWED_IPS` optionally restricts it to named
  addresses, answering everyone else with a 404 rather than a 403.
* `POST /api/v1/auth/logout/` blacklists the refresh token it is given.
  Clearing browser storage alone left that token valid for its full lifetime.

### Password recovery (Phase 3)

* `PASSWORD_RESET_TIMEOUT` is **one hour**, not Django's three-day default. A
  reset link is a full account takeover for anybody who can read the mailbox,
  and it sits in that mailbox — and in a mail provider's logs, and in any
  forwarded copy — for the whole of its validity.
* Tokens are Django's `default_token_generator`, which hashes the user's
  current password hash into the token. Using a link therefore invalidates it,
  and so does any later password change — single use, with no token table to
  keep in step.
* The request endpoint answers with **one constant sentence** whether or not
  the address is registered, and the per-address limit below does not change
  that response. A limit that produced a different answer would be the
  enumeration oracle the constant response exists to prevent.
* Two throttles on two axes, because they stop two different attacks. The
  `auth` scope counts per client address and stops one machine hammering the
  endpoint; `PASSWORD_RESET_MAX_PER_EMAIL` counts per **mailbox** and is the
  only one that stops somebody with a proxy pool filling one person's inbox.
* **A completed reset revokes every outstanding refresh token**
  (`apps/accounts/sessions.py`). Without this, a token stolen before the reset
  kept minting 15-minute access tokens for its full seven-day life — so
  resetting a password *because* it was stolen accomplished nothing. Access
  tokens already issued stay valid until they expire; that 15-minute window is
  the honest remaining exposure, and closing it would mean a blacklist lookup
  on every API call.
* `POST /api/v1/auth/password-change/` requires the **current** password even
  though the caller is authenticated. An access token found on an unlocked
  laptop is enough to reach that endpoint and must not be enough to take the
  account. It revokes sessions the same way.
* Both paths send a "your password was changed" notice. It carries no
  password, no hash and no token, and a mail failure never fails the change —
  the password is already changed, and refusing to say so would leave somebody
  unable to sign in with either one.

### Workspace suspension (Phase 3)

`Tenant.is_active` existed and was enforced nowhere. It is now checked in three
places, because a signed JWT stays valid for its whole lifetime whatever
happens afterwards:

* at sign-in, refused with a sentence rather than a silent broken session;
* in `SendStackJWTAuthentication`, so an existing token stops working on the
  next request rather than up to fifteen minutes later — and cannot be renewed;
* in the send gate, so queued and scheduled messages stop.

It deletes nothing, and it is deliberately separate from `user.is_active`,
which is a different decision about one account.

---

## QR bridge session storage

**This is the one item Phase 1 documents rather than fully solves.**

### What is stored

`qr-bridge` keeps one directory per session under its `qr_bridge_data` volume,
written by Baileys' `useMultiFileAuthState`:

| File | Contents |
| --- | --- |
| `creds.json` | The device's **Noise and Signal identity keys**, registration id, the linked phone number and push name, and the advertised-account signature |
| `pre-key-*.json` | One-time Signal pre-keys |
| `session-*.json` | Per-contact Signal session state (ratchet keys, chain counters) |
| `sender-key-*.json`, `app-state-sync-*.json` | Group sender keys and synced app state |
| `meta.json` | This service's own record: account id and the two webhook URLs. No credentials. |

**Sensitivity: equivalent to the phone.** Whoever can read `creds.json` can
bring up a socket as that WhatsApp number — send as it, read its messages, and
be indistinguishable from the linked device. It is the most sensitive data in
the deployment, and unlike the WhatsApp Cloud API tokens (encrypted at rest with
`WHATSAPP_TOKEN_ENCRYPTION_KEY`) it is stored in plaintext JSON.

### What protects it today

* A Docker **named volume**, not a bind mount. It is not inside any build
  context, not in the repository, and not reachable over the network.
* The container **publishes no public port** — `127.0.0.1:4001` only — and every
  route except `/health` requires `Authorization: Bearer $QR_PROVIDER_API_KEY`,
  compared in constant time.
* The container runs as the unprivileged `app` user (`qr-bridge/Dockerfile`),
  and the data directory is created **0700** (`qr-bridge/src/sessionStore.ts`).
* Baileys' own logger is pinned to `warn` and configured separately from the
  service's (`QR_BRIDGE_BAILEYS_LOG_LEVEL`). At `debug`/`trace` Baileys prints
  the Signal payloads it handles, including key material; wiring it to
  `QR_BRIDGE_LOG_LEVEL` meant that turning logging up to diagnose an unrelated
  problem would write those keys to container stdout.
* A session's directory is deleted on expiry, disconnect and logout
  (`removePersisted` / `forgetAuthState`).

### Backups

**Exclude the `qr_bridge_data` volume from any backup that is not itself
encrypted.** A tarball of it is a set of working WhatsApp identities. If it must
be backed up (to survive a host loss without re-scanning every number),
encrypt the archive and hold the key somewhere other than the backup target.

### Why it is not encrypted at rest yet — Phase 2/3

Encrypting it means intercepting every read and write inside
`useMultiFileAuthState`, which sits on the hot path of every message: libsignal
does load → mutate → store per message, and its mutex guards one file operation
at a time rather than the whole sequence. This is exactly the seam that produced
the session-corruption bug the current `authState.ts` exists to fix — divergent
in-memory creds across two sockets, messages encrypted against a ratchet the
peer had never seen, and a permanent "Waiting for this message" at the other
end. Adding a crypto layer there without a re-derivation and migration path for
existing sessions risks reintroducing exactly that, for numbers that are live.

The honest sequencing is: encrypted-at-rest auth state with a key derived from
`WHATSAPP_TOKEN_ENCRYPTION_KEY`, a migration that re-writes existing session
directories in place, and a test that a session survives a restart — as its own
change, not folded into a hardening pass.

**Residual risk until then:** an attacker who obtains host filesystem access, a
copy of the Docker volume, or an unencrypted backup can impersonate every
QR-linked WhatsApp number. Host access is the precondition in each case.

---

## Public developer API

The API external software calls with an API key (`apps/publicapi`, mounted at
`/api/public/v1/`). See `docs/PUBLIC_API.md` for the architecture; what follows
is only the security posture.

**Credentials at rest.** An API key is stored as a clear 8-hex prefix plus a
SHA-256 hash of a 128-bit `secrets.token_hex` secret. The full key exists once,
in the response that created it; no code path can reproduce it. SHA-256 rather
than Argon2 is deliberate — a slow KDF exists to make guessing a *low-entropy
human-chosen* string expensive, buys nothing against 128 bits from a CSPRNG,
and would cost ~50 ms of CPU on every API request. Webhook signing secrets are
**encrypted** rather than hashed, because the HMAC has to be recomputed on
every delivery; they use the same Fernet key
(`WHATSAPP_TOKEN_ENCRYPTION_KEY`) as the provider credentials, so there is one
secret-at-rest story rather than two. A webhook secret is likewise never
readable back through any endpoint.

**No session or cookie authentication.** A public view lists
`ApiKeyAuthentication` and nothing else, so a browser session — including an
admin's — cannot reach these endpoints and they inherit none of the SPA's CSRF
surface. Conversely an API key cannot reach `/api/v1/developer/`: a key able to
mint another key would make revoking one pointless.

**Default deny on scopes.** A view declares `required_scopes`; a view declaring
*nothing* is refused rather than allowed (`permissions.HasApiScope`). On a
platform where forgetting a decorator would otherwise publish an endpoint, the
forgetting is the vulnerability.

**Tenant isolation is applied in the query, not checked after the fetch.** A
miss is therefore indistinguishable from "does not exist": another workspace's
object is a 404, never a 403, so the API is not an existence oracle for ids.
The same reasoning covers key authentication — an unknown prefix and a wrong
secret both return `INVALID_API_KEY`, and the secret comparison is
`hmac.compare_digest`.

**A suspended workspace loses access on its next request.** API keys do not
expire on their own, so without this check a suspended tenant would keep full
send access indefinitely. This mirrors `SendStackJWTAuthentication`.

**Webhook signatures cover the timestamp.** The HMAC is over
`"{timestamp}.{raw body}"`, not the body alone. Signing the body only would let
anyone replaying a captured delivery rewrite the timestamp header to *now* and
still verify, making the five-minute replay window decorative.

**Webhook URLs are SSRF-checked twice** — at write time so a bad URL never
exists to be retried, and again immediately before each POST, which is the
check that catches a hostname that resolved publicly when it was saved and
resolves to `169.254.169.254` today. Both use `core/outbound_urls.py`, the same
guard the AI agent's `base_url` uses. Redirects are never followed: a redirect
would step straight around that guard.

**HTTPS is required by default** (`not DEBUG`) for both the API and webhook
URLs. The API key travels in a header on every request, so an integration
misconfigured with an `http://` base URL leaks its credential to every hop on
the path, repeatedly, with nothing in the response to say so — refusing is the
only signal that reaches the developer. Webhook bodies carry message content
and phone numbers and are signed with a shared secret; plaintext exposes all
three.

**Uploads go through `core/uploads.validate_upload`**, the same two-layer check
the dashboard uses: the stored filename is ours, and the content type is
derived from the bytes rather than believed from the request.

**What the request log stores.** One row per call: key prefix, method, path,
status, our own error code, IP, user agent, request id, duration. Never a
request body — on this API that would be a list of phone numbers and the text
being sent to them — and never a header. `WebhookDelivery.response_snippet` is
capped at 300 characters and holds only what the customer's *own* endpoint
returned. Audit events for key and webhook operations record the prefix and the
URL, never the secret.

**Never logged anywhere:** the `Authorization` header, a full API key, a webhook
secret, Baileys auth state, Signal keys.

**Rate limiting fails open.** The three per-key buckets live in Redis with
`IGNORE_EXCEPTIONS`, so a cache outage allows requests through — to the credit
gate, the plan allowance, the per-number sending limits and the compliance
gate, all of which are in PostgreSQL and none of which this layer can bypass.
Failing closed would turn a cache blip into a platform outage.

**The compliance gate is not bypassable from the API.** An API send resolves
the recipient to a `Contact` row precisely so consent, opt-out and quiet hours
apply — a send with no contact attached would silently skip the opt-out check.
`kind: "service"` exempts a message from quiet hours and from nothing else; no
value of any API field can message somebody who has unsubscribed.

---

## Transport

Production defaults to `SECURE_SSL_REDIRECT=True`, with HSTS (1 year,
subdomains, preload), secure + HttpOnly session and CSRF cookies,
`X-Frame-Options: DENY`, nosniff, and `strict-origin-when-cross-origin`.
`SECURE_PROXY_SSL_HEADER` trusts nginx's `X-Forwarded-Proto`, which is safe here
because the backend container publishes no port and nginx is the only thing that
can reach it.

`SECURE_REDIRECT_EXEMPT` covers `^health/` so the containers' own healthchecks —
which curl themselves over plain http on localhost — are not redirected to a
port that is not open inside the container.

Set `SECURE_SSL_REDIRECT=false` **only** while nothing terminates TLS in front
of nginx. Delete the line the moment a certificate is in place.

---

## Logs and error reports (Phase 3)

Logs are shipped, aggregated and searched by more people than the database is,
so they are treated as a lower-trust destination.

* **Never logged:** passwords, password hashes, JWTs, refresh tokens,
  password-reset tokens, API keys, WhatsApp access tokens, the encryption key,
  webhook secrets.
* **Phone numbers are masked in production** (`+91******3210`) by
  `core.logging_filters.PhoneMaskingFilter`, applied as a log filter rather
  than at each call site — a call site is a thing somebody adds later without
  remembering the rule. Off in development, where the full number is usually
  what is being debugged.
* Message bodies are never logged. Send lines carry ids and outcomes.
* **Error monitoring** (`core/monitoring.py`, optional, off without
  `SENTRY_DSN`) is shaped in the opposite direction from the library defaults,
  which send request bodies, headers, cookies and stack-frame locals. PII is
  off, request bodies never, and `before_send` runs last: credentials are
  removed **by key name** — a secret is recognisable by what it is called, not
  by what it looks like — customer content is removed, phone numbers are
  masked, and the user block is reduced to an id. A scrubber that raises drops
  the event rather than shipping it unscrubbed.
* Correlation ids cross the queue boundary (`config/celery.py`), so a request
  can be followed from the browser through Django into a worker minutes later.

---

## Backups (Phase 3)

A Docker volume is not a backup: it is on the same disk, in the same machine,
in the same failure domain as the database it copies.

`scripts/backup_postgres.sh` writes a timestamped `pg_dump`, verifies it is
readable before counting it, encrypts it with AES-256 when `BACKUP_PASSPHRASE`
is set, and prunes past `BACKUP_RETENTION_DAYS`. The directory is mode 700 and
each archive 600, and `backups/`, `*.dump*` and `*.sql.gz` are excluded from
Git and from both Docker build contexts.

`scripts/restore_postgres.sh` restores into a **throwaway** database by
default, runs verification queries, and drops it. Overwriting the live database
needs `--target` *and* `--i-understand-this-overwrites`.

Two things this does not do, both documented rather than hidden: it does not
copy archives off the host (the script says so on every run until
`BACKUP_OFFSITE_ACKNOWLEDGED` is set), and it does not schedule itself — see
`docs/DISASTER_RECOVERY.md`.

---

## Configuration checking

`manage.py check_production` reports PASS / WARNING / FAIL for the settings
that decide whether customer data is safe, and **never prints a value**: its
output goes into deploy logs, CI and screenshots in support tickets. It exits
non-zero on any FAIL, so a pipeline can simply run it.

Production settings additionally refuse to start on a console email backend, an
incomplete SMTP configuration, or a `FRONTEND_URL` pointing at localhost — each
of which silently breaks password recovery in a way nobody notices until a
customer is locked out.
