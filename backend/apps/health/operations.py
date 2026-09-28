"""
What an operator needs to see at 3am, gathered in one place.

Before this the answer to "is the platform healthy right now?" was assembled by
hand from four places -- ``docker logs``, ``celery inspect``, a psql session and
the backup endpoint -- and three of those need shell access to the host. The
questions an incident actually starts with are:

* is work piling up, and in which queue;
* is anything failing over and over;
* did the scheduler stop, so nobody's scheduled campaign is going out;
* are the sending numbers still connected;
* when did a backup last succeed.

Each is a cheap query. None of them was reachable without a terminal.

    NOTHING HERE IS A CUSTOMER'S BUSINESS, AND NOTHING HERE IS A SECRET.

Two rules shape every number below. It reports *volumes and states*, never
content: how many messages failed, never what they said; which tenants are the
busiest, by name and id, never their contact lists. And it reports outcomes,
never infrastructure: queue depths and worker counts, never a broker URL, a
hostname, a credential or a file path. A dashboard is exactly where a
connection string gets added "just to check something" and then lives in a
screenshot in a ticket for ever.

Every section catches its own exceptions and degrades to a stated error. An
operations view is read *during* an outage; one that 500s because Redis is down
is useless precisely when it is needed, and "redis: unreachable" is itself the
most useful thing on the screen that night.
"""

import logging

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

logger = logging.getLogger("sendstack")

#: How far back the "recent" counters look. One hour is the window an incident
#: is actually about -- a day's totals hide a failure that started ten minutes
#: ago behind twenty-three healthy hours.
RECENT_WINDOW_MINUTES = 60


def _since(minutes=RECENT_WINDOW_MINUTES):
    return timezone.now() - timezone.timedelta(minutes=minutes)


def queue_depths() -> dict:
    """
    How many tasks are waiting in each queue, straight from the broker.

    Celery's queues are Redis lists named after the queue, so ``LLEN`` is the
    backlog. This is deliberately not ``celery inspect`` -- that asks the
    *workers* what they are holding, which returns nothing at all when the
    workers are down, and "the workers are down" is the moment the backlog
    matters most.

    Every queue in ``CELERY_QUEUE_NAMES`` is listed even when empty, because a
    queue missing from this list is indistinguishable from a queue at zero --
    and a routed task whose queue no worker consumes is precisely the failure
    this is here to make visible.
    """
    names = list(getattr(settings, "CELERY_QUEUE_NAMES", ["celery"]))
    try:
        import redis

        client = redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2)
        depths = {name: int(client.llen(name)) for name in names}
        return {"queues": depths, "total": sum(depths.values()), "available": True}
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        logger.warning("queue depth unavailable: %s", exc, extra={"event": "ops.queue_depth_failed"})
        return {"queues": {name: None for name in names}, "total": None, "available": False}


