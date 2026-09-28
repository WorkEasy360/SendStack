import os

from .base import *  # noqa: F401,F403
from .base import env_bool, env_list

DEBUG = False

if not SECRET_KEY:  # noqa: F405
    raise RuntimeError("SECRET_KEY environment variable must be set in production.")

if not ALLOWED_HOSTS:  # noqa: F405
    raise RuntimeError("ALLOWED_HOSTS environment variable must be set in production.")

# The container's own Docker HEALTHCHECK talks to itself over plain http://localhost,
# regardless of what real domain(s) the operator configures above.
ALLOWED_HOSTS = ALLOWED_HOSTS + ["localhost"]  # noqa: F405

STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"


# --- Transport security -------------------------------------------------
#
# On by default. This deployment carries phone numbers, customer conversations
# and WhatsApp credentials, none of which belong on a plaintext connection, and
# a security setting that has to be remembered is one that gets forgotten.
#
# There is exactly one situation in which it is correct to turn this off:
# nothing in front of nginx terminates TLS yet, in which case every request
# redirects to an https port nothing is listening on and the site is down. That
# is what SECURE_SSL_REDIRECT=false in the environment is for -- an explicit,
# visible decision in a deployment that has not got a certificate yet, rather
# than the silent default it used to be.
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)

# The container healthchecks curl themselves over plain http on localhost. Left
# to redirect, they would follow a 301 to an https port that is not open inside
# the container, mark the container unhealthy, and take the whole stack down as
# each dependent service waits on a health condition that can never be met.
SECURE_REDIRECT_EXEMPT = [r"^health/"]

# nginx is the only thing that can reach gunicorn (the backend container
# publishes no port), and it sets X-Forwarded-Proto on every proxied request --
# so this header is trustworthy here in a way it would not be if the app server
# were directly reachable. Without it, Django behind a TLS-terminating proxy
# thinks every request is plain http and SECURE_SSL_REDIRECT loops forever.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Django 4+ requires the origin of any unsafe cross-origin POST to be listed,
# and behind a proxy the admin login is exactly that. Derived from ALLOWED_HOSTS
# so a correct deployment needs no second list, and overridable for the case
# where the public origin differs from the host header.
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS") or [
    f"https://{host}" for host in ALLOWED_HOSTS if host not in ("localhost", "*")  # noqa: F405
]

SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True


# --- Cookies ------------------------------------------------------------
#
# The SPA authenticates with JWTs and not with these, but the Django admin runs
# on the same origin and does, so its session cookie is worth the same care.
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
# Nothing in this deployment reads the CSRF cookie from JavaScript -- the admin
# templates embed the token in the form itself -- so it can be closed off.
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"


# --- Response headers ---------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
# Django's own default, set explicitly: a referer is sent in full within the
# site and only the origin when leaving it, so a link tapped out of the app
# never carries a tenant id or a code in its path to a third party.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

CORS_ALLOW_ALL_ORIGINS = False

LOGGING["root"]["level"] = os.getenv("ROOT_LOG_LEVEL", "WARNING")  # noqa: F405

# --- Log hygiene --------------------------------------------------------
#
# Production logs are shipped, aggregated, searched and retained by whatever
# platform this runs on, and they are read by more people than the database
# is. A full recipient number on every send line turns that log into a
# contact list. Masked, it still identifies a message uniquely enough to
# diagnose one -- see core.logging_filters.PhoneMaskingFilter.
LOG_MASK_PHONE_NUMBERS = env_bool("LOG_MASK_PHONE_NUMBERS", True)


# --- Email --------------------------------------------------------------
#
# Production must not pretend to send mail. The console backend is the default
# in base.py because it is right for development, and inherited unchanged here
# it would mean every password-reset link is printed to a container log and
# never delivered -- a customer locked out of their account with the
# application reporting success.
#
# So: an SMTP backend in production has to be configured, and a deployment that
# has not configured one must say so at startup rather than at the moment
# somebody needs a reset link.
if EMAIL_BACKEND == "django.core.mail.backends.console.EmailBackend":  # noqa: F405
    raise RuntimeError(
        "EMAIL_BACKEND is still the development console backend. Production sends "
        "password-reset and security email; configure SMTP (EMAIL_BACKEND, EMAIL_HOST, "
        "EMAIL_HOST_USER, EMAIL_HOST_PASSWORD) or set it explicitly to "
        "django.core.mail.backends.dummy.EmailBackend to accept that no mail is sent."
    )

if EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend":  # noqa: F405
    missing = [
        name for name, value in (
            ("EMAIL_HOST", EMAIL_HOST),  # noqa: F405
            ("EMAIL_HOST_USER", EMAIL_HOST_USER),  # noqa: F405
            ("EMAIL_HOST_PASSWORD", EMAIL_HOST_PASSWORD),  # noqa: F405
        )
        if not value
    ]
    if missing:
        # Names only. Never the values -- this message reaches container logs.
        raise RuntimeError(
            "SMTP email is configured but incomplete. Missing: " + ", ".join(missing)
        )

# The link in a reset email is absolute and is built from this, so a wrong
# value means a mail nobody can act on. localhost is the development default
# from base.py and is never right in production.
if not FRONTEND_URL or "localhost" in FRONTEND_URL:  # noqa: F405
    raise RuntimeError(
        "FRONTEND_URL must be the public URL of the application in production -- "
        "password-reset and unsubscribe links are built from it."
    )


# --- Error monitoring ---------------------------------------------------
#
# Started here rather than in wsgi.py so the Celery workers -- which load
# settings and never touch wsgi -- get it too. No-ops when SENTRY_DSN is unset.
from core.monitoring import configure as _configure_monitoring  # noqa: E402

ERROR_MONITORING_ENABLED = _configure_monitoring()
