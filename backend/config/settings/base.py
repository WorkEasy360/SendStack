"""
Base Django settings for the SendStack project.

Shared by every environment. Environment-specific overrides live in
``development.py`` and ``production.py``, which both start with
``from .base import *``.
"""

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from dotenv import load_dotenv

# config/settings/base.py -> config/settings -> config -> backend
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


SECRET_KEY = os.getenv("SECRET_KEY", "")

DEBUG = False

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",

    # SendStack apps
    "apps.tenants",
    "apps.accounts",
    "apps.whatsapp",
    "apps.contacts",
    "apps.templates_app",
    "apps.campaigns",
    "apps.messaging",
    "apps.media_library",
    "apps.inbox",
    "apps.automation",
    "apps.crm",
    "apps.compliance",
    "apps.tracking",
    "apps.ai",
    # After `ai`, `inbox`, `automation` and `whatsapp`: its engine reaches into
    # all four, and its one hook into the inbound ladder is a call from
    # `automation.receivers` rather than a competing signal receiver.
    "apps.chatbot",
    "apps.voice",
    "apps.realtime",
    "apps.leadgen",
    "apps.leadtools",
    "apps.reports",
    "apps.credits",
    "apps.subscriptions",
    "apps.notifications",
    "apps.teams",
    "apps.audit",
    "apps.health",
    "apps.legal",
    "apps.scheduling",
    "apps.onboarding",
    "apps.billing",
    "apps.support",
    # Last on purpose. Its receivers connect to signals declared by messaging,
    # campaigns, inbox and whatsapp, and its models reference whatsapp and
    # tenants -- so every app it listens to is already loaded when its
    # AppConfig.ready() runs.
    "apps.publicapi",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.RequestIDMiddleware",
    # After AuthenticationMiddleware so request.user exists, and it reads
    # request.POST -- which needs CsrfViewMiddleware to have already run.
    "core.middleware.AdminSecurityMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

AUTH_USER_MODEL = "accounts.User"


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST": os.getenv("DB_HOST"),
        "PORT": os.getenv("DB_PORT"),
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
    }
}

# Argon2 first: verifies in ~50ms here vs. ~300ms for Django's default
# PBKDF2 iteration count, and is the algorithm Django's own docs recommend.
# The PBKDF2 hashers stay listed so existing users' stored hashes still
# verify; each such user is transparently upgraded to Argon2 on their next
# successful login (Django's standard check_password upgrade path).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static & media files

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

FILE_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("FILE_UPLOAD_MAX_MEMORY_SIZE", str(10 * 1024 * 1024)))
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("DATA_UPLOAD_MAX_MEMORY_SIZE", str(10 * 1024 * 1024)))

# Largest single attachment accepted by /api/v1/media/assets/. Keep this at
# or below nginx's `client_max_body_size` (20M) -- anything bigger is cut
# off by the proxy before Django can produce a useful error.
MEDIA_ASSET_MAX_UPLOAD_BYTES = int(os.getenv("MEDIA_ASSET_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))

# Whatever ends up sending an uploaded attachment fetches its URL itself --
# Baileys does so from inside the qr-bridge container, Meta does so from the
# public internet -- so the link stored on a message has to be reachable by
# them, not just by the browser.
#
# MEDIA_PUBLIC_BASE_URL is the origin written into those links (e.g.
# "https://app.example.com"). Left blank, links are built from the incoming
# request, which is correct for any single-host deployment and is what
# happens today.
MEDIA_PUBLIC_BASE_URL = os.getenv("MEDIA_PUBLIC_BASE_URL", "").rstrip("/")

# MEDIA_INTERNAL_BASE_URL swaps that origin for one the provider container can
# actually resolve, at the moment a link is handed over (e.g. "http://nginx").
# Only the outgoing request is rewritten: the stored link stays the public one
# so it still opens from the inbox. Blank means no rewriting.
MEDIA_INTERNAL_BASE_URL = os.getenv("MEDIA_INTERNAL_BASE_URL", "").rstrip("/")


# Email (console in development; configure SMTP through environment variables in production).
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@sendstack.local")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")

# How long a password-reset link stays usable, in seconds. Django's own
# default is three days, which for a link that is a full account takeover if
# it is read by anybody else is far too generous: it sits in a mailbox, in a
# mail provider's logs and in any forwarded copy for that whole time.
#
# One hour is long enough for somebody to walk to their laptop and short
# enough that a leaked mail is usually already worthless. The token is
# single-use regardless (see apps.accounts.views.PasswordResetConfirmView):
# expiry is the control for a link that was never used, not for one that was.
PASSWORD_RESET_TIMEOUT = int(os.getenv("PASSWORD_RESET_TIMEOUT", str(60 * 60)))

# Attempts to request a reset for one *email address* before the endpoint
# stops sending, and for how long. The DRF "auth" throttle above counts per
# client address, which does nothing about somebody who wants one person's
# mailbox filled: they rotate addresses and every request still sends a mail.
# This counts against the address being mailed instead. It is deliberately
# looser than the login lockout -- asking twice because the first mail was
# slow is normal behaviour, guessing a password twenty times is not.
PASSWORD_RESET_MAX_PER_EMAIL = int(os.getenv("PASSWORD_RESET_MAX_PER_EMAIL", "5"))
PASSWORD_RESET_EMAIL_WINDOW_SECONDS = int(os.getenv("PASSWORD_RESET_EMAIL_WINDOW_SECONDS", str(60 * 60)))

# Where customers are told to go when something is wrong. Configurable rather
# than hardcoded, because a reseller's customers must not be sent to the
# platform's inbox -- TenantBranding.support_email overrides this per tenant
# where it is set (apps.tenants.models.TenantBranding.resolve).
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")
SUPPORT_URL = os.getenv("SUPPORT_URL", "")


# --- Build identity ------------------------------------------------------
#
# Which build is actually running, for the support diagnostics view, the
# readiness probe and a bug report. APP_BUILD is expected to be a short commit
# sha stamped in at image build time; it is deliberately *not* derived from
# the .git directory, which is not in the image and must not be.
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
APP_BUILD = os.getenv("APP_BUILD", "")
APP_ENVIRONMENT = os.getenv("APP_ENVIRONMENT", "development")


# --- Legal documents -----------------------------------------------------
#
# The version a customer is currently being asked to accept. Bumping one of
# these is what makes every existing acceptance stale, so the re-acceptance
# prompt appears -- see apps.legal. Changing the text of a document without
# bumping its version is the one thing that must never happen, because the
# acceptance records would then point at wording nobody agreed to.
TERMS_VERSION = os.getenv("TERMS_VERSION", "1.0")
PRIVACY_VERSION = os.getenv("PRIVACY_VERSION", "1.0")
DPA_VERSION = os.getenv("DPA_VERSION", "1.0")

# The business behind the legal documents. Every one of these is a placeholder
# the owner must replace before the documents mean anything -- apps.legal
# renders an explicit "unreviewed draft" banner for as long as they are blank,
# rather than silently shipping a contract naming nobody.
LEGAL_ENTITY_NAME = os.getenv("LEGAL_ENTITY_NAME", "")
LEGAL_ENTITY_ADDRESS = os.getenv("LEGAL_ENTITY_ADDRESS", "")
LEGAL_CONTACT_EMAIL = os.getenv("LEGAL_CONTACT_EMAIL", "")
LEGAL_GOVERNING_LAW = os.getenv("LEGAL_GOVERNING_LAW", "")


# Redis / Cache / Celery

def _with_redis_password(url: str) -> str:
    """
    Splice REDIS_PASSWORD into a Redis URL that does not already carry one.

    Redis is authenticated (docker-compose runs it with ``--requirepass``), and
    the password is a secret that belongs in one environment variable rather
    than repeated inside three URLs. Assembling the URL here rather than in
    docker-compose also means it is *quoted*: a generated password containing
    "@", ":" or "/" would otherwise silently produce a URL pointing at the
    wrong host.

    A URL that already has credentials is left exactly as given, so an
    operator who prefers to write the whole URL themselves still can.
    """
    password = os.getenv("REDIS_PASSWORD", "")
    if not password or not url:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=f":{quote(password, safe='')}@{parts.netloc}"))


