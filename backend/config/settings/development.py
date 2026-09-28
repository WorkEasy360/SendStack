import socket
import sys
from urllib.parse import urlsplit

from .base import *  # noqa: F401,F403
from .base import env_bool, env_list

DEBUG = True

if not SECRET_KEY:  # noqa: F405
    SECRET_KEY = "django-insecure-development-only-key-please-change-me-1234567890"  # noqa: F405

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")

CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", True)

LOGGING["root"]["level"] = "DEBUG"  # noqa: F405

# Test runs shouldn't depend on live Redis or a running Celery worker: fall
# back to an in-memory cache and execute tasks inline so `manage.py test` /
# pytest work without Docker and campaign/messaging flows are testable
# end-to-end with plain function calls (`.delay()` runs synchronously).
_TESTING = "test" in sys.argv or "pytest" in sys.modules

if _TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}  # noqa: F405
    CELERY_TASK_ALWAYS_EAGER = True  # noqa: F405
    # Same reasoning: under docker-compose QR_PROVIDER_BASE_URL points at a
    # real, running qr-bridge, so leaving it set here would make the QR
    # provider tests (which mock NotConfiguredProvider, the test-env
    # fallback) silently hit the live bridge instead and spin up real,
    # never-scanned Baileys sessions on every test run.
    QR_PROVIDER_BASE_URL = ""  # noqa: F405

    # Same reasoning again, and the sharpest case of it: under docker-compose
    # the backend reads the deployment's .env, so a site that has switched
    # knowledge-base-only answering on would silently run the whole suite in
    # strict mode -- changing the fallback wording, the relevance floor and
    # whether tools suspend the gate. The suite would then pass or fail
    # depending on an operator's environment file, which makes it evidence of
    # nothing.
    #
    # Pinned off, so the baseline is the permissive behaviour and every strict
    # expectation has to say so: see apps/ai/tests_knowledge_only.py, which
    # opts in with @override_settings(AI_KNOWLEDGE_ONLY=True).
    AI_KNOWLEDGE_ONLY = False  # noqa: F405
    AI_KNOWLEDGE_MIN_RELEVANCE = 0.15  # noqa: F405


# --- Cache: don't hard-depend on a Redis this process cannot reach ------
#
# docker-compose deliberately does not publish Redis: it holds the rate-limit
# counters, the failed-login counters and the Celery queue, and nothing outside
# that network has any business reaching it. That is the right call, and this
# does not change it.
#
# But it means a `runserver` on the *host* -- the frontend dev setup, where
# Vite on :5173 proxies to :8000 -- has no route to Redis at all, while still
# inheriting base.py's django_redis cache. DRF throttles every request and
# throttling reads and writes the cache, so every single API call opened a
# doomed Redis connection first.
#
# On Linux that is a cheap instant ECONNREFUSED. On Windows it is not: a
# refused loopback connection costs ~2s of SYN retransmission, and ~4s through
# the name `localhost`, which tries ::1 before 127.0.0.1. With
# IGNORE_EXCEPTIONS swallowing the error, the result was a dev server that
# answered every request correctly about three seconds late and logged nothing
# to say why -- the whole UI simply felt broken.
#
# So: probe once, at import, with a short timeout, and fall back to a local
# cache when Redis is genuinely unreachable. Under docker-compose the sibling
# container answers immediately and the real cache is used exactly as before,
# which is what keeps this from quietly hiding a Redis outage from anyone
# testing the containerised stack.
def _redis_reachable(url, timeout=0.25):
    parts = urlsplit(url)
    host, port = parts.hostname or "localhost", parts.port or 6379
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


if not _TESTING and not _redis_reachable(REDIS_URL):  # noqa: F405
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    # Written straight to stderr rather than through `print`, and rather than
    # through the logger: settings are imported before logging is configured,
    # so a logger call here would go nowhere -- and `print` is forbidden in
    # application code (see tests.test_deployment_security.NoDebugPrintTests),
    # for the good reason that it bypasses every filter a deployment set up.
    sys.stderr.write(
        "development settings: Redis is unreachable, using an in-memory cache. "
        "Rate limiting is per-process and will not be shared. "
        "Run the app under docker-compose if you need the real thing.\n"
    )
