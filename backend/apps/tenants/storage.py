"""
What one workspace is actually storing, and the one thing it may clean up.

Two questions, and they are not the same question:

  * **Where has it all gone?** A customer whose media library is near its
    plan's ceiling has no way to find out which of six things is responsible.
    `usage_for` answers that by category, in rows, bytes, or both, and is
    read-only.

  * **What can be removed?** Almost nothing, and that is the point of this
    module rather than an omission from it. `prune_campaign_data` removes
    *finished campaigns and the message rows under them*, past a horizon the
    workspace itself sets, and nothing else ever.

    NOTHING HERE CAN REACH:

      users, subscriptions, plans, credits, the credit ledger, payments,
      checkouts, invoices, contacts, consent events, audit events,
      impersonation sessions, legal acceptances, WhatsApp accounts,
      conversations, inbox messages, templates, automation rules, the media
      library, or any campaign that has not finished.

    Those are not protected by a flag that could be turned off; there is
    simply no rule that names them, which is the same guarantee
    ``apps.health.retention`` makes and for the same reason. The deployment-
    wide retention rules in that module and this per-workspace one are
    deliberately separate: one is the operator's policy about their own
    infrastructure, the other is a customer's choice about their own history.

Deletion is batched by primary key, exactly as `health.retention` does it, so
a workspace clearing a year of campaigns does not hold a lock long enough to
stall the numbers sending through the same tables.
"""

import logging

from django.db.models import Count, Sum
from django.utils import timezone

logger = logging.getLogger("sendstack")

#: Rows removed per statement. Same reasoning as health.retention: short
#: transactions keep the message tables available to the send pipeline.
DELETE_BATCH_SIZE = 1000

#: A ceiling on one pass. A first run against a workspace that has never
#: pruned would otherwise run for a long time holding a connection; the next
#: scheduled pass picks up where this one stopped.
MAX_CAMPAIGNS_PER_PASS = 500

#: Campaign states that are over. A RUNNING, QUEUED, SCHEDULED or PAUSED
#: campaign is never touched however old it is -- a paused campaign is one
#: somebody intends to resume, and deleting it would be deleting work in
#: progress.
FINISHED_STATUSES = ("COMPLETED", "CANCELLED", "FAILED")

#: Rough bytes per row, used to turn a row count into something comparable
#: with a media library measured in megabytes. Deliberately an estimate and
#: labelled as one in the response: an exact answer would mean asking the
#: database for table statistics that are themselves approximations, per
#: tenant, on a page somebody opens to decide whether to clean up.
_ROW_BYTES = {
    "messages": 800,
    "campaigns": 1200,
    "contacts": 900,
    "reports": 400,
    "logs": 600,
}


def usage_for(tenant) -> dict:
    """
    A breakdown of what this workspace is storing.

    Media is measured exactly -- those are real files with a real size on
    disk, summed from the column the uploader already records. Everything else
    is a row count with an estimate beside it, and the response says which is
    which so the screen never presents an estimate as a measurement.
    """
    from apps.campaigns.models import Campaign
    from apps.contacts.models import Contact
    from apps.media_library.models import MediaAsset
    from apps.messaging.models import Message

    tenant_id = getattr(tenant, "pk", tenant)

    media = MediaAsset.objects.filter(tenant_id=tenant_id).aggregate(
        count=Count("id"), bytes=Sum("size_bytes"),
    )
    media_bytes = int(media["bytes"] or 0)

    outbound = Message.objects.filter(tenant_id=tenant_id, direction=Message.Direction.OUTBOUND).count()
    inbound = Message.objects.filter(tenant_id=tenant_id, direction=Message.Direction.INBOUND).count()
    campaigns = Campaign.objects.filter(tenant_id=tenant_id).count()
    contacts = Contact.objects.filter(tenant_id=tenant_id).count()

    categories = [
        {
            "key": "media",
            "label": "Media library",
            "count": int(media["count"] or 0),
            "bytes": media_bytes,
            "exact": True,
            "detail": "Images, video and documents you have uploaded for messages.",
        },
        {
            "key": "campaigns",
            "label": "Campaign data",
            # Campaigns *and* the message rows under them, because that is what
            # the estimate beside it measures. Counting only the campaigns
            # produced "0 items, ~12 KB" for a workspace whose campaigns had
            # been deleted but whose message rows had not -- a count that
            # contradicts the number next to it is worse than no count.
            "count": campaigns + outbound,
            "bytes": campaigns * _ROW_BYTES["campaigns"] + outbound * _ROW_BYTES["messages"],
            "exact": False,
            "detail": "Campaigns and the per-recipient message rows behind their reports.",
        },
        {
            "key": "received",
            "label": "Received messages",
            "count": inbound,
            "bytes": inbound * _ROW_BYTES["messages"],
            "exact": False,
            "detail": "Everything your customers have sent you. Never removed automatically.",
        },
        {
            "key": "contacts",
            "label": "Contacts",
            "count": contacts,
            "bytes": contacts * _ROW_BYTES["contacts"],
            "exact": False,
            "detail": "Your contact list. Never removed automatically.",
        },
        {
            "key": "reports",
            "label": "Reports",
            "count": campaigns,
            "bytes": campaigns * _ROW_BYTES["reports"],
            "exact": False,
            "detail": "Delivery and engagement history, derived from the message rows above.",
        },
        {
            "key": "logs",
            "label": "Activity log",
            "count": _audit_count(tenant_id),
            "bytes": _audit_count(tenant_id) * _ROW_BYTES["logs"],
            "exact": False,
            "detail": "Who did what, and when. Kept as a record and never removed automatically.",
        },
    ]

    used = sum(category["bytes"] for category in categories)
    limit = _storage_limit(tenant_id)

    return {
        "categories": categories,
        "used_bytes": used,
        "limit_bytes": limit,
        # None rather than 0 for an unlimited plan: a percentage of no limit
        # is not 0%, it is not a number, and a bar drawn at 0 would tell
        # somebody their storage is empty.
        "available_bytes": max(limit - used, 0) if limit else None,
        "used_percent": round(used / limit * 100, 1) if limit else None,
        "unlimited": not limit,
        # What `prune_campaign_data` would remove right now, so the screen can
        # say "this will delete 412 campaigns" rather than asking somebody to
        # confirm an unknown quantity.
        "cleanable": cleanable_summary(tenant_id),
    }