REDIS_URL = _with_redis_password(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # docker-compose.yml runs Redis as a sibling container on the
            # same Docker network as the app -- normal round trips there are
            # sub-millisecond, not the multi-hundred-ms a cross-region link
            # would need. 2s (the old default) meant a Redis outage stalled
            # every throttled request for a full 2 seconds before
            # IGNORE_EXCEPTIONS let it continue; 250ms is generous headroom
            # over same-network latency while failing fast on a real outage.
            # Override via env if Redis ever moves off-box.
            "SOCKET_CONNECT_TIMEOUT": float(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "0.25")),
            "SOCKET_TIMEOUT": float(os.getenv("REDIS_SOCKET_TIMEOUT", "0.25")),
            # A Redis blip degrades rate limiting, not the whole API: cache
            # calls no-op instead of raising. /health/ready/ still reports
            # Redis outages so it's never silently invisible to ops.
            "IGNORE_EXCEPTIONS": True,
        },
    }
}

CELERY_BROKER_URL = _with_redis_password(os.getenv("CELERY_BROKER_URL", "")) or REDIS_URL
CELERY_RESULT_BACKEND = _with_redis_password(os.getenv("CELERY_RESULT_BACKEND", "")) or REDIS_URL

# Celery reads these two out of the environment *directly* and prefers what it
# finds there over anything config_from_object hands it -- see
# celery.app.utils.Settings.broker_url, which is literally
# `os.environ.get('CELERY_BROKER_URL') or self.first('broker_url', ...)`.
#
# So computing the authenticated URL as a Django setting is not enough: with a
# bare `CELERY_BROKER_URL=redis://redis:6379/0` in the environment, the worker
# connects with no password at all and every task sits unconsumed behind
# "HELLO must be called with the client already authenticated". Putting the
# resolved value back where Celery looks is what makes the setting effective,
# and it does it for any deployment however it configured the variable --
# docker-compose, a plain .env, or a hand-rolled systemd unit.
os.environ["CELERY_BROKER_URL"] = CELERY_BROKER_URL
os.environ["CELERY_RESULT_BACKEND"] = CELERY_RESULT_BACKEND
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = int(os.getenv("CELERY_WORKER_PREFETCH_MULTIPLIER", "4"))

# --- Why Celery settings alone do not make sending crash-safe -------------
#
# `acks_late` + the Redis broker's visibility timeout will *redeliver* a
# send_message_task whose worker died. That does not recover the message: the
# redelivered task finds the row in SENDING (not QUEUED), hits the early-return
# guard, and does nothing. Turning on `task_reject_on_worker_lost` only makes
# that redelivery happen sooner -- same dead end -- and it would also mask a
# genuine poison task by requeuing it forever, so it stays OFF.
#
# The authority for "did this message send, and is it safe to try again" is
# the durable lease on the Message row plus the reconciliation sweep
# (apps/messaging/recovery.py, apps.messaging.tasks.sweep_stuck_sends). A
# worker that dies mid-send leaves a SENDING row with an expired lease and an
# attempt row that records how far it got; the sweep classifies it and moves
# it forward without ever blindly resending. The soft time limit below turns a
# hung provider call into a clean RECOVERY_REQUIRED instead of a hard kill.
SEND_LEASE_SECONDS = int(os.getenv("SEND_LEASE_SECONDS", "180"))
SEND_SOFT_TIME_LIMIT_SECONDS = int(os.getenv("SEND_SOFT_TIME_LIMIT_SECONDS", "90"))
SEND_HARD_TIME_LIMIT_SECONDS = int(os.getenv("SEND_HARD_TIME_LIMIT_SECONDS", "120"))
SEND_RECOVERY_SWEEP_INTERVAL_SECONDS = int(os.getenv("SEND_RECOVERY_SWEEP_INTERVAL_SECONDS", "60"))

#: Deterministic crash-injection points for the send path. Empty everywhere
#: except in tests that arm it (see core/failpoints.py). Never set this in a
#: real deployment.
FAILPOINTS = set()

# --- Queues ---------------------------------------------------------------
#
# Everything used to run on one queue behind one worker, which meant a single
# 200,000-row CSV import sat in front of every campaign send, every inbox
# auto-reply and the scheduler tick that fires scheduled campaigns. The work
# has genuinely different shapes and they should not share a line:
#
#   messages     one provider call each, thousands of them, latency matters
#   campaigns    bursty; one task enqueues thousands of the above
#   bulk         imports -- minutes long, customer-facing, must not block sends
#   maintenance  retention sweeps and periodic housekeeping, nobody waiting
#   webhooks     outbound HTTP to a customer's own server: a ten-second
#                timeout, up to five attempts, and entirely at the mercy of
#                somebody else's uptime. One dead endpoint must not sit in
#                front of anything else.
#   celery       everything else (automation, AI replies, CRM, voice)
#
# **A worker still consumes all six by default** (see docker-compose.yml), so
# this changes nothing for a deployment that does not act on it. What it makes
# possible is running a worker dedicated to `messages` -- at which point a
# large import can no longer delay anybody's campaign. The danger with routing
# is a queue nobody consumes, where tasks vanish silently; /health/queues/
# reports the depth of every queue below for exactly that reason.
CELERY_TASK_DEFAULT_QUEUE = "celery"

#: Every queue this application publishes to. The single source of truth for
#: the worker's -Q list, the operations dashboard and the production check.
CELERY_QUEUE_NAMES = ["celery", "messages", "campaigns", "bulk", "maintenance", "webhooks"]

