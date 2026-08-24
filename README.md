# MultiSender

Multi-tenant WhatsApp business messaging and automation platform: campaigns, contacts,
automated replies/workflows, a shared inbox, and a credit-based billing ledger, built on
the official WhatsApp Business Cloud API (no unofficial/reverse-engineered client).

## Status

Backend: Django REST API covering accounts/tenancy/impersonation, WhatsApp account
connections, contacts, templates, campaigns, messaging, credits, inbox, automation, and
reports — see `backend/apps/`. Frontend: a React + Tailwind app with a page for every one
of those domains — dashboard, reports, campaigns (list, wizard, detail), inbox, automation,
templates, contacts, devices, credits, users and settings — sharing one set of UI
primitives in `frontend/src/components/ui/`. See `/api/docs/` for the live OpenAPI schema
once the backend is running.

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env: SECRET_KEY, DB_PASSWORD, ALLOWED_HOSTS, WHATSAPP_TOKEN_ENCRYPTION_KEY at minimum

docker compose up -d --build
```

This starts Postgres, Redis, the Django API (gunicorn), a Celery worker, Celery beat, and
an nginx container that serves the built frontend and reverse-proxies `/api/`, `/admin/`,
and `/health/` to the backend. The backend container runs migrations automatically on
startup. Visit `http://localhost/` for the app, `http://localhost/admin/` for the Django
admin, and `http://localhost/api/docs/` for the API reference.

Generate the required `WHATSAPP_TOKEN_ENCRYPTION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Known issue on Windows + OneDrive:** if `docker compose build` fails with
`invalid file request apps/__init__.py`, that's a Docker Desktop BuildKit bug triggered by
building from inside a OneDrive-synced folder, not a problem with this repository. Work
around it with the legacy builder:

```bash
DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 docker compose build
```

**TLS:** the bundled nginx config does not terminate TLS. Put a real certificate in front
of it (a managed load balancer, Cloudflare, or nginx + certbot) before setting
`SECURE_SSL_REDIRECT=true` — turning that on without TLS in front will redirect every
request to an https port nothing is listening on.

## Local development (without Docker)

Backend:

```bash
cd backend
python -m venv ../.venv && ../.venv/Scripts/activate  # or source ../.venv/bin/activate
pip install -r requirements/development.txt
cp .env.example .env  # fill in DB_PASSWORD and WHATSAPP_TOKEN_ENCRYPTION_KEY

# Postgres and Redis must be running locally, e.g.:
docker run -d --name multisender-redis -p 6379:6379 redis:7-alpine

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Celery (in separate terminals, needed for campaigns/automation/follow-ups to actually run):

