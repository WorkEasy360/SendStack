import sys

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
if "test" in sys.argv or "pytest" in sys.modules:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}  # noqa: F405
    CELERY_TASK_ALWAYS_EAGER = True  # noqa: F405
    # Same reasoning: under docker-compose QR_PROVIDER_BASE_URL points at a
    # real, running qr-bridge, so leaving it set here would make the QR
    # provider tests (which mock NotConfiguredProvider, the test-env
    # fallback) silently hit the live bridge instead and spin up real,
    # never-scanned Baileys sessions on every test run.
    QR_PROVIDER_BASE_URL = ""  # noqa: F405