CELERY_TASK_ROUTES = {
    "apps.messaging.tasks.send_message_task": {"queue": "messages"},
    "apps.campaigns.tasks.dispatch_campaign": {"queue": "campaigns"},
    "apps.contacts.tasks.process_contact_import": {"queue": "bulk"},
    "apps.health.tasks.run_retention_sweep": {"queue": "maintenance"},
    "apps.tenants.tasks.run_storage_cleanup": {"queue": "maintenance"},
    "apps.crm.tasks.refresh_crm_signals": {"queue": "maintenance"},
    "apps.automation.tasks.sweep_stuck_workflow_runs": {"queue": "maintenance"},
    "apps.voice.tasks.reap_stale_calls_task": {"queue": "maintenance"},
    "apps.messaging.tasks.sweep_stuck_sends": {"queue": "maintenance"},
    # Deliberately *not* maintenance. The scheduler tick is what turns a
    # scheduled campaign into work at all, and putting it behind an hour-long
    # retention delete is how a Friday campaign goes out on Saturday.
    "apps.scheduling.tasks.run_due_scheduled_jobs": {"queue": "celery"},
    # Webhook delivery is an outbound HTTP call to somebody else's server with
    # a ten-second timeout and up to five attempts. On the default queue, one
    # customer's dead endpoint would sit in front of everything else that
    # queue carries. `maintenance` is the wrong home for the same reason the
    # scheduler tick is -- a delivery is time-sensitive to the integration
    # waiting on it -- so it gets its own.
    "apps.publicapi.tasks.deliver_webhook": {"queue": "webhooks"},
    "apps.publicapi.tasks.sweep_pending_webhooks": {"queue": "webhooks"},
    # Pruning log tables is exactly what `maintenance` is for.
    "apps.publicapi.tasks.prune_public_api_data": {"queue": "maintenance"},
}


# --- The durable scheduler ------------------------------------------------
#
# How often the poller asks PostgreSQL for work that has come due, and
# therefore the scheduling precision this system offers: a job fires between
# its scheduled time and one interval later. Late, never early, bounded by
# this number. Say sixty seconds to a customer, not "at 9:00:00".
SCHEDULER_POLL_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_POLL_INTERVAL_SECONDS", "60"))

# How long a worker's claim on a job is honoured before the job is treated as
# abandoned and returned to the queue. Must exceed the longest a handler can
# legitimately take; dispatching a campaign hands its messages to the broker
# and returns, so fifteen minutes is generous by design.
SCHEDULER_LEASE_SECONDS = int(os.getenv("SCHEDULER_LEASE_SECONDS", "900"))

# Most jobs one tick will claim. A ceiling on the damage a backlog can do to
# one tick, not a throughput limit: what is not claimed now is claimed next
# tick, a minute later.
SCHEDULER_BATCH_SIZE = int(os.getenv("SCHEDULER_BATCH_SIZE", "100"))

# How long a campaign must show **no message activity at all** before it is
# treated as stranded and re-dispatched.
#
# Deliberately generous. A campaign paces itself with per-message Celery
# countdowns, and a large one legitimately runs for hours -- so a short
# threshold would "rescue" campaigns that are working and compress the pacing
# the customer chose. What this detects is silence, not slowness: a healthy
# campaign of any size has *some* message changing state.
CAMPAIGN_STALL_SECONDS = int(os.getenv("CAMPAIGN_STALL_SECONDS", "3600"))


# Periodic work, run by the `celery-beat` container.
#
# A phone call is the one thing in this system whose end we may never be told
# about: if a carrier's completion webhook is lost, the call sits IN_PROGRESS
# for ever, holding a slot in "live calls" and making the daily limit wrong.
# Everything else here is driven by an event that has already happened.
CELERY_BEAT_SCHEDULE = {
    "reap-stale-calls": {
        "task": "apps.voice.tasks.reap_stale_calls_task",
        "schedule": int(os.getenv("VOICE_REAP_INTERVAL_SECONDS", "300")),
    },
    # Workflow runs whose worker died mid-step. Without this they sit in
    # RUNNING forever: the task that would have resumed them died with the
    # worker, so nothing else ever looks at them again.
    "sweep-stuck-workflow-runs": {
        "task": "apps.automation.tasks.sweep_stuck_workflow_runs",
        "schedule": int(os.getenv("WORKFLOW_SWEEP_INTERVAL_SECONDS", "300")),
    },
    # Messages left in SENDING by a worker that died inside the ambiguous
    # provider window. This is the P0 recovery path: without it one OOM strands
    # a message (and its campaign) for ever. Every minute, because a stuck
    # SENDING row is a customer's campaign that has silently stopped moving.
    # See apps/messaging/recovery.py.
    "sweep-stuck-sends": {
        "task": "apps.messaging.tasks.sweep_stuck_sends",
        "schedule": SEND_RECOVERY_SWEEP_INTERVAL_SECONDS,
    },
    # Lead scores decay with time and contacts go quiet with nothing
    # happening, so neither has an event to react to -- see apps/crm/tasks.py.
    # Daily by default: the recency bands are measured in days, so a more
    # frequent pass would cost a full table scan to change almost nothing.
    "refresh-crm-signals": {
        "task": "apps.crm.tasks.refresh_crm_signals",
        "schedule": int(os.getenv("CRM_REFRESH_INTERVAL_SECONDS", str(24 * 60 * 60))),
    },
    # **The durable scheduler.** Everything else in this dict reacts to an
    # event; this one is the event. A campaign scheduled for Friday exists as
    # a row in PostgreSQL and nowhere else, and this tick is what turns that
    # row into work. If it stops, scheduled campaigns stop -- which is why
    # apps.health.checks watches for overdue jobs rather than trusting it.
    "run-due-scheduled-jobs": {
        "task": "apps.scheduling.tasks.run_due_scheduled_jobs",
        "schedule": SCHEDULER_POLL_INTERVAL_SECONDS,
    },
    # Chatbot conversations nobody has touched in a long time. Without this a
    # workspace's "active conversations" figure only ever grows: somebody who
    # asked one question in March is not an open conversation in September, and
    # counting them as one makes the overview useless.
    "expire-idle-chatbot-sessions": {
        "task": "apps.chatbot.tasks.expire_idle_sessions_task",
        "schedule": int(os.getenv("CHATBOT_SESSION_SWEEP_INTERVAL_SECONDS", str(15 * 60))),
    },
    # The chatbot debug trail, which grows by roughly a dozen rows per inbound
    # message and answers a question nobody asks about last quarter.
    "prune-chatbot-events": {
        "task": "apps.chatbot.tasks.prune_chatbot_events_task",
        "schedule": int(os.getenv("CHATBOT_EVENT_PRUNE_INTERVAL_SECONDS", str(24 * 60 * 60))),
    },
    # Housekeeping: the tables that grow with traffic rather than with
    # customers. Nothing here touches customer data -- see
    # apps.health.retention, which is explicit about what it will never delete.
    "prune-expired-data": {
        "task": "apps.health.tasks.run_retention_sweep",
        "schedule": int(os.getenv("RETENTION_SWEEP_INTERVAL_SECONDS", str(24 * 60 * 60))),
    },
    # Housekeeping a *customer* asked for, as opposed to the operator's own
    # above. Removes finished campaigns past the horizon each workspace set
    # for itself, and does nothing at all for the workspaces that set none --
    # which is every workspace by default. See apps.tenants.storage.
    "prune-tenant-campaign-data": {
        "task": "apps.tenants.tasks.run_storage_cleanup",
        "schedule": int(os.getenv("STORAGE_CLEANUP_INTERVAL_SECONDS", str(24 * 60 * 60))),
    },
    # Trials that have run out. Note what this task is *not*: it is not what
    # stops an expired trial's entitlements. `Subscription.access_state` reads
    # the dates and answers EXPIRED whether or not this ever runs, so a
    # lifecycle job that dies cannot turn a trial into unlimited premium
    # access. This task records the status change and sends the notices.
    # Webhook deliveries that were written but never picked up -- see
    # apps.publicapi.tasks.sweep_pending_webhooks for the two ways that
    # happens. This is what makes the delivery guarantee at-least-once rather
    # than best-effort.
    "sweep-pending-webhooks": {
        "task": "apps.publicapi.tasks.sweep_pending_webhooks",
        "schedule": int(os.getenv("WEBHOOK_SWEEP_INTERVAL_SECONDS", "60")),
    },
    # The public API's own log tables, which grow with API traffic rather than
    # with customers.
    "prune-public-api-data": {
        "task": "apps.publicapi.tasks.prune_public_api_data",
        "schedule": int(os.getenv("PUBLIC_API_PRUNE_INTERVAL_SECONDS", str(6 * 60 * 60))),
    },
    "expire-due-trials": {
        "task": "apps.subscriptions.tasks.sweep_subscription_lifecycle",
        "schedule": int(os.getenv("TRIAL_SWEEP_INTERVAL_SECONDS", str(60 * 60))),
    },
}