```bash
celery -A config worker --loglevel=info
celery -A config beat --loglevel=info
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Tests: `python manage.py test apps` (196 tests as of this writing; runs against an
in-memory cache and eager Celery execution automatically, so neither Redis nor a worker
needs to be running for the suite to pass).

## Architecture

- `backend/config/settings/{base,development,production}.py` — environment-specific
  Django settings; `DJANGO_SETTINGS_MODULE` selects which one loads.
- `backend/apps/` — one Django app per domain (tenants, accounts, whatsapp, contacts,
  templates_app, campaigns, messaging, credits, inbox, automation, reports, audit,
  health). Cross-app coupling is deliberately one-directional where possible and uses
  Django signals at the boundaries that would otherwise create cycles (e.g.
  `apps.whatsapp.signals.webhook_event_stored` lets `inbox` and `messaging` react to
  inbound WhatsApp activity without `whatsapp` knowing either app exists).
- `backend/apps/whatsapp/providers/` — the only supported integration is the official
  WhatsApp Business Cloud API (`cloud_api.py`). `connections/qr.py` is an intentionally
  unimplemented extension point, not a stub pretending to work — see its docstring.
  Provider credentials (access tokens, app secrets) are encrypted at rest
  (`connections/session.py`, Fernet, key from `WHATSAPP_TOKEN_ENCRYPTION_KEY`).
  Webhook events are deduplicated and processed idempotently.
- `backend/apps/credits/` — an append-only, row-locked ledger (`CreditTransaction`).
  Campaign launches and inbox replies reserve credits up front; a successful send
  consumes the reservation, a permanent failure releases it. Nothing mutates a balance
  outside this ledger.
- `backend/apps/messaging/` — the single Celery-backed send pipeline (`send_message_task`)
  used by campaigns, inbox replies, and automation alike: atomic claim-before-send,
  exponential backoff retry, credit gating.
- `backend/apps/automation/` — welcome messages, keyword auto-replies, button-tap replies,
  follow-ups, and a validated JSON workflow graph (`TRIGGER`/`MESSAGE`/`WAIT`/`CONDITION`/
  `TAG`/`ASSIGN_AGENT`/`END` nodes) executed via Celery, with `WAIT` nodes pausing and
  resuming asynchronously.
- `backend/apps/voice/` — the AI calling agent: outbound and inbound phone calls the
  model actually holds a conversation on. Carrier access is behind a provider
  abstraction (`providers/base.py`) exactly like the WhatsApp one, with Twilio
  Programmable Voice as the implementation; the engine (`services.py`) speaks only in
  provider-neutral directives (Speak/Listen/Transfer/Hangup) and never sees TwiML.
  `agent.py` holds the prompt and the capped tool loop, `tools.py` the six things the
  agent may actually do on a live call. Carrier credentials are encrypted at rest with
  the same Fernet key as the WhatsApp ones.
- `core/` — cross-cutting infrastructure: request-ID middleware/logging, the shared API
  exception handler, CSV streaming helper, and the shared message-content schemas
  (`media.py` for attachments, `buttons.py` for interactive buttons).

## Interactive buttons

Templates, campaigns, automation rules, workflow `MESSAGE` nodes and inbox replies can all
carry a `buttons` list, which turns the send into a WhatsApp *interactive* message. The two
shapes cannot be mixed on one message, because WhatsApp sends them as different interactive
types: **up to three reply buttons**, or **exactly one link button**. Labels are capped at
20 characters and the message needs body text for the buttons to sit under. All of this is
enforced on save (`core/buttons.py`) rather than at send time.

Tapping a reply button sends the button's `id` back, which is what makes buttons drive
automation:

- an automation rule with the **Button tapped** trigger answers a specific `button_id`, and
  takes precedence over keyword rules so one tap never draws two replies;
- a workflow `CONDITION` on `{"field": "button_clicked", "value": "<id>"}` branches on which
  button was tapped, and a tap wakes a run that is still sitting in a `WAIT` rather than
  making the contact wait it out.

A reply button's id is derived from its label when first added and then left alone —
renaming the label afterwards must not unhook the rules pointing at it.

Both connection types send buttons: the Cloud API has interactive messages natively, and
the QR bridge relays them to Baileys as a *native-flow* interactive message (see
`sendInteractive()` in `qr-bridge/src/providers/baileysProvider.ts`). Native flow is used
rather than Baileys' older `buttonsMessage` because current WhatsApp clients no longer
render the legacy shape — it arrives as plain text with the buttons missing. Button
rendering on QR/session accounts is not a WhatsApp-supported guarantee and can vary with
the recipient's client version; Cloud API accounts are the officially supported path.

Should a provider ever report no interactive support, the message still goes out with the
choices written underneath as a numbered list rather than failing or silently dropping
them.

## Sender health

A WhatsApp number that suddenly sends far more than it ever has is what gets a number
banned, and a banned number costs the customer their whole channel — not just the campaign
that triggered it. Every account therefore has a ceiling and a health score
(`backend/apps/whatsapp/limits.py`, `health.py`), surfaced per device on the Devices page.

- **Daily and hourly limits.** Deployment-wide defaults (`WHATSAPP_DAILY_SEND_LIMIT`,
  `WHATSAPP_HOURLY_SEND_LIMIT`; `0` means no limit) with an optional per-number override.
  Where both exist the *lower* applies. Slots are reserved by locking a counter row, so two
  workers cannot both be told they have the last one.
- **Warmup.** A newly connected QR number is walked up to its ceiling over its first days
  (`WHATSAPP_WARMUP_LADDER`, default `50,100,250,500,750`). The clock starts at the number's
  **first send**, not at when the policy row was created, so numbers with existing history
  are already past it. Cloud API numbers never ramp — Meta meters those itself, in tiers
  tied to the business's quality rating.
- **Automatic pause.** If an unusual share of recent sends fail
  (`WHATSAPP_FAILURE_RATE_THRESHOLD` over `WHATSAPP_FAILURE_SAMPLE_MIN` messages), the
  number stops sending for `WHATSAPP_AUTO_PAUSE_MINUTES` and resumes on its own. A provider
  error that reads as a block or logout pauses it **indefinitely** instead, because nothing
  about that improves by waiting.
- **Health score.** 0–100, computed live from connection state, 24-hour failure rate, pause
  state and recent disconnects — never stored, so it can't disagree with the numbers printed
  beside it.

An automatic campaign routes recipients around a paused number the same way it routes
around a disconnected one. A campaign pinned to a single number has its messages *held*
instead, so a one-hour pause costs no recipients.

## Compliance: opt-out and quiet hours

`Contact.subscribed` answers "may we message them". `compliance.ConsentEvent` answers who
decided that, when, and on what evidence — which is the half a platform review actually
asks about. Rows are append-only; opting back in writes a new row.

- **Stop words.** An inbound message matching the built-in multi-language list
  (`backend/apps/compliance/keywords.py`) or the tenant's own additions unsubscribes the
  contact. Two rules keep it off ordinary conversation: the phrase must appear as whole
  words, and the whole message must be short — "please stop" opts out, "I'll never stop
  recommending you" does not. Ambiguous words (`cancel`, `cancelar`) are deliberately not in
  the defaults; on a commerce account they usually mean "cancel my order".
- **Automation stands down.** A message that asks to stop is answered by the opt-out
  confirmation and nothing else — no welcome message, no keyword reply, no workflow step.
- **The way out on campaigns.** `Campaign.append_opt_out` (on by default) adds an opt-out
  line, or an unsubscribe button where the tenant has chosen that style and it fits. The
  footer is the default because it renders on every client, while a button turns the message
  into an interactive one, which QR connections cannot guarantee. It is written into the
  stored message, not added in flight: if a campaign is questioned, the answer has to come
  from the row that was sent.
- **Quiet hours.** Optional per-tenant window, measured in the tenant's timezone or in the
  contact's own where the country is known. Messages inside it are **held and sent when it
  opens**, never dropped.

Every send-time check runs in one place — `backend/apps/messaging/gating.py` — which returns
ALLOW, HOLD (re-queue, nobody is lost) or BLOCK (skip, with the reason recorded). What a
message is decides which rules apply, via `Message.Kind`: `MARKETING` obeys consent and
quiet hours, `SERVICE` (an agent's inbox reply, an auto-reply to something the contact just
did) obeys consent only, and `NOTICE` — the opt-out confirmation itself — obeys neither,
because it is owed to the person at the moment they asked.

## AI calling agent

The agent phones a contact, holds a spoken conversation, and writes down what happened.
It is off by default, per tenant, and refuses to switch on until it has credentials that
could actually place a call.

**How a call runs.** Twilio does the speech recognition (`<Gather input="speech">`) and
the speech synthesis (`<Say>`), and each turn of the conversation arrives as one HTTP
request to `/voice/turn/<token>/`. That is why this needs no new service and no socket
held open — it runs on the same gunicorn workers as everything else. The cost is the turn
boundary: Twilio waits for the caller to stop talking before it posts, so the agent cannot
be interrupted mid-sentence the way a media-stream agent can.

**The clock is the hard constraint.** Twilio abandons a webhook that has not answered in
15 seconds and fails the call, so a turn works to `VOICE_TURN_BUDGET_SECONDS` (11 by
default), the model runs with adaptive thinking at `low` effort, and the tool loop is
capped at three rounds and checks the deadline between them. If the model cannot be
reached at all the call stays up and the agent asks the person to repeat themselves —
every failure path ends in something being said, never in a 500.

**What the agent may do**, each gated by its own setting: `end_call`, `transfer_to_human`,
`save_details` (onto the call and the contact record), `search_knowledge` (the same
knowledge base the WhatsApp AI agent uses), `send_whatsapp` (for anything easier read than
heard — an address, a link), and `schedule_callback` (which books a real second call).
Repeated identical tool calls within one turn are executed once and answered from the
first result: a model going round in circles would otherwise send the same WhatsApp
message three times.

**Webhook security** is two independent halves, and both must hold: an unguessable
per-call token in the URL path says which call this is, and Twilio's HMAC signature over
the URL and body says the request is really from Twilio. `VOICE_VALIDATE_SIGNATURES=false`
disables the second one and belongs only in local development.

**Placing a real call** needs a URL Twilio can reach. `VOICE_PUBLIC_BASE_URL` is
deliberately separate from `BACKEND_PUBLIC_URL`, which under docker-compose is the
internal `http://backend:8000` — a name that resolves only inside the Docker network:

```bash
cloudflared tunnel --url http://localhost      # or: ngrok http 80
# put the https URL it prints into VOICE_PUBLIC_BASE_URL in .env, then
docker compose up -d --build backend celery-worker celery-beat
```

Then on the **Calling Agent** page: paste the Twilio Account SID, Auth Token and a
voice-capable Twilio number, add an Anthropic key (or set `AI_API_KEY` deployment-wide),
switch the agent on, and use **Place a call**. The Calls tab shows the live transcript as
it happens.

**Answering the phone** is a separate switch from placing calls, because they are
separate decisions — a business may be glad for the agent to make its own reminder calls
and not at all glad for it to be the first voice a customer hears. Turn on *Answer calls
to this number*, then paste the URL the page shows into the Twilio number's "A call comes
in" setting (Webhook, HTTP POST). That URL carries a per-tenant token rather than a
per-call one, since an incoming call has no session yet. With the switch off, an incoming
call is hung up quietly rather than left ringing.

**Costs and consent.** Calls are billed by Twilio per minute and are *not* metered by the
credit ledger — that ledger is for WhatsApp messages, and quietly spending message credits
on phone calls would be wrong. Recording is off by default because recording somebody
without telling them is illegal in many places; if you turn it on, say so in the greeting.

## Environment variables

See `.env.example` (Docker/production) and `backend/.env.example` (local development) for
the full list with comments. Both are the source of truth — this section intentionally
doesn't duplicate it and risk going stale.

## Database backup and restore

```bash
# Backup
docker compose exec postgres pg_dump -U multisender multisender | gzip > backup.sql.gz

# Restore (into a running, empty database)
gunzip -c backup.sql.gz | docker compose exec -T postgres psql -U multisender multisender
```

For local (non-Docker) Postgres, drop `docker compose exec postgres` and run `pg_dump`
/`psql` directly against your local instance.

## What's not built yet

Team/agent roles beyond the tenant owner, subscriptions/billing/plan entitlements,
reseller white-labeling, and WebSocket-based real-time updates — the inbox works over plain
REST and keeps itself current by polling every 15 seconds, and conversations can be
assigned only to yourself until team roles exist. Workflows are edited as a JSON node
graph; there is no visual builder. The calling agent is turn-based over Twilio
webhooks — there is no real-time media-stream mode, so it cannot be interrupted
mid-sentence, and Twilio is the only carrier implemented behind `apps.voice.providers`. Reports and credit visibility are scoped to the
requesting user's own tenant — there is no cross-tenant roll-up view for
resellers/superadmins yet.