def _audit_count(tenant_id) -> int:
    from apps.audit.models import AuditEvent

    return AuditEvent.objects.filter(tenant_id=tenant_id).count()


def _storage_limit(tenant_id):
    """
    The storage ceiling in bytes, or None when there is none.

    A **deployment** setting (``TENANT_STORAGE_LIMIT_MB``), not a plan
    entitlement, and that is a deliberate limit on this feature rather than an
    oversight. Storage is not something any plan in this product sells: the
    plan limits are messages, contacts, devices and users
    (``subscriptions.Plan.limits``), and inventing a fifth here would mean a
    ceiling that appears on a usage bar, is enforced nowhere, and cannot be
    changed by the reseller who owns the customer. When storage becomes
    something a plan sells, this reads `limits_for` like every other quota.

    0 (the default) means unlimited, which is the convention every limit in
    this codebase uses, and makes the usage bar say "no limit" rather than
    drawing a full bar against a ceiling of zero.
    """
    from django.conf import settings

    megabytes = int(getattr(settings, "TENANT_STORAGE_LIMIT_MB", 0) or 0)
    if megabytes <= 0:
        return None
    return megabytes * 1024 * 1024


def _finished_before(tenant_id, horizon):
    from apps.campaigns.models import Campaign

    return Campaign.objects.filter(
        tenant_id=tenant_id,
        status__in=FINISHED_STATUSES,
        # `completed_at` and not `created_at`: a campaign that finished
        # yesterday is recent history whenever it was drafted, and a horizon
        # measured from creation would delete a long-scheduled campaign's
        # report the week it went out.
        completed_at__lt=horizon,
    )


def cleanable_summary(tenant_id, days=None) -> dict:
    """
    How much a cleanup would remove, without removing it.

    `days` defaults to the workspace's own setting; passing one answers "what
    if I set it to 30?" for the screen's dropdown, which is the question
    somebody actually has before choosing a horizon.
    """
    from .models import TenantSettings

    if days is None:
        days = TenantSettings.for_tenant(tenant_id).campaign_data_retention_days
    days = int(days or 0)
    if days <= 0:
        return {"enabled": False, "days": 0, "campaigns": 0, "messages": 0}

    from apps.messaging.models import Message

    horizon = timezone.now() - timezone.timedelta(days=days)
    campaigns = _finished_before(tenant_id, horizon)
    campaign_count = campaigns.count()
    message_count = (
        Message.objects.filter(campaign__in=campaigns).count() if campaign_count else 0
    )
    return {
        "enabled": True,
        "days": days,
        "campaigns": campaign_count,
        "messages": message_count,
    }


def prune_campaign_data(tenant_id, days=None) -> dict:
    """
    Delete finished campaigns older than the horizon, and nothing else.

    The message rows go with them by cascade, which is the correct
    relationship: a campaign's messages are its report, and keeping orphaned
    per-recipient rows after deleting the campaign they belong to would leave
    data nobody can interpret.

    Inbound messages are untouched even when they arrived in reply to one of
    these campaigns: an inbound message belongs to the conversation, not to
    the campaign, and `Message.campaign` is null on it.
    """
    from apps.campaigns.models import Campaign

    from .models import TenantSettings

    if days is None:
        days = TenantSettings.for_tenant(tenant_id).campaign_data_retention_days
    days = int(days or 0)
    if days <= 0:
        return {"campaigns": 0, "days": 0}

    horizon = timezone.now() - timezone.timedelta(days=days)
    removed = 0

    while removed < MAX_CAMPAIGNS_PER_PASS:
        batch = list(
            _finished_before(tenant_id, horizon).order_by("pk").values_list("pk", flat=True)[:DELETE_BATCH_SIZE]
        )
        if not batch:
            break
        Campaign.objects.filter(pk__in=batch).delete()
        removed += len(batch)
        if len(batch) < DELETE_BATCH_SIZE:
            break

    if removed:
        logger.info(
            "storage: removed %s finished campaign(s) older than %s days for tenant %s",
            removed, days, tenant_id,
            extra={"event": "storage.pruned", "tenant": tenant_id, "campaigns": removed, "days": days},
        )
    return {"campaigns": removed, "days": days}


def prune_all() -> dict:
    """
    Run every workspace's own campaign-data horizon.

    Only workspaces that have set one are visited: the default is 0, which
    means keep everything, so a deployment where nobody has opted in does no
    work at all.
    """
    from .models import TenantSettings

    results = {}
    rows = TenantSettings.objects.filter(campaign_data_retention_days__gt=0).values_list(
        "tenant_id", "campaign_data_retention_days",
    )
    for tenant_id, days in rows:
        try:
            results[tenant_id] = prune_campaign_data(tenant_id, days)["campaigns"]
        except Exception:  # noqa: BLE001 - one workspace must not take the pass down
            logger.exception(
                "storage cleanup failed for tenant %s", tenant_id,
                extra={"event": "storage.failed", "tenant": tenant_id},
            )
            results[tenant_id] = None
    return results
