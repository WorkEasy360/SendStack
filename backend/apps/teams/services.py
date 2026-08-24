"""
Routing work to people, and noticing when it is late.

Two things live here that the inbox deliberately does not do for itself:
deciding *who* should own a conversation, and keeping the clock that says
whether it has been answered in time. Both are team policy, and both need to
survive an agent going offline halfway through their shift.
"""

import logging
import re
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import AssignmentPolicy, CannedReply, TeamMember, WorkingHours

logger = logging.getLogger("multisender")

# @username, stopping at anything a username cannot contain. Django usernames
# allow letters, digits and @/./+/-/_ ; the dot and dash are excluded from the
# tail here so "@priya." at the end of a sentence still resolves to "priya".
_MENTION_RE = re.compile(r"@([\w+]+(?:[.\-][\w+]+)*)")


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def ensure_member(user) -> TeamMember:
    """
    This user's agent profile, created on first sight.

    Created lazily rather than by a signal on User so that a tenant that never
    opens the inbox never accumulates rows, and so that backfilling existing
    users is not a migration anybody has to remember to run.
    """
    member, _ = TeamMember.objects.get_or_create(
        user=user, defaults={"tenant_id": user.tenant_id, "role": TeamMember.Role.AGENT},
    )
    return member


def assignable_members(tenant_id, policy=None):
    """
    Members the queue may hand a conversation to, with their current load.

    Load is counted here rather than read from a column so it cannot drift:
    a conversation closed directly in the admin, or reassigned by hand, is
    reflected on the very next assignment.
    """
    policy = policy or AssignmentPolicy.for_tenant(tenant_id)
    queryset = TeamMember.objects.filter(tenant_id=tenant_id, auto_assign=True, user__is_active=True)
    if policy.respect_availability:
        queryset = queryset.filter(is_available=True)

    from apps.inbox.models import Conversation

    return queryset.annotate(
        open_conversations=Count(
            "user__assigned_conversations",
            filter=Q(user__assigned_conversations__status=Conversation.Status.OPEN),
            distinct=True,
        )
    )


def _has_capacity(member) -> bool:
    return not member.max_open_conversations or member.open_conversations < member.max_open_conversations


def pick_member(tenant_id, policy=None):
    """The next agent to receive work, or None when nobody can take it."""
    policy = policy or AssignmentPolicy.for_tenant(tenant_id)
    if policy.mode == AssignmentPolicy.Mode.OFF:
        return None

    candidates = [member for member in assignable_members(tenant_id, policy) if _has_capacity(member)]
    if not candidates:
        return None

    if policy.mode == AssignmentPolicy.Mode.LEAST_BUSY:
        # Ties broken by id so the choice is deterministic; without it two
        # equally idle agents would be picked in whatever order the database
        # felt like returning, and neither would ever be reliably first.
        return min(candidates, key=lambda member: (member.open_conversations, member.pk))

    # Round robin: continue after whoever went last, wrapping around. Members
    # who have since gone offline simply are not in the list any more.
    candidates.sort(key=lambda member: member.pk)
    last_id = policy.last_assigned_member_id
    for member in candidates:
        if last_id is None or member.pk > last_id:
            return member
    return candidates[0]


def auto_assign(conversation) -> bool:
    """
    Give this conversation an owner if policy says it should have one.

    Returns whether an assignment was made. Never takes a conversation away
    from an agent who already has it -- reassigning mid-thread is how a
    customer ends up explaining their problem twice.
    """
    if conversation.assigned_agent_id:
        return False

    policy = AssignmentPolicy.for_tenant(conversation.tenant_id)
    if policy.mode == AssignmentPolicy.Mode.OFF:
        return False

    member = pick_member(conversation.tenant_id, policy)
    if member is None:
        logger.info("No agent available to take conversation %s.", conversation.pk)
        return False

    conversation.assigned_agent_id = member.user_id
    conversation.save(update_fields=["assigned_agent", "updated_at"])

    if policy.pk:
        AssignmentPolicy.objects.filter(pk=policy.pk).update(last_assigned_member=member, updated_at=timezone.now())
    else:
        # First assignment for a tenant that has never saved a policy: the row
        # has to exist now, or round robin restarts from the top every time.
        policy.last_assigned_member = member
        policy.save()

    logger.info("Conversation %s auto-assigned to user %s (%s).", conversation.pk, member.user_id, policy.mode)
    return True


# ---------------------------------------------------------------------------
# Response clock
# ---------------------------------------------------------------------------

def on_inbound(conversation) -> None:
    """Start the response clock, if it is not already running."""
    if conversation.awaiting_reply_since is None:
        conversation.awaiting_reply_since = timezone.now()
        conversation.save(update_fields=["awaiting_reply_since", "updated_at"])


def on_human_reply(conversation) -> None:
    """
    Stop the clock, and record the first response time if this is the first.

    Only called for a reply an agent actually typed. An auto-reply keeps the
    customer informed but has not answered them, and letting it stop the clock
    would make every SLA report read as a perfect score.
    """
    fields = []
    if conversation.awaiting_reply_since is not None:
        if conversation.first_response_seconds is None:
            elapsed = (timezone.now() - conversation.awaiting_reply_since).total_seconds()
            conversation.first_response_seconds = max(0, int(elapsed))
            fields.append("first_response_seconds")
        conversation.awaiting_reply_since = None
        fields.append("awaiting_reply_since")

    if fields:
        conversation.save(update_fields=fields + ["updated_at"])


def response_due_at(conversation, policy=None):
    """When a reply to this conversation becomes late, or None if nothing is owed."""
    if conversation.awaiting_reply_since is None:
        return None
    policy = policy or AssignmentPolicy.for_tenant(conversation.tenant_id)
    if not policy.first_response_target_minutes:
        return None
    return conversation.awaiting_reply_since + timedelta(minutes=policy.first_response_target_minutes)


