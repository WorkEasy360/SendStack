"""
What a customer is told about their own billing, and where.

Two channels, and they are not interchangeable. The in-app notification is
where a billing state change belongs by default: the person is already looking
at the product, the message is one line, and it costs nobody an inbox. Email
is for the messages that have to reach somebody who is *not* looking -- a
trial ending, a payment that failed -- because those are the ones where not
knowing costs the customer something.

Rules this module keeps:

* **Nothing sensitive.** No payment token, no card detail, no provider id, no
  internal identifier. A billing mail says what changed and where to go.
* **Not one message per event.** Every send here is claimed through
  `SubscriptionNotice` by the caller first, so a sweep that runs twice cannot
  mail twice. This module trusts that and does not re-check.
* **Never raises.** Every caller has already committed the state change this
  is describing. A mail failure is logged and dropped, never propagated.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from apps.notifications.services import notify
from apps.notifications.models import Notification

from .models import SubscriptionNotice

logger = logging.getLogger("sendstack")


#: kind -> (subject, in-app title, the sentence, where to send them)
_COPY = {
    SubscriptionNotice.Kind.TRIAL_ENDING: (
        "Your {product_name} trial ends soon",
        "Your trial ends soon",
        "Choose a plan to keep sending once your trial ends. Everything you have created stays where it is.",
        "/billing",
    ),
    SubscriptionNotice.Kind.TRIAL_EXPIRED: (
        "Your {product_name} trial has ended",
        "Your trial has ended",
        "Your contacts, campaigns and history are all still here. Choose a plan to start sending again.",
        "/billing",
    ),
    SubscriptionNotice.Kind.SUBSCRIPTION_ACTIVATED: (
        "Your {product_name} subscription is active",
        "Subscription active",
        "Your plan is active and your allowances have been reset for the new period.",
        "/billing",
    ),
    SubscriptionNotice.Kind.PAYMENT_FAILED: (
        "A payment for {product_name} did not go through",
        "Payment failed",
        "We could not take the last payment for your plan. Your workspace keeps working for now.",
        "/billing",
    ),
    SubscriptionNotice.Kind.SUBSCRIPTION_CANCELLED: (
        "Your {product_name} subscription has been cancelled",
        "Subscription cancelled",
        "You keep full access until the end of the period you have already paid for.",
        "/billing",
    ),
}


def workspace_recipients(tenant):
    """
    Who in a workspace hears about its billing.

    Its own active users, and only its own: a reseller's other customers,
    the reseller's staff and platform administrators are all somebody else,
    and a billing mail is a private fact about one business.
    """
    from django.contrib.auth import get_user_model

    return list(
        get_user_model().objects.filter(
            tenant_id=tenant.pk if hasattr(tenant, "pk") else tenant,
            is_active=True,
            is_deleted=False,
        )
    )


def send_billing_notice(subscription, kind) -> int:
    """
    Deliver one billing notice to a workspace. Returns how many people it reached.

    The caller has already claimed the right to send this (see
    `SubscriptionNotice.claim`); this only decides how to say it.
    """
    copy = _COPY.get(kind)
    if copy is None:
        logger.warning("No copy defined for billing notice %s.", kind)
        return 0

    subject_template, title, body, link = copy
    recipients = workspace_recipients(subscription.tenant)
    if not recipients:
        logger.info("Workspace %s has no active users to notify.", subscription.tenant_id)
        return 0

    context = _context(subscription, title, body, link)

    for user in recipients:
        # In-app first, and unconditionally: it cannot fail in a way that
        # matters and it is the copy the customer sees when they next open
        # the product, whatever their mail provider did.
        try:
            notify(user, kind=Notification.Kind.SYSTEM, title=title, body=body, link=link)
        except Exception:  # noqa: BLE001 - a notification row is not worth a failed sweep
            logger.exception("Could not write the in-app billing notice for user %s.", user.pk)

    _email(recipients, subject_template, context)
    return len(recipients)


def _context(subscription, title, body, link) -> dict:
    from apps.tenants.models import TenantBranding

    try:
        brand = TenantBranding.resolve(subscription.tenant)
    except Exception:  # noqa: BLE001 - branding is decoration
        brand = dict(TenantBranding.DEFAULTS)

    frontend = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return {
        "product_name": brand.get("product_name") or "SendStack",
        "support_email": brand.get("support_email") or getattr(settings, "SUPPORT_EMAIL", "") or "",
        "support_url": getattr(settings, "SUPPORT_URL", "") or "",
        "title": title,
        "body": body,
        "action_url": f"{frontend}{link}" if frontend else "",
        "plan_name": subscription.plan.name,
        # A date, not a timestamp: nobody needs to know a trial ended at
        # 03:41:07 UTC, and a bare date reads the same in every timezone.
        "trial_ends_on": subscription.trial_ends_at.date().isoformat() if subscription.trial_ends_at else "",
        "period_ends_on": (
            subscription.current_period_end.date().isoformat() if subscription.current_period_end else ""
        ),
    }


def _email(recipients, subject_template, context) -> None:
    addresses = sorted({user.email for user in recipients if getattr(user, "email", "")})
    if not addresses:
        return

    subject = subject_template.format(product_name=context["product_name"])
    try:
        text_body = render_to_string("subscriptions/billing_notice.txt", context)
        html_body = render_to_string("subscriptions/billing_notice.html", context)
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            # bcc, not to: everyone in a workspace can see the billing page
            # anyway, but putting their colleagues' addresses in a header is
            # disclosing something the product was not asked to disclose.
            to=[],
            bcc=addresses,
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("Could not send the billing notice email (%s recipients).", len(addresses))
