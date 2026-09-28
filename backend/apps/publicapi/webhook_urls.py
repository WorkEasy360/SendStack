"""
Where a webhook may be delivered.

A webhook URL is a tenant-supplied address this server then makes an outbound
POST to, carrying a payload about their business. That is the same server-side
request forgery primitive as the AI agent's `base_url`, so it gets the same
guard: `core.outbound_urls`, which is already the deployment's one policy for
"is this address ours to fetch" and already has the allowlist escape hatch an
operator needs for a self-hosted receiver.

A second module rather than calling it directly, for two reasons that both
matter at delivery time:

* the failure has to be a `PublicAPIError` with a code, not a `ValueError`, so
  a developer creating an endpoint gets the standard error envelope; and
* HTTPS is required in production. A webhook body carries message contents and
  phone numbers and is signed with a shared secret; sending that over plaintext
  HTTP hands all three to anybody on the path. Localhost over HTTP stays legal
  in development, because that is how anybody tests a receiver.
"""

from urllib.parse import urlsplit

from django.conf import settings

from core.outbound_urls import OutboundURLNotAllowed, check_outbound_url

from .errors import INVALID_REQUEST, PublicAPIError


def ensure_deliverable(url: str) -> str:
    """
    Raise unless this deployment will POST to `url`. Returns it unchanged.

    Called at write time from the serializer *and* again in the delivery task,
    which is not redundant: a hostname that resolved to a public address when
    the endpoint was saved can resolve to 127.0.0.1 an hour later, and the
    check that matters is the one immediately before the request.
    """
    if not url:
        raise PublicAPIError(INVALID_REQUEST, "A webhook URL is required.")

    parts = urlsplit(url.strip())

    if _require_https() and parts.scheme != "https":
        raise PublicAPIError(
            INVALID_REQUEST,
            "Webhook URLs must use https. The payload carries message content and "
            "is signed with a shared secret; plaintext HTTP exposes both.",
        )

    try:
        return check_outbound_url(url, field="url")
    except OutboundURLNotAllowed as exc:
        raise PublicAPIError(INVALID_REQUEST, str(exc)) from exc


def _require_https() -> bool:
    """
    HTTPS is mandatory unless this deployment says otherwise.

    Defaults to `not DEBUG`, so production requires it without an operator
    having to know the setting exists, and a developer running the stack
    locally can point a webhook at their own machine without one.
    """
    configured = getattr(settings, "PUBLIC_API_REQUIRE_HTTPS_WEBHOOKS", None)
    if configured is None:
        return not settings.DEBUG
    return bool(configured)