# ---------------------------------------------------------------------------
# Working hours
# ---------------------------------------------------------------------------

def is_open_now(tenant_id, now=None) -> bool:
    """
    Whether the team is within its working hours.

    Answered in the tenant's own timezone -- the same one quiet hours uses, so
    a business cannot be simultaneously open for the away message and asleep
    for the send gate. With no hours configured the team counts as always
    open: a tenant who has not set this has not asked to be closed.
    """
    policy = AssignmentPolicy.for_tenant(tenant_id)
    if not policy.working_hours_enabled:
        return True

    rows = {row.weekday: row for row in WorkingHours.objects.filter(tenant_id=tenant_id)}
    if not rows:
        return True

    from apps.compliance.services import tenant_zone

    local = (now or timezone.now()).astimezone(tenant_zone(tenant_id))
    today = rows.get(local.weekday())
    if today is None or not today.is_open:
        return False

    if today.opens_at <= today.closes_at:
        return today.opens_at <= local.time() < today.closes_at
    # A window that wraps past midnight (a night shift): open from opening
    # time to the end of the day, and again from the start of the day to
    # closing time.
    return local.time() >= today.opens_at or local.time() < today.closes_at


def maybe_send_away_message(conversation, inbound_message) -> bool:
    """
    Tell the contact nobody is here, at most once per period of silence.

    "Once" is anchored to `awaiting_reply_since`: as long as the team has not
    replied, the contact has already been told, and repeating it every time
    they follow up would be worse than saying nothing. It is also skipped for
    an opt-out request, which is answered by compliance and by nothing else.
    """
    policy = AssignmentPolicy.for_tenant(conversation.tenant_id)
    if not policy.send_away_message or not policy.away_message.strip():
        return False
    if is_open_now(conversation.tenant_id):
        return False

    from apps.compliance.keywords import is_start, is_stop

    body = inbound_message.body or ""
    if is_stop(body) or is_start(body):
        return False

    from apps.messaging.models import Message

    already_told = Message.objects.filter(
        conversation=conversation, direction=Message.Direction.OUTBOUND, is_automated=True,
        queued_at__gte=conversation.awaiting_reply_since or conversation.created_at,
        body=policy.away_message,
    ).exists()
    if already_told:
        return False

    from apps.automation.services import send_automated_message

    sent = send_automated_message(
        conversation.whatsapp_account, conversation.contact,
        {"message_type": Message.MessageType.TEXT, "body": policy.away_message},
        conversation=conversation, reference=f"teams:away:{conversation.pk}", kind=Message.Kind.SERVICE,
    )
    return sent is not None


# ---------------------------------------------------------------------------
# Canned replies and mentions
# ---------------------------------------------------------------------------

def expand_shortcut(tenant_id, shortcut: str):
    reply = CannedReply.objects.filter(tenant_id=tenant_id, shortcut=shortcut.lstrip("/")).first()
    if reply is None:
        return None
    CannedReply.objects.filter(pk=reply.pk).update(usage_count=reply.usage_count + 1)
    return reply


def mentioned_users(tenant_id, text: str):
    """Users named with @username in an internal note, within this tenant only."""
    usernames = {match.group(1) for match in _MENTION_RE.finditer(text or "")}
    if not usernames:
        return []

    from django.contrib.auth import get_user_model

    return list(get_user_model().objects.filter(tenant_id=tenant_id, username__in=usernames, is_active=True))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def agent_performance(tenant_id, start=None, end=None) -> list:
    """
    Per-agent totals for the period, from the conversations themselves.

    Nothing here is a stored counter: a counter that disagrees with the
    conversation list is worse than no counter, and these queries are cheap
    enough that there is no reason to keep one.
    """
    from apps.inbox.models import Conversation
    from apps.messaging.models import Message

    conversations = Conversation.objects.filter(tenant_id=tenant_id, assigned_agent__isnull=False)
    if start:
        conversations = conversations.filter(created_at__gte=start)
    if end:
        conversations = conversations.filter(created_at__lte=end)

    replies = Message.objects.filter(
        tenant_id=tenant_id, direction=Message.Direction.OUTBOUND, is_automated=False, billed_to__isnull=False,
    )
    if start:
        replies = replies.filter(queued_at__gte=start)
    if end:
        replies = replies.filter(queued_at__lte=end)
    replies_by_user = dict(
        replies.values_list("billed_to_id").annotate(total=Count("id")).values_list("billed_to_id", "total")
    )

    rows = {}
    for member in TeamMember.objects.filter(tenant_id=tenant_id).select_related("user"):
        rows[member.user_id] = {
            "user_id": member.user_id,
            "username": member.user.username,
            "role": member.role,
            "is_available": member.is_available,
            "assigned": 0,
            "open": 0,
            "closed": 0,
            "replies_sent": replies_by_user.get(member.user_id, 0),
            "_response_times": [],
        }

    for conversation in conversations.only("assigned_agent_id", "status", "first_response_seconds"):
        row = rows.get(conversation.assigned_agent_id)
        if row is None:
            continue
        row["assigned"] += 1
        row["open" if conversation.status == Conversation.Status.OPEN else "closed"] += 1
        if conversation.first_response_seconds is not None:
            row["_response_times"].append(conversation.first_response_seconds)

    result = []
    for row in rows.values():
        times = row.pop("_response_times")
        row["avg_first_response_seconds"] = round(sum(times) / len(times)) if times else None
        result.append(row)
    return sorted(result, key=lambda row: (-row["assigned"], row["username"]))