# --- Public developer API ------------------------------------------------
#
# The ceilings a key falls back to when it sets none of its own. Note that on
# ApiKey these fields use 0 to mean "use these defaults" rather than
# "unlimited" -- see the comment on ApiKey.requests_per_minute for why a
# forgotten rate limit must not become no rate limit.
PUBLIC_API_DEFAULT_REQUESTS_PER_MINUTE = int(os.getenv("PUBLIC_API_DEFAULT_REQUESTS_PER_MINUTE", "120"))
# Lower than the request ceiling on purpose: a request is cheap and a message
# occupies a worker and a real WhatsApp connection for seconds.
PUBLIC_API_DEFAULT_MESSAGES_PER_MINUTE = int(os.getenv("PUBLIC_API_DEFAULT_MESSAGES_PER_MINUTE", "60"))
PUBLIC_API_DEFAULT_CAMPAIGNS_PER_DAY = int(os.getenv("PUBLIC_API_DEFAULT_CAMPAIGNS_PER_DAY", "50"))

# The address customers are told to call, printed in the docs, in the developer
# portal, in the Postman export and in every generated example.
#
# **Deliberately not BACKEND_PUBLIC_URL**, for exactly the reason
# VOICE_PUBLIC_BASE_URL is not: under docker-compose that one is
# `http://backend:8000`, a name that resolves only inside the Docker network.
# Printing it on a screen whose whole purpose is to answer "where do I take the
# URL from?" would hand every customer an address that cannot work, which is
# worse than printing nothing.
#
# Left empty by default, and empty does not mean broken: the readers fall back
# to rebuilding the URL from the request, so a deployment that never sets this
# still shows the address the customer's own browser just reached us on -- which
# is by construction one that works from where they are. Set it explicitly when
# the API is served on a different hostname from the dashboard (`api.acme.com`
# in front of `app.acme.com`), because then the request cannot tell us.
PUBLIC_API_BASE_URL = os.getenv("PUBLIC_API_BASE_URL", "").rstrip("/")

# HTTPS. Both default to `not DEBUG` when left unset, so production requires it
# without an operator having to know these exist, and a developer running the
# stack locally is not blocked. Setting either to False in production means API
# keys and webhook payloads travel in cleartext.
def _env_tristate(name):
    """The env var as a bool, or None when it is unset.

    None is not the same as False here: it means "nobody chose", which the
    readers resolve to `not DEBUG`. Resolving it here instead would bake
    DEBUG's value in at import time, and the test suite flips DEBUG.
    """
    return None if os.getenv(name) is None else env_bool(name)


PUBLIC_API_REQUIRE_HTTPS = _env_tristate("PUBLIC_API_REQUIRE_HTTPS")
PUBLIC_API_REQUIRE_HTTPS_WEBHOOKS = _env_tristate("PUBLIC_API_REQUIRE_HTTPS_WEBHOOKS")

# A kill switch for outbound webhooks. Emission becomes a no-op; nothing else
# about the API changes. Useful when a deployment's egress is down and the
# delivery queue would otherwise fill with doomed retries.
PUBLIC_API_WEBHOOKS_ENABLED = env_bool("PUBLIC_API_WEBHOOKS_ENABLED", True)
PUBLIC_API_WEBHOOK_TIMEOUT_SECONDS = int(os.getenv("PUBLIC_API_WEBHOOK_TIMEOUT_SECONDS", "10"))

# How long the two API log tables are kept. 0 turns a rule off entirely, the
# same convention the retention settings below use.
PUBLIC_API_REQUEST_LOG_RETENTION_DAYS = int(os.getenv("PUBLIC_API_REQUEST_LOG_RETENTION_DAYS", "30"))
PUBLIC_API_WEBHOOK_LOG_RETENTION_DAYS = int(os.getenv("PUBLIC_API_WEBHOOK_LOG_RETENTION_DAYS", "30"))


# --- Data retention ------------------------------------------------------
#
# What apps.health.retention is allowed to delete, and after how long. Every
# one of these is measured in days and **0 turns that rule off entirely**,
# which is the same "0 means no limit" convention the plan ceilings use.
#
# These cover operational exhaust only. Customer business data (messages,
# contacts, campaigns), consent evidence, audit events, the credit ledger and
# financial records have no rule here and are never pruned by the application
# -- see docs/DATA_RETENTION.md, which this implements.

# Raw provider webhook bodies, kept only long enough that a provider's own
# retry still finds the row and is recognised as a duplicate. Providers retry
# over hours; a month is generous.
RETENTION_WEBHOOK_EVENT_DAYS = int(os.getenv("RETENTION_WEBHOOK_EVENT_DAYS", "30"))

# Scheduled jobs that already ran, or that the customer cancelled. FAILED jobs
# are deliberately not covered: a failed scheduled job is exactly what an
# operator goes looking for weeks later.
RETENTION_SCHEDULED_JOB_DAYS = int(os.getenv("RETENTION_SCHEDULED_JOB_DAYS", "30"))

# The number-to-button lookup for a question sent as a numbered list. Inert
# once BUTTON_PROMPT_TIMEOUT_MINUTES has passed; the message that asked the
# question stays in the conversation regardless.
RETENTION_BUTTON_PROMPT_DAYS = int(os.getenv("RETENTION_BUTTON_PROMPT_DAYS", "7"))

# How often the retention pass runs. It is a slow background job, not a
# reaction to anything, so once a day is right.
RETENTION_SWEEP_INTERVAL_SECONDS = int(os.getenv("RETENTION_SWEEP_INTERVAL_SECONDS", str(24 * 60 * 60)))

# The ceiling the per-workspace storage screen measures against, in MB.
# 0 (the default) means no ceiling, which is the convention every limit in
# this product uses -- and it is the honest default, because storage is not
# something any plan here sells. See apps.tenants.storage._storage_limit.
TENANT_STORAGE_LIMIT_MB = int(os.getenv("TENANT_STORAGE_LIMIT_MB", "0"))


# WhatsApp provider credential encryption (Fernet key, see connections/session.py)
WHATSAPP_TOKEN_ENCRYPTION_KEY = os.getenv("WHATSAPP_TOKEN_ENCRYPTION_KEY", "")

