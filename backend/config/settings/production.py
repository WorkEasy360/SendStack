import os

from .base import *  # noqa: F401,F403

DEBUG = False

if not SECRET_KEY:  # noqa: F405
    raise RuntimeError("SECRET_KEY environment variable must be set in production.")

if not ALLOWED_HOSTS:  # noqa: F405
    raise RuntimeError("ALLOWED_HOSTS environment variable must be set in production.")

# The container's own Docker HEALTHCHECK talks to itself over plain http://localhost,
# regardless of what real domain(s) the operator configures above.
ALLOWED_HOSTS = ALLOWED_HOSTS + ["localhost"]  # noqa: F405

STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Off by default: the bundled docker-compose/nginx setup does not terminate TLS itself.
# Only enable this once a real certificate is terminated in front of nginx (e.g. a
# managed load balancer, Cloudflare, or nginx + certbot), otherwise every request loops
# into a redirect to an https port nothing is listening on.
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "false").lower() == "true"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CORS_ALLOW_ALL_ORIGINS = False

LOGGING["root"]["level"] = os.getenv("ROOT_LOG_LEVEL", "WARNING")  # noqa: F405
