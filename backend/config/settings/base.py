"""
Base Django settings for the MultiSender project.

Shared by every environment. Environment-specific overrides live in
``development.py`` and ``production.py``, which both start with
``from .base import *``.
"""

import os
from datetime import timedelta
from pathlib import Path

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

    # MultiSender apps
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
    "apps.voice",
    "apps.realtime",
    "apps.leadgen",
    "apps.reports",
    "apps.credits",
    "apps.subscriptions",
    "apps.notifications",
    "apps.teams",
    "apps.audit",
    "apps.health",
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
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@multisender.local")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")


# Redis / Cache / Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

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

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = int(os.getenv("CELERY_WORKER_PREFETCH_MULTIPLIER", "4"))

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
    # Lead scores decay with time and contacts go quiet with nothing
    # happening, so neither has an event to react to -- see apps/crm/tasks.py.
    # Daily by default: the recency bands are measured in days, so a more
    # frequent pass would cost a full table scan to change almost nothing.
    "refresh-crm-signals": {
        "task": "apps.crm.tasks.refresh_crm_signals",
        "schedule": int(os.getenv("CRM_REFRESH_INTERVAL_SECONDS", str(24 * 60 * 60))),
    },
}


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
# every environment by default -- no bridge ships with MultiSender, so QR
# connections report "QR provider is not configured" until one is deployed
# and pointed at here.
QR_PROVIDER_BASE_URL = os.getenv("QR_PROVIDER_BASE_URL", "")
QR_PROVIDER_API_KEY = os.getenv("QR_PROVIDER_API_KEY", "")
QR_PROVIDER_WEBHOOK_SECRET = os.getenv("QR_PROVIDER_WEBHOOK_SECRET", "")

# How long a generated QR code stays valid before the account moves to
# EXPIRED and the user must request a new one.
WHATSAPP_QR_SESSION_TTL_SECONDS = int(os.getenv("WHATSAPP_QR_SESSION_TTL_SECONDS", "99999999999999999999"))


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


# AI agent
#
# A tenant can store their own key (encrypted, per tenant) from the AI page;
# AI_API_KEY is the deployment-wide fallback for a single-tenant install that
# would rather configure this once in the environment. With neither set the
# agent simply never speaks -- it is off by default in any case.
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_REQUEST_TIMEOUT_SECONDS = int(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "30"))


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
        "apps.accounts.authentication.MultiSenderJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
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
        "whatsapp_connect": os.getenv("THROTTLE_RATE_WHATSAPP_CONNECT", "10/min"),
    },
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "MultiSender API",
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


# Logging

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {
            "()": "core.logging_filters.RequestIDFilter",
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
            "filters": ["request_id"],
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
        "multisender": {
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