# Seconds staggered between each message a campaign dispatches, to stay under
# provider-imposed sending limits. Not a promise of unlimited throughput.
#
# This is now only the *default* for the min/max bounds below -- the actual
# gap before each individual message is drawn randomly from that range (see
# apps/campaigns/assignment.py). Leaving both bounds unset keeps min == max,
# which reproduces the fixed interval this setting always meant.
CAMPAIGN_SEND_INTERVAL_SECONDS = float(os.getenv("CAMPAIGN_SEND_INTERVAL_SECONDS", "1"))

# Bounds for the per-message random send delay, in seconds. Every outgoing
# campaign message gets its own freshly generated delay d with
# min < d <= max; a campaign may override either bound (Campaign.
# min_delay_seconds / max_delay_seconds) and falls back to these when it
# doesn't. Defaulting both to CAMPAIGN_SEND_INTERVAL_SECONDS means a
# deployment that only ever configured that one variable keeps exactly the
# pacing it has today.
CAMPAIGN_MIN_DELAY_SECONDS = float(os.getenv("CAMPAIGN_MIN_DELAY_SECONDS", str(CAMPAIGN_SEND_INTERVAL_SECONDS)))
CAMPAIGN_MAX_DELAY_SECONDS = float(os.getenv("CAMPAIGN_MAX_DELAY_SECONDS", str(CAMPAIGN_SEND_INTERVAL_SECONDS)))

# Account-assignment mode applied to campaigns created through the API that
# don't specify one. "MANUAL" (the default) preserves the historical
# behaviour -- every message in a campaign goes out on the single account
# the campaign was created with. "AUTO" spreads recipients across every
# connected account in the tenant.
CAMPAIGN_ACCOUNT_ASSIGNMENT_MODE = os.getenv("CAMPAIGN_ACCOUNT_ASSIGNMENT_MODE", "MANUAL").strip().upper()

# Publicly reachable base URL for this backend, used to build the callback
# URL handed to the QR bridge service so it knows where to push webhooks.
BACKEND_PUBLIC_URL = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")

# QR/device-linking bridge service (see apps/whatsapp/providers/qr_bridge.py
# for the HTTP contract this backend expects it to implement). Left unset in
# every environment by default -- no bridge ships with SendStack, so QR
# connections report "QR provider is not configured" until one is deployed
# and pointed at here.
QR_PROVIDER_BASE_URL = os.getenv("QR_PROVIDER_BASE_URL", "")
QR_PROVIDER_API_KEY = os.getenv("QR_PROVIDER_API_KEY", "")
QR_PROVIDER_WEBHOOK_SECRET = os.getenv("QR_PROVIDER_WEBHOOK_SECRET", "")

# How long a generated QR code stays valid before the account moves to
# EXPIRED and the user must request a new one.
#
# 120 seconds, and clamped. The value shipped previously
# (99999999999999999999) is not a duration anybody meant: it overflows a
# JavaScript Date in the qr-bridge (RangeError: Invalid time value) and
# setTimeout's 32-bit delay (TimeoutOverflowWarning, after which the timer
# fires immediately), and on this side it overflows the timedelta that ages
# out abandoned pairing attempts. So a value outside the sane range is
# corrected to the default rather than honoured -- a QR nobody can scan for
# a plausible length of time is a broken deployment, not a configuration
# choice.
QR_TTL_MIN_SECONDS = 30
QR_TTL_MAX_SECONDS = 3600
QR_TTL_DEFAULT_SECONDS = 120


def _qr_ttl_seconds() -> int:
    try:
        value = int(os.getenv("WHATSAPP_QR_SESSION_TTL_SECONDS", "") or QR_TTL_DEFAULT_SECONDS)
    except ValueError:
        return QR_TTL_DEFAULT_SECONDS
    if value < QR_TTL_MIN_SECONDS or value > QR_TTL_MAX_SECONDS:
        return QR_TTL_DEFAULT_SECONDS
    return value


WHATSAPP_QR_SESSION_TTL_SECONDS = _qr_ttl_seconds()


# Sender health: how hard a single WhatsApp number may be pushed.
#
# These are the deployment-wide ceilings. Any account can be given its own
# (apps.whatsapp.models.SendingPolicy), and where both exist the *lower* one
# applies -- see apps.whatsapp.limits. A message that runs into a ceiling is
# held and retried when the window reopens, never dropped.
#
# Set either limit to 0 for "no limit", which is what every deployment had
# before this existed.
WHATSAPP_DAILY_SEND_LIMIT = int(os.getenv("WHATSAPP_DAILY_SEND_LIMIT", "1000"))
WHATSAPP_HOURLY_SEND_LIMIT = int(os.getenv("WHATSAPP_HOURLY_SEND_LIMIT", "200"))

# How long a question sent as a numbered list stays answerable.
#
# WhatsApp reserves interactive buttons for the Cloud API, so on a QR-linked
# number a question goes out as "1. Yes please / 2. Not now" and the answer
# arrives as ordinary text -- see apps.automation.services.resolve_option_reply.
# This is the window in which a bare number is read as that answer.
#
# Long enough to put the phone down and come back; short enough that a "2"
# typed the next morning is not taken as agreement to yesterday's question,
# which would fire an automation nobody chose.
BUTTON_PROMPT_TIMEOUT_MINUTES = int(os.getenv("BUTTON_PROMPT_TIMEOUT_MINUTES", "30"))

# Lead tools (apps.leadtools): the number filter and the group grabber.
#
# The number filter asks WhatsApp, through a QR-linked number, which numbers
# are registered. It does so in batches with a pause between them, because a
# linked device that fires thousands of contact queries back to back looks
# like the scraping WhatsApp bans numbers for. The ceiling per check is there
# for the same reason: a run that would take an hour of steady querying from
# one number is a run that should be split across days, and the person
# starting it should be told so rather than have it silently begin.
LEADTOOLS_NUMBER_CHECK_MAX_NUMBERS = int(os.getenv("LEADTOOLS_NUMBER_CHECK_MAX_NUMBERS", "10000"))
LEADTOOLS_NUMBER_CHECK_BATCH_SIZE = int(os.getenv("LEADTOOLS_NUMBER_CHECK_BATCH_SIZE", "100"))
LEADTOOLS_NUMBER_CHECK_PAUSE_SECONDS = float(os.getenv("LEADTOOLS_NUMBER_CHECK_PAUSE_SECONDS", "1.5"))
# How many WhatsApp groups one grab may read at once. Each is a round trip to
# the phone; the bridge caps this at the same figure.
LEADTOOLS_GROUP_GRAB_MAX_GROUPS = int(os.getenv("LEADTOOLS_GROUP_GRAB_MAX_GROUPS", "25"))

# The warmup ramp for a newly connected QR number, as that number's allowance
# on its first day, second day, and so on. Once the ladder runs out the number
# is "warm" and gets the normal daily limit. Cloud API numbers are metered by
# Meta's own tiers and never ramp -- see apps.whatsapp.limits.warmup_state.
WHATSAPP_WARMUP_ENABLED = os.getenv("WHATSAPP_WARMUP_ENABLED", "true").strip().lower() == "true"
WHATSAPP_WARMUP_LADDER = [
    int(step) for step in os.getenv("WHATSAPP_WARMUP_LADDER", "50,100,250,500,750").split(",") if step.strip()
]

