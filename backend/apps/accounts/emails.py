"""
The mail this application sends to a person about their own account.

Three messages, and between them they are the whole of a customer's ability to
recover and to notice a break-in:

* the reset link,
* "your password was changed" -- the one that lets somebody catch a takeover
  they did not perform,
* nothing else. A security mailbox that also carries marketing is a mailbox
  people filter away.

Two rules the templates enforce and this module keeps enforcing:

* **A credential is never in the body.** Not the old password, not the new one,
  not a token in a log line. The reset link is the single exception, and it is
  the reason the link is single-use and short-lived.
* **Nothing internal leaks.** No tenant id, no user id, no database detail --
  a mail forwarded to a colleague, or read by whoever else has that mailbox,
  should say nothing about the shape of the system.

Everything here is best-effort at the call site's discretion: `send_security_*`
returns whether the mail went, and a failure is logged rather than raised, so a
mail server outage cannot stop somebody resetting a password they still need.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger("sendstack")


def _brand(user) -> dict:
    """
    How this mail should sign itself, and where it should send somebody for help.

    A reseller's customer must not be told to email the platform, so the
    tenant's resolved branding wins over the deployment-wide defaults -- the
    same inheritance the UI uses (tenants.TenantBranding.resolve).
    """
    product_name = "SendStack"
    support_email = getattr(settings, "SUPPORT_EMAIL", "") or ""

    tenant = getattr(user, "tenant", None)
    if tenant is not None:
        try:
            from apps.tenants.models import TenantBranding

            resolved = TenantBranding.resolve(tenant)
            product_name = resolved.get("product_name") or product_name
            support_email = resolved.get("support_email") or support_email
        except Exception:  # noqa: BLE001 - branding is decoration on a mail, never a reason not to send it
            logger.exception("Could not resolve branding for an account email; using defaults.")

    return {
        "product_name": product_name,
        "support_email": support_email,
        "support_url": getattr(settings, "SUPPORT_URL", "") or "",
        "site_url": getattr(settings, "FRONTEND_URL", ""),
    }


def _send(user, subject, template_stem, context) -> bool:
    """Render the text/HTML pair and send it. Never raises."""
    if not getattr(user, "email", ""):
        logger.warning("No email address on this account; skipping the %s mail.", template_stem)
        return False

    payload = {**_brand(user), **context, "username": user.get_username()}
    try:
        text_body = render_to_string(f"accounts/{template_stem}.txt", payload)
        html_body = render_to_string(f"accounts/{template_stem}.html", payload)
        message = EmailMultiAlternatives(
            subject=subject.format(product_name=payload["product_name"]),
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
        return True
    except Exception:  # noqa: BLE001 - see the module docstring
        # The address is deliberately absent from this line, and so is the
        # token: a mail failure must not put either into the logs.
        logger.exception("Could not send the %s email for user id=%s.", template_stem, getattr(user, "pk", "?"))
        return False


def send_password_reset(user, reset_url: str, expires_in_hours: int) -> bool:
    return _send(
        user,
        "Reset your {product_name} password",
        "password_reset",
        {"reset_url": reset_url, "expires_in_hours": expires_in_hours},
    )


def send_password_changed(user, *, reason: str = "changed") -> bool:
    """
    Tell somebody their password moved.

    Sent after the change and not before it, and sent whether the change came
    from a reset link or from the settings page: the value of this mail is
    entirely in reaching a person who did *not* do it, while the session that
    did it is still fresh enough to act on.
    """
    return _send(
        user,
        "Your {product_name} password was changed",
        "password_changed",
        {"reason": reason},
    )