def workers() -> dict:
    """
    How many workers are answering, and which queues they say they consume.

    The queue list is the useful half. A deployment that routes to five queues
    and runs one worker started with the default ``-Q`` is consuming one of
    them, and everything else is accumulating silently -- which looks exactly
    like an idle system until a customer asks where their campaign went.
    """
    try:
        from config.celery import app

        replies = app.control.ping(timeout=1.0) or []
        active_queues = app.control.inspect(timeout=1.0).active_queues() or {}
        consumed = sorted({
            queue.get("name", "")
            for queues in active_queues.values()
            for queue in (queues or [])
            if queue.get("name")
        })
        configured = set(getattr(settings, "CELERY_QUEUE_NAMES", ["celery"]))
        return {
            "count": len(replies),
            "reachable": bool(replies),
            "queues_consumed": consumed,
            # The actionable line: tasks routed here have nowhere to go.
            "queues_unconsumed": sorted(configured - set(consumed)) if consumed else sorted(configured),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker inspection unavailable: %s", exc, extra={"event": "ops.worker_inspect_failed"})
        return {"count": 0, "reachable": False, "queues_consumed": [], "queues_unconsumed": []}


def dead_letters() -> dict:
    """Background jobs that gave up, and which task it keeps happening to."""
    from apps.health.models import DeadLetter

    open_rows = DeadLetter.objects.filter(resolved_at__isnull=True)
    by_task = (
        open_rows.values("task_name")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )
    return {
        "open": open_rows.count(),
        "last_hour": open_rows.filter(failed_at__gte=_since()).count(),
        "by_task": [{"task": row["task_name"], "count": row["count"]} for row in by_task],
    }


def scheduler() -> dict:
    """
    Whether the durable scheduler is keeping up.

    ``overdue`` is the number that matters and the one worth understanding:
    a job is due when its time arrives and is picked up on the next tick, so a
    handful of jobs a few seconds overdue is the system working. Jobs overdue
    by *minutes* mean the beat process has stopped, and every scheduled
    campaign on the platform is now silently not going out.
    """
    from apps.scheduling.models import ScheduledJob

    now = timezone.now()
    grace = int(getattr(settings, "SCHEDULER_POLL_INTERVAL_SECONDS", 60)) * 3

    pending = ScheduledJob.objects.filter(status=ScheduledJob.Status.PENDING)
    overdue = pending.filter(scheduled_for__lt=now - timezone.timedelta(seconds=grace))
    oldest = overdue.order_by("scheduled_for").values_list("scheduled_for", flat=True).first()

    return {
        "pending": pending.count(),
        "processing": ScheduledJob.objects.filter(status=ScheduledJob.Status.PROCESSING).count(),
        "overdue": overdue.count(),
        "overdue_by_seconds": round((now - oldest).total_seconds()) if oldest else 0,
        "failed_last_day": ScheduledJob.objects.filter(
            status=ScheduledJob.Status.FAILED, updated_at__gte=_since(60 * 24)
        ).count(),
        # So a reader can judge "overdue" against the precision this scheduler
        # actually offers rather than against zero.
        "poll_interval_seconds": int(getattr(settings, "SCHEDULER_POLL_INTERVAL_SECONDS", 60)),
    }


def messaging() -> dict:
    """
    The send pipeline's own state: what is queued, what failed, and why.

    The failure breakdown is by ``Message.FailureReason``, which is the whole
    point of that column existing. "Four hundred failures in the last hour" is
    an alarm; "four hundred, all device_disconnected" is an instruction.
    """
    from apps.messaging.models import Message

    recent = Message.objects.filter(updated_at__gte=_since())
    reasons = (
        recent.filter(status__in=[Message.Status.FAILED, Message.Status.SKIPPED])
        .exclude(failure_reason="")
        .values("failure_reason")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    stats = recent.aggregate(
        sent=Count("id", filter=Q(status__in=[
            Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ,
        ])),
        failed=Count("id", filter=Q(status=Message.Status.FAILED)),
        skipped=Count("id", filter=Q(status=Message.Status.SKIPPED)),
    )
    attempted = stats["sent"] + stats["failed"]
    return {
        "window_minutes": RECENT_WINDOW_MINUTES,
        "queued_now": Message.objects.filter(status=Message.Status.QUEUED).count(),
        "sent_recent": stats["sent"],
        "failed_recent": stats["failed"],
        "skipped_recent": stats["skipped"],
        "failure_rate": round(stats["failed"] / attempted * 100, 1) if attempted else 0.0,
        "failure_reasons": [
            {"reason": row["failure_reason"], "count": row["count"]} for row in reasons
        ],
    }


def providers() -> dict:
    """
    The sending numbers, by state. A count per state, never a phone number.

    ``paused`` is separate from ``disconnected`` because they are different
    incidents: disconnected is a customer who needs to rescan a QR code, paused
    is this application protecting a number that started failing -- and a jump
    in the second one is a platform-level signal, not a customer one.
    """
    from apps.whatsapp.models import WhatsAppAccount

    rows = WhatsAppAccount.objects.values("status").annotate(count=Count("id"))
    by_status = {row["status"]: row["count"] for row in rows}

    paused = 0
    try:
        from apps.whatsapp.models import SendingPolicy

        # A pause with no `paused_until` is indefinite and is the *more*
        # serious of the two -- it means a number needs a person to look at it.
        # Filtering on `paused_until` alone would have missed exactly those.
        paused = SendingPolicy.objects.filter(
            Q(is_paused=True),
            Q(paused_until__isnull=True) | Q(paused_until__gt=timezone.now()),
        ).count()
    except Exception:  # noqa: BLE001 - a missing panel, not a broken page
        paused = None

    return {
        "by_status": by_status,
        "connected": by_status.get(WhatsAppAccount.Status.CONNECTED, 0),
        "total": sum(by_status.values()),
        "paused": paused,
    }


def tenants() -> dict:
    """Platform shape: how many workspaces there are and how they are doing commercially."""
    from apps.subscriptions.models import Subscription
    from apps.tenants.models import Tenant

    tenant_rows = Tenant.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        suspended=Count("id", filter=Q(is_active=False)),
    )
    subscription_rows = (
        Subscription.objects.values("status").annotate(count=Count("id"))
    )
    return {
        **tenant_rows,
        "subscriptions": {row["status"]: row["count"] for row in subscription_rows},
    }


def campaigns() -> dict:
    """How much sending is actually in flight across the platform."""
    from apps.campaigns.models import Campaign

    rows = Campaign.objects.filter(
        status__in=[Campaign.Status.QUEUED, Campaign.Status.RUNNING, Campaign.Status.SCHEDULED]
    ).values("status").annotate(count=Count("id"))
    return {row["status"]: row["count"] for row in rows}


def backups() -> dict:
    """The two numbers from Phase 4's backup records that belong on this screen."""
    from apps.health.models import BackupRun

    last = BackupRun.last_success()
    restore = BackupRun.last_success(kind=BackupRun.Kind.RESTORE_TEST)
    return {
        "last_success_at": last.finished_at if last else None,
        "age_hours": round(last.age_seconds / 3600, 1) if last else None,
        "offsite": last.offsite if last else False,
        "last_restore_verified_at": restore.finished_at if restore else None,
        "restore_age_days": round(restore.age_seconds / 86400, 1) if restore else None,
    }


#: Every panel, so the view and the tests agree on what a snapshot contains.
SECTIONS = (
    ("queues", queue_depths),
    ("workers", workers),
    ("dead_letters", dead_letters),
    ("scheduler", scheduler),
    ("messaging", messaging),
    ("providers", providers),
    ("tenants", tenants),
    ("campaigns", campaigns),
    ("backups", backups),
)


def snapshot() -> dict:
    """
    Every panel, with one broken panel reported rather than fatal.

    See the module docstring: this is read during an incident, so a section
    that cannot be computed must render as "unavailable" beside the sections
    that can, not take the page down with it.
    """
    result = {"generated_at": timezone.now()}
    for name, section in SECTIONS:
        try:
            result[name] = section()
        except Exception:  # noqa: BLE001
            logger.exception("operations panel %s failed", name, extra={"event": "ops.panel_failed"})
            result[name] = {"error": "unavailable"}
    return result
