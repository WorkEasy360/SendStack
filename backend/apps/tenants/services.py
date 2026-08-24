"""
What a reseller needs to know about the customers underneath them.

Everything here is aggregated per tenant in a handful of grouped queries
rather than per row, because the whole point of the customer list is to open
it with fifty customers on it and not wait.
"""

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q, Sum

from .models import Tenant


def _counts_by_tenant(queryset, tenant_ids, **annotations):
    """`{tenant_id: {annotation: value}}` for one grouped query."""
    rows = queryset.filter(tenant_id__in=tenant_ids).values("tenant_id").annotate(**annotations)
    return {row["tenant_id"]: row for row in rows}


def customer_summaries(parent_tenant_id, *, include_all=False) -> list:
    """
    One row per customer: who they are, what they are on, and what they used.

    `include_all` is for a superadmin, who is looking at every tenant rather
    than at their own children.
    """
    from apps.contacts.models import Contact
    from apps.messaging.models import Message
    from apps.subscriptions.models import Subscription
    from apps.whatsapp.models import WhatsAppAccount

    tenants = Tenant.objects.all() if include_all else Tenant.objects.filter(parent_id=parent_tenant_id)
    tenants = list(tenants.order_by("name"))
    tenant_ids = [tenant.pk for tenant in tenants]
    if not tenant_ids:
        return []

    user_model = get_user_model()
    users = {
        row["tenant_id"]: row
        for row in user_model.objects.filter(tenant_id__in=tenant_ids, is_active=True)
        .values("tenant_id")
        .annotate(total=Count("id"), credits=Sum("credits"), reserved=Sum("reserved_credits"))
    }
    contacts = _counts_by_tenant(Contact.objects, tenant_ids, total=Count("id"))
    devices = _counts_by_tenant(
        WhatsAppAccount.objects, tenant_ids,
        total=Count("id"),
        connected=Count("id", filter=Q(status=WhatsAppAccount.Status.CONNECTED)),
    )
    messages = _counts_by_tenant(
        Message.objects.filter(direction=Message.Direction.OUTBOUND), tenant_ids,
        sent=Count("id", filter=Q(status__in=[Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ])),
        failed=Count("id", filter=Q(status=Message.Status.FAILED)),
        last_at=Max("queued_at"),
    )
    subscriptions = {
        subscription.tenant_id: subscription
        for subscription in Subscription.objects.filter(tenant_id__in=tenant_ids).select_related("plan")
    }

    summaries = []
    for tenant in tenants:
        user_row = users.get(tenant.pk, {})
        message_row = messages.get(tenant.pk, {})
        subscription = subscriptions.get(tenant.pk)

        summaries.append({
            "tenant_id": tenant.pk,
            "name": tenant.name,
            "kind": tenant.kind,
            "is_active": tenant.is_active,
            "created_at": tenant.created_at,
            "users": user_row.get("total", 0),
            # What they can still spend, which is the number that matters when
            # deciding whether to top somebody up.
            "available_credits": (user_row.get("credits") or 0) - (user_row.get("reserved") or 0),
            "contacts": contacts.get(tenant.pk, {}).get("total", 0),
            "devices": devices.get(tenant.pk, {}).get("total", 0),
            "connected_devices": devices.get(tenant.pk, {}).get("connected", 0),
            "messages_sent": message_row.get("sent", 0),
            "messages_failed": message_row.get("failed", 0),
            "last_activity_at": message_row.get("last_at"),
            "plan": subscription.plan.name if subscription else None,
            "plan_id": subscription.plan_id if subscription else None,
            "subscription_id": subscription.pk if subscription else None,
            "subscription_status": subscription.status if subscription else None,
        })

    return summaries