# Automatic pause: a number that starts failing at an unusual rate stops
# sending by itself, because continuing to push a number WhatsApp has begun
# refusing is how a temporary block turns into a permanent one. The minimum
# sample is what stops two failures on a quiet account from pausing it.
WHATSAPP_AUTO_PAUSE_ENABLED = os.getenv("WHATSAPP_AUTO_PAUSE_ENABLED", "true").strip().lower() == "true"
WHATSAPP_FAILURE_WINDOW_MINUTES = int(os.getenv("WHATSAPP_FAILURE_WINDOW_MINUTES", "30"))
WHATSAPP_FAILURE_SAMPLE_MIN = int(os.getenv("WHATSAPP_FAILURE_SAMPLE_MIN", "10"))
WHATSAPP_FAILURE_RATE_THRESHOLD = float(os.getenv("WHATSAPP_FAILURE_RATE_THRESHOLD", "0.5"))
WHATSAPP_AUTO_PAUSE_MINUTES = int(os.getenv("WHATSAPP_AUTO_PAUSE_MINUTES", "60"))

# Longest a single message may be held by the send gate (quiet hours, a send
# limit, a paused number) before it is given up on and reported as skipped.
# Long enough to sit out a quiet night and the following day's limit; short
# enough that nobody discovers a week-old message going out.
MESSAGE_MAX_HOLD_SECONDS = int(os.getenv("MESSAGE_MAX_HOLD_SECONDS", str(48 * 60 * 60)))


# Link tracking
#
# Every URL in an outbound message is replaced with a per-recipient short link
# served from /r/<code>, so a campaign can report clicks and not just reads.
# LINK_TRACKING_BASE_URL is where those short links point: it must be the
# public origin of this deployment, because the contact's phone resolves it,
# not the server. It falls back to MEDIA_PUBLIC_BASE_URL, which is already the
# public origin for the same reason. With neither set, links are left exactly
# as the tenant typed them rather than rewritten to somewhere unreachable.
LINK_TRACKING_ENABLED = env_bool("LINK_TRACKING_ENABLED", True)
LINK_TRACKING_BASE_URL = os.getenv("LINK_TRACKING_BASE_URL", "")
LINK_TRACKING_UTM_SOURCE = os.getenv("LINK_TRACKING_UTM_SOURCE", "whatsapp")
LINK_TRACKING_UTM_MEDIUM = os.getenv("LINK_TRACKING_UTM_MEDIUM", "broadcast")
# An agent's inbox reply keeps the link they typed unless this is turned on.
LINK_TRACKING_TRACK_REPLIES = env_bool("LINK_TRACKING_TRACK_REPLIES", False)


# -- Billing entitlements ----------------------------------------------------
#
# What a customer may consume is decided in apps/subscriptions/services.py and
# documented in docs/ENTITLEMENTS.md. Only two things about it are a
# deployment's choice rather than the product's.

# How long a PAST_DUE subscription keeps working. Nothing here collects money,
# so "past due" is a flag somebody set by hand; cutting a customer off the
# instant it is set would make it an unusable flag. 0 disables the grace.
BILLING_PAST_DUE_GRACE_DAYS = int(os.getenv("BILLING_PAST_DUE_GRACE_DAYS", "14"))

# The platform-owned plan (by name) that governs a CUSTOMER tenant with no
# subscription of its own. Unset -- the default -- means such a tenant is
# unlimited, which is how this product worked before entitlements existed and
# what every tenant created before them relies on. Naming a plan here is the
# explicit, documented way to close that: it applies to customers only, never
# to the platform or a reseller, and `manage.py assign_default_plan` writes
# real subscriptions for the tenants that have none. A name matching no plan
# falls back to unlimited rather than to a guess.
BILLING_DEFAULT_PLAN = os.getenv("BILLING_DEFAULT_PLAN", "")


# -- Commercial rollout switches ---------------------------------------------
#
# Three independent switches, and the defaults are the conservative ones on
# purpose. An operator upgrading an existing deployment gets exactly the
# behaviour they had -- no public signup page, no payment buttons -- until
# they decide otherwise. A commercialization release that silently opened
# registration on every existing install would be a serious surprise.

# Whether strangers may create their own workspace. Off by default: a
# deployment that sells through resellers, or one that is not finished being
# configured, must not have a live signup form because a release shipped.
SELF_SERVICE_SIGNUP_ENABLED = env_bool("SELF_SERVICE_SIGNUP_ENABLED", False)

# Which tenant self-service workspaces hang off, by slug. Blank means the
# platform tenant -- a direct customer of whoever runs the deployment. Set it
# to a reseller's slug when the public website belongs to that reseller. It is
# validated as a PLATFORM or RESELLER tenant; a customer tenant is refused,
# because a customer under a customer is not a shape this hierarchy has.
SELF_SERVICE_SIGNUP_PARENT_SLUG = os.getenv("SELF_SERVICE_SIGNUP_PARENT_SLUG", "")

# Whether a signup starts a free trial. On by default, but it is *also*
# governed by whether BILLING_TRIAL_PLAN resolves: with no trial plan
# configured, trials are unavailable however this is set, and signup says so
# rather than falling through to "no plan" -- which in this codebase means
# unlimited. See apps/subscriptions/trials.py.
TRIALS_ENABLED = env_bool("TRIALS_ENABLED", True)

# The platform-owned plan a trial runs on, by name. Resolved the same careful
# way BILLING_DEFAULT_PLAN is: platform-owned, active, matched exactly, never
# guessed. `manage.py seed_plans` writes a starting set.
BILLING_TRIAL_PLAN = os.getenv("BILLING_TRIAL_PLAN", "")

# How long a trial runs. **The only place this number is decided** -- the
# frontend asks the API for it rather than knowing it. See
# apps/subscriptions/trials.py.
BILLING_TRIAL_DAYS = int(os.getenv("BILLING_TRIAL_DAYS", "14"))

# How many days before a trial ends the one reminder goes out. 0 silences it.
BILLING_TRIAL_REMINDER_DAYS = int(os.getenv("BILLING_TRIAL_REMINDER_DAYS", "3"))

# The currency this deployment prices in, as an ISO 4217 code. Centralised
# here rather than assumed anywhere: `Plan.currency` carries its own and this
# is the default a new plan and every checkout is denominated in. A deployment
# selling in one currency changes this in one place.
BILLING_CURRENCY = os.getenv("BILLING_CURRENCY", "INR").strip().upper()


# -- Payments ----------------------------------------------------------------
#
# Off unless both switched on and configured. `PAYMENTS_ENABLED` alone does
# nothing: apps/billing/providers resolves an adapter and refuses to hand back
# a live one whose credentials are missing, so a half-configured deployment
# gets "checkout unavailable" and never a fake success. See
# apps/billing/providers/base.py.
PAYMENTS_ENABLED = env_bool("PAYMENTS_ENABLED", False)

# Which adapter to use. "razorpay" is implemented; "manual" is the explicit
# no-gateway mode, in which plans are assigned by an operator and no checkout
# is ever offered.
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "manual").strip().lower()

