"""
CRM operations that more than one caller needs, and the two assembled reads
(the contact timeline, the Today digest) that are the point of the whole app.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.contacts.models import Contact

from . import activity, lifecycle, segments
from .models import Activity, ContactNote, Deal, Pipeline, PipelineStage, Task

logger = logging.getLogger("sendstack")

#: The stages a pipeline starts with: (name, kind, probability, colour).
#: Three working steps and two endings, because a board a seller can read at a
#: glance beats the full B2B ladder they would have to scroll -- but only a
#: starting point. Stages are editable per pipeline, and nothing in the code
#: knows these names (see PipelineStage.kind), so a tenant who wants "Proposal"
#: back adds one from the Stages dialog and every report keeps working.
#:
#: Both endings are here on purpose: a pipeline with nowhere to put a sale that
#: fell through leaves dead deals sitting in an open stage, inflating the
#: forecast, which is the one number the board exists to get right.
DEFAULT_STAGES = (
    ("New", PipelineStage.Kind.OPEN, 10, "#64748b"),
    ("Contacted", PipelineStage.Kind.OPEN, 30, "#3b82f6"),
    ("Qualified", PipelineStage.Kind.OPEN, 60, "#8b5cf6"),
    ("Won", PipelineStage.Kind.WON, 100, "#16a34a"),
    ("Lost", PipelineStage.Kind.LOST, 0, "#dc2626"),
)


def default_pipeline(tenant) -> Pipeline:
    """
    This tenant's default pipeline, created with the standard stages if they
    have none yet.

    Built on first use rather than at signup so a tenant who never opens a
    deal never acquires a pipeline they did not ask for, and so this works for
    every tenant that already existed before the CRM did.
    """
    existing = Pipeline.objects.filter(tenant=tenant).order_by("-is_default", "pk").first()
    if existing:
        return existing

    with transaction.atomic():
        pipeline = Pipeline.objects.create(tenant=tenant, name="Sales", is_default=True)
        PipelineStage.objects.bulk_create([
            PipelineStage(pipeline=pipeline, name=name, kind=kind, order=index, probability=probability, color=color)
            for index, (name, kind, probability, color) in enumerate(DEFAULT_STAGES)
        ])
    return pipeline


# ---------------------------------------------------------------------------
# Writes -- every one records a timeline entry, so the profile is complete
# whichever door the change came through.
# ---------------------------------------------------------------------------

def record_deal_created(deal: Deal, *, actor=None) -> None:
    """
    The side effects every new deal has, wherever it was created.

    Split out from `create_deal` so the DRF viewset -- which builds the row
    through a serializer, as every other endpoint does -- produces exactly the
    same timeline entry, lifecycle promotion and rescore as a deal opened from
    the contact screen. Two code paths writing deals is fine; two code paths
    disagreeing about what a deal *means* is how a CRM starts lying.
    """
    activity.record(
        deal.contact, Activity.Kind.DEAL_CREATED, f'Deal opened: "{deal.name}"',
        actor=actor, metadata={"deal_id": deal.pk, "value": str(deal.value), "currency": deal.currency},
    )
    lifecycle.on_deal_opened(deal.contact)
    rescore(deal.contact)


def record_deal_moved(deal: Deal, previous_stage, *, actor=None) -> None:
    kind = {
        PipelineStage.Kind.WON: Activity.Kind.DEAL_WON,
        PipelineStage.Kind.LOST: Activity.Kind.DEAL_LOST,
    }.get(deal.stage.kind, Activity.Kind.DEAL_STAGE_CHANGED)
    previous_name = previous_stage.name if previous_stage else "?"
    activity.record(
        deal.contact, kind, f'"{deal.name}" moved from {previous_name} to {deal.stage.name}',
        actor=actor,
        metadata={"deal_id": deal.pk, "from": previous_name, "to": deal.stage.name, "value": str(deal.value)},
    )
    if deal.stage.kind == PipelineStage.Kind.WON:
        lifecycle.on_deal_won(deal.contact)
    rescore(deal.contact)


def create_deal(*, tenant, contact, name, actor=None, pipeline=None, stage=None, **fields) -> Deal:
    pipeline = pipeline or default_pipeline(tenant)
    stage = stage or pipeline.stages.filter(kind=PipelineStage.Kind.OPEN).order_by("order", "pk").first()
    if stage is None:
        raise ValueError("This pipeline has no open stage to put a deal in.")

    deal = Deal.objects.create(
        tenant=tenant, contact=contact, pipeline=pipeline, stage=stage,
        name=name, created_by=actor if getattr(actor, "pk", None) else None,
        owner=fields.pop("owner", None) or contact.owner,
        **fields,
    )
    record_deal_created(deal, actor=actor)
    return deal


def move_deal(deal: Deal, stage: PipelineStage, *, actor=None) -> Deal:
    """Move a deal to another stage, recording what that meant."""
    if stage.pipeline_id != deal.pipeline_id:
        raise ValueError("That stage belongs to a different pipeline.")
    if stage.pk == deal.stage_id:
        return deal

    previous = deal.stage
    deal.stage = stage
    # closed_at is recomputed by save(): reopening a closed deal has to clear
    # it, or the deal reports a close date while sitting in an open stage.
    if previous.kind != PipelineStage.Kind.OPEN and stage.kind == PipelineStage.Kind.OPEN:
        deal.closed_at = None
    deal.save()
    record_deal_moved(deal, previous, actor=actor)
    return deal


def create_task(*, tenant, title, due_at, actor=None, contact=None, deal=None, **fields) -> Task:
    task = Task.objects.create(
        tenant=tenant, title=title, due_at=due_at, contact=contact, deal=deal,
        created_by=actor if getattr(actor, "pk", None) else None,
        assigned_to=fields.pop("assigned_to", None) or (actor if getattr(actor, "pk", None) else None),
        **fields,
    )
    if task.contact_id:
        activity.record(
            task.contact, Activity.Kind.TASK_CREATED, f'Task added: "{task.title}"',
            actor=actor, metadata={"task_id": task.pk, "due_at": task.due_at.isoformat()},
        )
    return task


def complete_task(task: Task, *, actor=None) -> Task:
    if task.status == Task.Status.DONE:
        return task
    task.status = Task.Status.DONE
    task.completed_at = timezone.now()
    task.save(update_fields=["status", "completed_at", "updated_at"])
    if task.contact_id:
        activity.record(
            task.contact, Activity.Kind.TASK_COMPLETED, f'Task done: "{task.title}"',
            actor=actor, metadata={"task_id": task.pk},
        )
    return task


def add_note(*, contact, body, actor=None) -> ContactNote:
    note = ContactNote.objects.create(
        tenant_id=contact.tenant_id, contact=contact, body=body,
        author=actor if getattr(actor, "pk", None) else None,
    )
    activity.record(
        contact, Activity.Kind.NOTE_ADDED, body.strip().splitlines()[0][:200] if body.strip() else "Note added",
        actor=actor, metadata={"note_id": note.pk},
    )
    return note


def assign_owner(contact, owner, *, actor=None) -> bool:
    if contact.owner_id == (owner.pk if owner else None):
        return False
    previous = contact.owner
    contact.owner = owner
    contact.save(update_fields=["owner", "updated_at"])
    activity.record(
        contact, Activity.Kind.OWNER_CHANGED,
        f"Owner changed from {previous or 'nobody'} to {owner or 'nobody'}",
        actor=actor,
        metadata={"from": previous.pk if previous else None, "to": owner.pk if owner else None},
    )
    return True


def rescore(contact) -> None:
    from .scoring import score_contact

    try:
        score_contact(contact)
    except Exception:  # noqa: BLE001 - a stale score must not fail the write that caused it
        logger.exception("Could not rescore contact %s", contact.pk)


# ---------------------------------------------------------------------------
# Assembled reads
# ---------------------------------------------------------------------------

def contact_timeline(contact, *, limit=40, before=None) -> list:
    """
    One list of everything that ever happened with this person, newest first.

    Merged at read time from two sources rather than stored as one table --
    see the note on `models.Activity` for why messages are not copied. Both
    sides are cut to `limit` first so this is two indexed range scans of at
    most `limit` rows each, whatever the size of either table.
    """
    from apps.messaging.models import Message

    activities = Activity.objects.filter(contact=contact).select_related("actor")
    messages = (
        Message.objects.filter(contact=contact)
        .select_related("campaign", "template")
        .only(
            "id", "direction", "body", "caption", "status", "queued_at", "sent_at", "message_type",
            "is_automated", "campaign__id", "campaign__name", "template__id", "template__name",
        )
    )
    if before:
        activities = activities.filter(occurred_at__lt=before)
        messages = messages.filter(queued_at__lt=before)

    entries = [
        {
            "id": f"activity-{item.pk}",
            "source": "activity",
            "kind": item.kind,
            "summary": item.summary,
            "metadata": item.metadata,
            "actor": item.actor.username if item.actor else None,
            "at": item.occurred_at,
        }
        for item in activities.order_by("-occurred_at", "-id")[:limit]
    ]

    for message in messages.order_by("-queued_at", "-id")[:limit]:
        inbound = message.direction == Message.Direction.INBOUND
        body = (message.body or message.caption or "").strip()
        entries.append({
            "id": f"message-{message.pk}",
            "source": "message",
            "kind": "MESSAGE_RECEIVED" if inbound else "MESSAGE_SENT",
            "summary": body[:280] or f"{message.get_message_type_display()} message",
            "metadata": {
                "status": message.status,
                "message_type": message.message_type,
                "automated": message.is_automated,
                "campaign": message.campaign.name if message.campaign_id else None,
                "campaign_id": message.campaign_id,
                "template": message.template.name if message.template_id else None,
            },
            "actor": None,
            "at": message.queued_at,
        })

    entries.sort(key=lambda entry: entry["at"], reverse=True)
    return entries[:limit]


def contact_profile(contact) -> dict:
    """
    Everything the one customer screen needs, in one request.

    Assembled server-side on purpose: the alternative is the profile page
    firing seven requests on open, which is seven round trips and seven
    chances to render half a customer.
    """
    from apps.inbox.models import Conversation

    deals = list(
        Deal.objects.filter(contact=contact).select_related("stage", "pipeline", "owner").order_by("-updated_at")[:20]
    )
    tasks = list(
        Task.objects.filter(contact=contact).select_related("assigned_to").order_by("status", "due_at")[:20]
    )
    notes = list(ContactNote.objects.filter(contact=contact).select_related("author")[:20])
    conversations = list(
        Conversation.objects.filter(contact=contact).select_related("whatsapp_account", "assigned_agent")
        .order_by("-last_message_at")[:10]
    )

    open_deals = [deal for deal in deals if deal.status == Deal.Status.OPEN]
    next_task = next((task for task in tasks if task.status == Task.Status.OPEN), None)

    return {
        "deals": deals,
        "tasks": tasks,
        "notes": notes,
        "conversations": conversations,
        "open_deal_value": sum((deal.value or 0) for deal in open_deals),
        "won_deal_value": sum((deal.value or 0) for deal in deals if deal.status == Deal.Status.WON),
        "next_action_at": next_task.due_at if next_task else None,
        "next_action": next_task.title if next_task else "",
    }


#: How long a contact may sit unanswered before the Today digest calls it out.
NEEDS_REPLY_HOURS = 4


def today_digest(tenant, *, user=None) -> dict:
    """
    "What should I do next?", as a handful of counted, clickable buckets.

    Every bucket is a real query against an indexed column, and each carries
    the filter that produced it so the UI can link straight to the list rather
    than making the user rebuild it.
    """
    from apps.inbox.models import Conversation

    now = timezone.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    tasks = Task.objects.filter(tenant=tenant, status=Task.Status.OPEN)
    if user is not None and getattr(user, "pk", None):
        # A shared queue is noise for an individual agent; theirs plus the
        # unassigned pile is the work they can actually pick up.
        tasks = tasks.filter(Q(assigned_to=user) | Q(assigned_to__isnull=True))

    due_tasks = tasks.filter(due_at__lte=now.replace(hour=23, minute=59, second=59))
    overdue = tasks.filter(due_at__lt=start_of_day).count()

    unanswered = Conversation.objects.filter(
        tenant=tenant, status=Conversation.Status.OPEN,
        awaiting_reply_since__lte=now - timedelta(hours=NEEDS_REPLY_HOURS),
    ).count()

    new_replies = Conversation.objects.filter(
        tenant=tenant, unread_count__gt=0, last_message_at__gte=start_of_day,
    ).count()

    hot_quiet = segments.resolve(tenant, "hot_leads").filter(
        Q(last_outbound_at__isnull=True) | Q(last_outbound_at__lt=now - timedelta(days=2))
    ).count()

    closing = Deal.objects.filter(
        tenant=tenant, status=Deal.Status.OPEN,
        expected_close_date__isnull=False, expected_close_date__lte=(now + timedelta(days=7)).date(),
    )
    closing_stats = closing.aggregate(count=Count("id"), value=Sum("value"))

    no_reply_7d = segments.resolve(tenant, "no_reply_7d").count()

    return {
        "generated_at": now,
        "items": [
            {
                "key": "tasks_due", "label": "Follow-ups due today",
                "count": due_tasks.count(), "urgent": overdue,
                "detail": f"{overdue} overdue" if overdue else "",
                "link": "/tasks",
            },
            {
                "key": "needs_reply", "label": "Chats waiting on a reply",
                "count": unanswered, "urgent": unanswered,
                "detail": f"Unanswered for over {NEEDS_REPLY_HOURS} hours" if unanswered else "",
                "link": "/inbox",
            },
            {
                "key": "new_replies", "label": "Customers who replied today",
                "count": new_replies, "urgent": 0, "detail": "", "link": "/inbox",
            },
            {
                "key": "hot_leads", "label": "Hot leads to call",
                "count": hot_quiet, "urgent": 0,
                "detail": "Hot, and nothing sent in 2 days" if hot_quiet else "",
                "link": "/contacts?smart=hot_leads",
            },
            {
                "key": "deals_closing", "label": "Deals closing this week",
                "count": closing_stats["count"] or 0, "urgent": 0,
                "detail": f"{closing_stats['value']:,.0f} in play" if closing_stats["value"] else "",
                "link": "/pipeline",
            },
            {
                "key": "going_cold", "label": "Leads going cold",
                "count": no_reply_7d, "urgent": 0,
                "detail": "No reply in 7 days" if no_reply_7d else "",
                "link": "/contacts?smart=no_reply_7d",
            },
        ],
        "tasks": list(
            due_tasks.select_related("contact", "assigned_to").order_by("due_at")[:8]
        ),
    }


def pipeline_board(pipeline) -> list:
    """One column per stage, with its deals and totals."""
    stages = list(pipeline.stages.all())
    deals = (
        Deal.objects.filter(pipeline=pipeline)
        .select_related("contact", "owner", "stage")
        .order_by("-updated_at")
    )
    # Closed deals accumulate forever; a board showing every deal ever lost is
    # unreadable and unbounded. Open stages show everything, terminal stages
    # show the most recent handful.
    by_stage = {stage.pk: [] for stage in stages}
    for deal in deals[:2000]:
        by_stage.setdefault(deal.stage_id, []).append(deal)

    columns = []
    for stage in stages:
        items = by_stage.get(stage.pk, [])
        limit = 100 if stage.kind == PipelineStage.Kind.OPEN else 25
        columns.append({
            "stage": stage,
            "deals": items[:limit],
            "count": len(items),
            "value": sum((deal.value or 0) for deal in items),
        })
    return columns


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def merge_contacts(primary: Contact, duplicate: Contact, *, actor=None) -> Contact:
    """
    Fold `duplicate` into `primary` and delete it.

    Nothing is discarded silently: every message, conversation, deal, task,
    note and timeline entry is repointed at the survivor, and empty fields on
    the survivor are filled from the duplicate (never overwritten -- the
    record someone has been editing wins over the one being absorbed).
    """
    if primary.pk == duplicate.pk:
        return primary
    if primary.tenant_id != duplicate.tenant_id:
        raise ValueError("Contacts from different tenants cannot be merged.")

    from apps.inbox.models import Conversation
    from apps.leadgen.models import LeadEvent
    from apps.messaging.models import Message

    with transaction.atomic():
        for field in ("name", "email", "company", "country", "source"):
            if not getattr(primary, field) and getattr(duplicate, field):
                setattr(primary, field, getattr(duplicate, field))
        if primary.owner_id is None and duplicate.owner_id:
            primary.owner_id = duplicate.owner_id
        # Custom fields merge key-by-key, primary winning on a clash.
        primary.custom_fields = {**(duplicate.custom_fields or {}), **(primary.custom_fields or {})}
        # An opt-out on either record is binding on the survivor: a merge must
        # not be a way of resubscribing somebody.
        if not duplicate.subscribed:
            primary.subscribed = False
            primary.unsubscribed_at = primary.unsubscribed_at or duplicate.unsubscribed_at
        primary.save()

        primary.tags.add(*duplicate.tags.all())
        primary.groups.add(*duplicate.groups.all())

        Message.objects.filter(contact=duplicate).update(contact=primary)
        Deal.objects.filter(contact=duplicate).update(contact=primary)
        Task.objects.filter(contact=duplicate).update(contact=primary)
        ContactNote.objects.filter(contact=duplicate).update(contact=primary)
        Activity.objects.filter(contact=duplicate).update(contact=primary)
        LeadEvent.objects.filter(contact=duplicate).update(contact=primary)

        # Conversations are unique per (account, contact), so a thread on an
        # account the survivor already has one for cannot simply be repointed
        # -- its messages have already moved, so the empty shell is dropped.
        existing_accounts = set(
            Conversation.objects.filter(contact=primary).values_list("whatsapp_account_id", flat=True)
        )
        for conversation in Conversation.objects.filter(contact=duplicate):
            if conversation.whatsapp_account_id in existing_accounts:
                conversation.delete()
            else:
                conversation.contact = primary
                conversation.save(update_fields=["contact"])

        merged_phone = duplicate.phone
        duplicate.delete()

    activity.record(
        primary, Activity.Kind.CONTACT_CREATED, f"Merged duplicate contact {merged_phone} into this one",
        actor=actor, metadata={"merged_phone": merged_phone},
    )
    refresh_message_stamps(primary)
    rescore(primary)
    return primary


def refresh_message_stamps(contact) -> None:
    """Recompute last_inbound_at/last_outbound_at from the message table."""
    from django.db.models import Max

    from apps.messaging.models import Message

    stamps = Message.objects.filter(contact=contact).aggregate(
        inbound=Max("queued_at", filter=Q(direction=Message.Direction.INBOUND)),
        outbound=Max("sent_at", filter=Q(direction=Message.Direction.OUTBOUND)),
    )
    Contact.objects.filter(pk=contact.pk).update(
        last_inbound_at=stamps["inbound"],
        last_outbound_at=stamps["outbound"],
        last_message_at=max([s for s in stamps.values() if s], default=None),
    )
