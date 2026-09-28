"""
Production error monitoring, and the scrubbing that makes it safe to use.

An error tracker is the only practical answer to "can we detect production
errors" -- logs on a container that restarts are not one. But an error tracker
is also the fastest way to move customer data somewhere it was never meant to
go: by default Sentry sends request bodies, headers, cookies, local variables
and the query string of every failing request, and in this application those
carry WhatsApp tokens, message bodies and phone numbers.

So the integration is deliberately shaped in the opposite direction from the
defaults:

* ``send_default_pii=False`` -- no cookies, no user email, no client address.
* ``request_bodies="never"`` -- a failing send never ships the message.
* ``before_send`` below runs last and removes anything the above still let
  through, matching on *key name* rather than on a value pattern, because a
  secret is recognisable by what it is called and not by what it looks like.

Entirely optional. With ``SENTRY_DSN`` unset -- which is every development
machine and every test run -- ``configure()`` returns having done nothing, and
the package need not even be installed.
"""

import logging
import os
import re

logger = logging.getLogger("sendstack")

#: Substrings that make a key too sensitive to report, matched case-insensitively
#: against the *key*. Deliberately broad: a false positive costs one redacted
#: debugging field, a false negative puts a live credential in a third party's
#: database where it cannot be recalled.
SENSITIVE_KEY_PARTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
    "auth", "credential", "session", "cookie", "csrf", "signature", "encryption",
    "private", "access_key", "refresh", "otp", "pin",
)

#: Message content and personal data. Not a credential, but not something a
#: customer agreed to have forwarded to a monitoring vendor either.
CUSTOMER_DATA_KEYS = (
    "body", "message", "caption", "text", "transcript", "prompt", "content",
    "phone", "to_phone", "from_phone", "mobile", "email", "recipient",
)

REDACTED = "[redacted]"

#: +919876543210 -> +91******3210. Enough to tell two recipients apart in a
#: bug report and not enough to message either of them.
_PHONE_RE = re.compile(r"\+?\d{7,15}")


def mask_phone(value) -> str:
    """A phone number with its middle removed, for a log line or an error report."""
    text = str(value or "")
    digits = re.sub(r"\D", "", text)
    if len(digits) < 7:
        return REDACTED
    prefix = "+" if text.strip().startswith("+") else ""
    return f"{prefix}{digits[:2]}{'*' * (len(digits) - 6)}{digits[-4:]}"


def _mask_phones_in_text(text: str) -> str:
    return _PHONE_RE.sub(lambda match: mask_phone(match.group(0)), text)


def _is_sensitive(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _is_customer_data(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in CUSTOMER_DATA_KEYS)


def scrub(value, _depth=0):
    """
    Recursively redact an event payload.

    Depth-bounded because a Sentry event can carry a self-referential structure
    (a frame's locals holding the frame), and a scrubber that hangs the error
    handler is worse than no scrubber at all.
    """
    if _depth > 12:
        return REDACTED

    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _is_sensitive(key):
                cleaned[key] = REDACTED
            elif _is_customer_data(key):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = scrub(item, _depth + 1)
        return cleaned

    if isinstance(value, (list, tuple)):
        cleaned = [scrub(item, _depth + 1) for item in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned

    if isinstance(value, str):
        return _mask_phones_in_text(value)

    return value


def before_send(event, hint):
    """The last thing that touches an event before it leaves this process."""
    try:
        for section in ("request", "extra", "contexts", "tags", "breadcrumbs"):
            if section in event:
                event[section] = scrub(event[section])

        # Stack-frame locals are where a `password` variable or a raw provider
        # payload actually lives, and they are not part of the sections above.
        for exception in (event.get("exception") or {}).get("values") or []:
            for frame in (exception.get("stacktrace") or {}).get("frames") or []:
                if "vars" in frame:
                    frame["vars"] = scrub(frame["vars"])

        if isinstance(event.get("logentry"), dict):
            event["logentry"] = scrub(event["logentry"])

        # An identified user is useful for "how many customers hit this"; their
        # mailbox and address are not ours to forward.
        user = event.get("user")
        if isinstance(user, dict):
            event["user"] = {
                key: user[key] for key in ("id", "tenant_id") if key in user
            }
        return event
    except Exception:  # noqa: BLE001 - a broken scrubber must drop the event, never ship it unscrubbed
        logger.exception("Error-report scrubbing failed; dropping the event rather than sending it.")
        return None


def configure() -> bool:
    """
    Start error reporting if this deployment has been given somewhere to report to.

    Returns whether it was switched on, which is what the production-readiness
    check reports. Never raises: a monitoring backend that cannot start is a
    reason to log loudly, not a reason for the application to refuse to boot.
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; error monitoring is off. "
            "Add sentry-sdk to requirements/production.txt."
        )
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("APP_ENVIRONMENT", "production"),
            release=os.getenv("APP_BUILD") or os.getenv("APP_VERSION") or None,
            integrations=[
                DjangoIntegration(),
                CeleryIntegration(),
                # WARNING and above becomes an event; INFO is kept only as
                # breadcrumbs. Reporting every INFO line would bury the
                # failures this exists to surface.
                LoggingIntegration(level=logging.INFO, event_level=logging.WARNING),
            ],
            # Off by default: performance traces sample real request URLs and
            # database queries, which is a second, quieter data-export path.
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
            send_default_pii=False,
            max_request_body_size="never",
            before_send=before_send,
        )
        logger.info("Error monitoring is on.")
        return True
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Could not start error monitoring; continuing without it.")
        return False