# Razorpay credentials. The key id is public (it is handed to the browser to
# open the checkout); the secret and the webhook secret never leave the
# server, are never logged, and are never serialised into any response.
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# How long a raw webhook payload is kept. It is retained at all so a disputed
# activation can be reconstructed, and not for ever because it is third-party
# data about a customer's payment that nothing needs after the dispute window.
BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS = int(
    os.getenv("BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS", "90")
)


# AI agent
#
# A tenant can store their own key (encrypted, per tenant) from the AI page;
# AI_API_KEY is the deployment-wide fallback for a single-tenant install that
# would rather configure this once in the environment. With neither set the
# agent simply never speaks -- it is off by default in any case.
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_REQUEST_TIMEOUT_SECONDS = int(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "30"))

# --- Knowledge-base-only answering -------------------------------------
#
# The strictest form of "do not make things up", switched on for the whole
# deployment rather than per tenant. Per-tenant `AIAgentConfig.
# answer_from_knowledge_only` already means "prefer the documents"; this means
# something stronger, and three things change when it is on:
#
#   1. Every tenant answers from their knowledge base, whatever their own
#      config says -- the tenant switch can no longer turn it off.
#   2. The relevance gate applies **even to an agent with tools**. Without
#      this, turning tools on quietly removed the gate (a tool-enabled agent
#      was called with no reference material and answered from the model's own
#      weights), which is the exact failure this setting exists to prevent.
#   3. A question the documents do not cover is refused in so many words
#      rather than answered around -- see `AIAgentConfig.knowledge_only_message`.
#
# Off by default so an existing deployment keeps the behaviour it has. The
# shipped .env.example turns it on, because a business assistant inventing a
# refund window is worse than one that says it does not know.
AI_KNOWLEDGE_ONLY = env_bool("AI_KNOWLEDGE_ONLY", False)

# The floor a retrieval must clear before the model is called at all, 0-1.
# A tenant may set a *higher* bar of their own (`AIAgentConfig.min_relevance`);
# this is the one they cannot go below while AI_KNOWLEDGE_ONLY is on, so
# "answer only from the documents" cannot be defeated by setting the tenant
# threshold to zero.
AI_KNOWLEDGE_MIN_RELEVANCE = float(os.getenv("AI_KNOWLEDGE_MIN_RELEVANCE", "0.15"))

# ---------------------------------------------------------------------------
# Chatbot
# ---------------------------------------------------------------------------
#
# The chatbot has no credentials of its own: the model, the key and the
# provider are the AI agent's (above), and the number is the WhatsApp
# account's. What is configurable here is only how long its own bookkeeping is
# kept.

# How long the per-message debug trail is kept. This is the fastest-growing
# table in the feature -- roughly a dozen rows per inbound message -- and it
# answers "why did that happen?", which nobody asks about last quarter. Set to
# 0 to keep it forever, which is only sensible on a deployment small enough
# that somebody is watching the table size.
CHATBOT_EVENT_RETENTION_DAYS = int(os.getenv("CHATBOT_EVENT_RETENTION_DAYS", "30"))

# OpenAI, configured deployment-wide by environment rather than per tenant.
#
# These are read only when a tenant's agent has its provider set to OPENAI,
# and only where the tenant has not set their own -- a tenant key always wins,
# so a multi-tenant install can still let one customer bring their own account.
#
# OPENAI_MODEL is deliberately the only source of the model name for that
# provider: the requirement is that switching model is an environment change
# and a worker restart, not an edit to a settings page in every tenant. A
# tenant who types a model of their own still overrides it, which is what
# makes "use the cheap model for this one customer" possible at all.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

# --- Semantic retrieval (embeddings) -----------------------------------
#
# Off by default, and the knowledge base works without it: retrieval is
# lexical (TF-IDF over the tenant's own chunks) and needs no embedding calls,
# no vector database and no dependency. Turning this on adds a second opinion
# rather than replacing the first -- see apps/ai/embeddings.py for why the two
# are blended instead of one winning.
#
# What it buys: "how long do I have to send it back?" retrieves the refund
# policy that never uses the word "return". What it costs: one embedding call
# per chunk at upload, one per question at ask time, and a vector per chunk in
# the database.
AI_EMBEDDINGS_ENABLED = env_bool("AI_EMBEDDINGS_ENABLED", False)
AI_EMBEDDING_MODEL = os.getenv("AI_EMBEDDING_MODEL", "text-embedding-3-small")
# How much of the final score the vector similarity is worth, 0-1. At 0.5 a
# chunk has to do well on both to win, which is the point of a hybrid: the
# lexical half keeps an exact product code or order number reliable, and the
# semantic half covers the question that shares no words with its answer.
AI_EMBEDDING_WEIGHT = float(os.getenv("AI_EMBEDDING_WEIGHT", "0.5"))
# Chunks sent to the embedding endpoint in one request.
AI_EMBEDDING_BATCH_SIZE = int(os.getenv("AI_EMBEDDING_BATCH_SIZE", "64"))


# --- Where a tenant may point an outbound provider call ------------------
#
# Two settings on the AI and Voice agents let a tenant type the URL this server
# then makes an authenticated request to, so that a customer can bring their own
# OpenAI-compatible endpoint. Unchecked, that is a way for a tenant admin to make
# this server read the cloud metadata service or scan the internal Docker
# network and have the response reported back to them -- see core.outbound_urls.
#
# Private, loopback and link-local addresses are refused by default. That would
# also refuse the documented self-hosted case (Ollama on
# http://localhost:11434/v1), so an operator running one names its host here.
# It is deployment configuration on purpose: a tenant must not be able to grant
# themselves access to this deployment's own network.
#
#   OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS=ollama,localhost
OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS = env_list("OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS")

# The blunt version, for a deployment with no untrusted tenants at all (a
# single-tenant install on a private network). Off by default, and turning it
# on removes the protection above entirely.
OUTBOUND_URL_ALLOW_PRIVATE = env_bool("OUTBOUND_URL_ALLOW_PRIVATE", False)


# Voice agent (outbound and inbound phone calls)
#
# The carrier credentials are per tenant and encrypted, like the WhatsApp ones
# -- only the deployment-wide plumbing lives here.
#
# VOICE_PUBLIC_BASE_URL is the one setting a call cannot be placed without: it
# is the origin the carrier posts its webhooks back to, and it is deliberately
# separate from BACKEND_PUBLIC_URL because under docker-compose that one is the
# internal http://backend:8000, a name that resolves only inside the Docker
# network. In development this is a tunnel (ngrok, cloudflared); in production
# it is the real public hostname.
VOICE_PUBLIC_BASE_URL = os.getenv("VOICE_PUBLIC_BASE_URL", "")
# Signature checking is the only thing standing between a public webhook and
# anybody who has seen a call token. It is never turned off in production.
VOICE_VALIDATE_SIGNATURES = env_bool("VOICE_VALIDATE_SIGNATURES", True)
# How long one turn of conversation may spend thinking. The carrier abandons a
# webhook that has not answered in 15 seconds and fails the call, so this has
# to leave room for retrieval, the tool round-trips and the HTTP overhead.
VOICE_TURN_BUDGET_SECONDS = int(os.getenv("VOICE_TURN_BUDGET_SECONDS", "11"))
VOICE_REQUEST_TIMEOUT_SECONDS = int(os.getenv("VOICE_REQUEST_TIMEOUT_SECONDS", "15"))
VOICE_RING_TIMEOUT_SECONDS = int(os.getenv("VOICE_RING_TIMEOUT_SECONDS", "40"))
VOICE_TRANSFER_TIMEOUT_SECONDS = int(os.getenv("VOICE_TRANSFER_TIMEOUT_SECONDS", "30"))
# Gap between calls in a batch. Carriers rate-limit concurrent originations,
# and a burst from one number is also what their spam filtering looks for.
VOICE_CALL_INTERVAL_SECONDS = int(os.getenv("VOICE_CALL_INTERVAL_SECONDS", "5"))
# A call the carrier has stopped reporting on is closed out after this long.
VOICE_STALE_CALL_SECONDS = int(os.getenv("VOICE_STALE_CALL_SECONDS", "1800"))


# Real-time inbox
#
# Live updates are Server-Sent Events fanned out over Redis pub/sub. They need
# gunicorn to be running threaded workers (see gunicorn.conf.py) -- with sync
# workers each open stream would occupy a whole worker process. Where Redis is
# unreachable the stream says so and the inbox falls back to polling at this
# interval, which is exactly what it did before live updates existed.
REALTIME_FALLBACK_POLL_SECONDS = int(os.getenv("REALTIME_FALLBACK_POLL_SECONDS", "15"))


# Django REST Framework

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.SendStackJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    # Honours ?page_size=, which plain PageNumberPagination does not. The
    # frontend asks for a bigger page on the screens that show a whole
    # collection at once -- the knowledge base's document list among them,
    # where a 21st document silently missing looks exactly like an upload that
    # failed.
    "DEFAULT_PAGINATION_CLASS": "core.pagination.DefaultPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.getenv("THROTTLE_RATE_ANON", "60/min"),
        "user": os.getenv("THROTTLE_RATE_USER", "300/min"),
        "auth": os.getenv("THROTTLE_RATE_AUTH", "10/min"),
        # Creating an account is rarer than signing in and far more
        # expensive -- a workspace, a user, a subscription and an
        # onboarding row per request -- so it gets its own, tighter
        # budget rather than sharing the login one.
        "signup": os.getenv("THROTTLE_RATE_SIGNUP", "5/min"),
        # The public pricing page. Cacheable, cheap, and hit by every
        # visitor, so it is looser than the anonymous default rather
        # than tighter.
        "pricing": os.getenv("THROTTLE_RATE_PRICING", "120/min"),
        "whatsapp_connect": os.getenv("THROTTLE_RATE_WHATSAPP_CONNECT", "10/min"),
        # A whole-workspace export reads every table this tenant owns. At the
        # ordinary user rate one account could build a hundred a minute and
        # take the database with it.
        "export": os.getenv("THROTTLE_RATE_EXPORT", "5/hour"),
        # A coupon box is a code oracle if it is not rate limited: given
        # enough attempts, somebody walks the keyspace until a discount comes
        # back. The refusal message is deliberately identical for every
        # failure reason, so each guess is uninformative; this makes guessing
        # slow as well. Generous enough that a customer mistyping a code three
        # times never notices.
        "coupon": os.getenv("THROTTLE_RATE_COUPON", "20/min"),
        # One support request is a person typing a paragraph. A hundred is
        # either a script or a mistake, and both fill an inbox somebody has to
        # empty by hand.
        "support": os.getenv("THROTTLE_RATE_SUPPORT", "10/hour"),
    },
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "SendStack API",
    "DESCRIPTION": "Multi-tenant WhatsApp business messaging & automation platform.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

SIMPLE_JWT = {
    # os.getenv(name, default) only falls back when the var is truly unset -- Docker Compose's
    # env_file passes through JWT_SECRET= (present but empty) as an empty string, so fall back
    # explicitly rather than ever signing tokens with an empty key.
    "SIGNING_KEY": os.getenv("JWT_SECRET") or SECRET_KEY,
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(os.getenv("JWT_ACCESS_TOKEN_MINUTES", "15"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.getenv("JWT_REFRESH_TOKEN_DAYS", "7"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
)


# --- Failed-login protection -------------------------------------------
#
# The DRF "auth" throttle rate above counts requests per client address, which
# is the wrong axis on its own: an attacker with a proxy pool spends one
# request per address and never trips it. These count attempts against a single
# account instead, wherever they come from -- see core.lockout.
#
# Ten is deliberately generous. It is far below what a guessing run needs and
# well above what somebody mistyping a password reaches, and the lockout is a
# fixed fifteen minutes from the *first* failure rather than a window the
# attacker can keep extending, so a locked-out user is never stuck for longer
# than that. Set AUTH_MAX_FAILED_ATTEMPTS=0 to disable the mechanism entirely.
AUTH_MAX_FAILED_ATTEMPTS = int(os.getenv("AUTH_MAX_FAILED_ATTEMPTS", "10"))
AUTH_LOCKOUT_SECONDS = int(os.getenv("AUTH_LOCKOUT_SECONDS", str(15 * 60)))

# Where the Django admin is mounted, without a leading slash. Moving it off the
# default is not a security control by itself -- it is one fewer path a mass
# scanner finds, and costs nothing.
DJANGO_ADMIN_URL = os.getenv("DJANGO_ADMIN_URL", "admin/").strip().lstrip("/")

# Addresses allowed to reach the admin at all, as a comma-separated list of
# plain IPs. Empty (the default) means no restriction, because a deployment
# whose operators do not have fixed addresses would otherwise lock itself out
# of its own admin -- see core.middleware.AdminSecurityMiddleware, which also
# applies a failed-login lockout to the admin whether or not this is set.
ADMIN_ALLOWED_IPS = env_list("ADMIN_ALLOWED_IPS")


# Logging

# Whether phone numbers are masked in log output (+91******3210). Off here
# because the base settings are shared with development, where the full number
# is usually the thing being debugged; production.py turns it on. See
# core.logging_filters.PhoneMaskingFilter.
LOG_MASK_PHONE_NUMBERS = env_bool("LOG_MASK_PHONE_NUMBERS", False)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {
            "()": "core.logging_filters.RequestIDFilter",
        },
        "mask_phones": {
            "()": "core.logging_filters.PhoneMaskingFilter",
        },
    },
    "formatters": {
        "standard": {
            "format": "%(asctime)s level=%(levelname)s request_id=%(request_id)s logger=%(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            # Order matters: the request id is added, then the record's message
            # and arguments are masked. Both run before the formatter sees it.
            "filters": ["request_id", "mask_phones"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("ROOT_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "sendstack": {
            "handlers": ["console"],
            "level": os.getenv("APP_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        # redis-py 8's RESP3 "maintenance notifications" handshake
        # (CLIENT MAINT_NOTIFICATIONS) isn't supported by OSS Redis 7, so it
        # logs a harmless per-connection fallback message at DEBUG -- keep
        # that logger above DEBUG regardless of ROOT_LOG_LEVEL.
        "redis.connection": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
