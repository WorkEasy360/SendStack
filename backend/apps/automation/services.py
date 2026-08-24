import logging
from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone

from apps.contacts.models import ContactTag
from apps.credits.services import InsufficientCreditsError, reserve_credits
from apps.messaging.models import Message
from apps.templates_app.services import build_render_context, render_text
from apps.whatsapp.models import WhatsAppAccount
from core.buttons import clean_buttons
from core.media import clean_media_items, resolve_media_items, spec_from_object

from .models import AutomationRule, FollowUpRule, Workflow, WorkflowRun

logger = logging.getLogger("multisender")


class WorkflowError(Exception):
    """Raised for a workflow definition that fails validation."""


# ---------------------------------------------------------------------------
# Safety limits
#
# Every one of these exists because the engine will otherwise do something
# unbounded on a graph a user is perfectly able to draw in the builder. They
# are deliberately generous -- they are backstops against runaway, not a
# product limit anybody should meet in normal use.
# ---------------------------------------------------------------------------

#: Most nodes one workflow may contain.
MAX_NODES = 200

#: Nodes one _advance() call may execute before it gives up. Reached only by a
#: graph that loops without a WAIT in the cycle, which validate_nodes now
#: rejects on save -- this catches the rows saved before it did.
MAX_STEPS_PER_ADVANCE = 100

#: Nodes a single run may execute over its whole lifetime, across every
#: resume. A cycle *through* a WAIT is a legitimate drip sequence, so it
#: passes validation; this is what stops it nudging a contact forever.
MAX_TOTAL_STEPS = 500

#: Longest a WAIT node may pause for (30 days). A typo of "60000" minutes
#: otherwise parks a run past the heat death of the campaign.
MAX_WAIT_MINUTES = 60 * 24 * 30

#: Most follow-up rules that may be armed by one outbound message.
MAX_FOLLOW_UP_RULES_PER_MESSAGE = 20

#: A run left RUNNING longer than this had its worker killed mid-advance
#: (deploy, OOM, hard restart). sweep_stuck_runs reclaims it.
STUCK_RUNNING_MINUTES = 15

#: Never answer the same contact from the same rule twice inside this window,
#: whatever the rule's own cooldown says. This is not a policy knob -- it is
#: the guard against a redelivery loop or a retry storm turning one keyword
#: into a burst of identical messages.
MIN_RULE_INTERVAL_SECONDS = 10


def _claim_rule_cooldown(rule, contact) -> bool:
    """
    Take this rule's right to answer `contact` now, or report that it may not.

    Backed by the cache rather than a table: this is throttling, not an audit
    trail, and the cache is configured to fail open (see CACHES OPTIONS), so a
    Redis outage degrades to "answer every time" -- the behaviour before
    cooldowns existed -- instead of silencing every automation on the
    platform.

    Uses add() rather than get()-then-set() so two workers racing on the same
    inbound message cannot both decide they are first.
    """
    if contact is None or not getattr(contact, "pk", None):
        return True

    window = max(int(getattr(rule, "cooldown_seconds", 0) or 0), MIN_RULE_INTERVAL_SECONDS)
    key = f"automation:cooldown:{rule.__class__.__name__}:{rule.pk}:{contact.pk}"
    try:
        claimed = cache.add(key, 1, timeout=window)
    except Exception:  # pragma: no cover - cache is configured to fail open
        logger.warning("Cooldown cache unavailable; letting rule %s answer.", rule.pk)
        return True

    if not claimed:
        logger.info(
            "Rule %s (%s) skipped for contact %s: answered within the last %ss.",
            rule.pk, rule.__class__.__name__, contact.pk, window,
        )
    return bool(claimed)


# ---------------------------------------------------------------------------
# Shared send helper (welcome / keyword reply / follow-up / workflow message
# nodes all funnel through here so they get the same credit gating, template
# rendering, and delivery pipeline as everything else).
# ---------------------------------------------------------------------------

def send_automated_message(account, contact, message_spec: dict, *, conversation=None, reference: str = "",
                           kind=Message.Kind.MARKETING, policy_exempt=False):
    """
    Queue one automated message.

    `kind` decides what the compliance gate may do with it (see
    messaging.Message.Kind): a reply to something the contact just did is
    SERVICE and goes out at once, while an unprompted nudge -- a follow-up, a
    workflow step that woke up hours later -- is MARKETING and waits for the
    tenant's sending hours. `policy_exempt` is narrower still and belongs only
    to the confirmation of an opt-out, which has to reach someone we are
    otherwise no longer allowed to message at all.
    """
    if account.status != WhatsAppAccount.Status.CONNECTED or not account.user_id:
        return None
    if not message_spec.get("body") and not message_spec.get("media_items") and not message_spec.get("media_url"):
        return None

    try:
        reserve_credits(user_id=account.user_id, amount=1, reference=reference)
    except InsufficientCreditsError:
        logger.info("Skipping automated message for account %s: insufficient credits.", account.pk)
        return None

    context = build_render_context(contact)
    variables = message_spec.get("variables", {})
    body = render_text(message_spec.get("body", ""), context, variables)
    caption = render_text(message_spec.get("caption") or "", context, variables)

    # An opt-out confirmation is a compliance notice, not marketing: shortening
    # the links in it would make the one message we owe someone who just asked
    # us to stop look like another tracked campaign.
    links = []
    if not policy_exempt:
        from apps.tracking.services import default_utm, rewrite

        utm = default_utm("automation")
        body, body_links = rewrite(body, tenant_id=account.tenant_id, contact=contact, utm=utm)
        caption, caption_links = rewrite(caption, tenant_id=account.tenant_id, contact=contact, utm=utm)
        links = body_links + caption_links

    message = Message.objects.create(
        tenant=account.tenant, conversation=conversation, whatsapp_account=account, contact=contact,
        to_phone=contact.phone, direction=Message.Direction.OUTBOUND, is_automated=True, billed_to_id=account.user_id,
        template_id=message_spec.get("template_id"),
        message_type=message_spec["message_type"],
        body=body,
        media_url=message_spec.get("media_url") or "",
        media_items=message_spec.get("media_items") or [],
        caption=caption,
        filename=message_spec.get("filename") or "",
        buttons=message_spec.get("buttons") or [],
        kind=Message.Kind.NOTICE if policy_exempt else kind,
        status=Message.Status.QUEUED,
    )

    if links:
        from apps.tracking.services import save_links

        save_links(links, message=message)

    from apps.messaging.tasks import send_message_task

    send_message_task.delay(message.pk)
    return message


# ---------------------------------------------------------------------------
# Welcome message + keyword auto-reply
# ---------------------------------------------------------------------------

def trigger_welcome_message(inbound_message: Message) -> bool:
    """Returns whether a welcome rule answered, so the trigger ladder in
    receivers.py knows this message is already spoken for."""
    account = inbound_message.whatsapp_account
    rule = AutomationRule.objects.filter(
        tenant_id=account.tenant_id, whatsapp_account=account, trigger_type=AutomationRule.Trigger.WELCOME, is_active=True,
    ).select_related("template").first()
    if not rule:
        return False
    if not _claim_rule_cooldown(rule, inbound_message.contact):
        return True  # answered a moment ago; still counts as handled
    send_automated_message(
        account, inbound_message.contact, rule.effective_message(),
        conversation=inbound_message.conversation, reference=f"automation:welcome:{rule.pk}", kind=Message.Kind.SERVICE,
    )
    return True


def trigger_button_rules(inbound_message: Message) -> bool:
    """
    Answer a tapped reply button.

    Runs before keyword rules and, when one matches, instead of them: the
    contact chose a specific option, so a keyword rule that happens to match
    the button's label too must not also fire and send a second message.
    Returns True when a rule handled the tap.
    """
    button_id = inbound_message.replied_button_id
    if not button_id:
        return False

    account = inbound_message.whatsapp_account
    rules = AutomationRule.objects.filter(
        tenant_id=account.tenant_id, whatsapp_account=account, trigger_type=AutomationRule.Trigger.BUTTON,
        button_id=button_id, is_active=True,
    ).select_related("template")

    for rule in rules:
        if rule.matches_button(button_id):
            if _claim_rule_cooldown(rule, inbound_message.contact):
                send_automated_message(
                    account, inbound_message.contact, rule.effective_message(),
                    conversation=inbound_message.conversation, reference=f"automation:button:{rule.pk}",
                    kind=Message.Kind.SERVICE,
                )
            return True  # first (highest-priority) match wins
    return False


def trigger_keyword_rules(inbound_message: Message) -> bool:
    """Returns whether a rule answered, so a fallback (the AI agent) knows to stay quiet."""
    text = (inbound_message.body or "").strip()
    if not text:
        return False

    account = inbound_message.whatsapp_account
    rules = AutomationRule.objects.filter(
        tenant_id=account.tenant_id, whatsapp_account=account, trigger_type=AutomationRule.Trigger.KEYWORD, is_active=True,
    ).select_related("template")

    for rule in rules:
        if rule.matches(text):
            if _claim_rule_cooldown(rule, inbound_message.contact):
                send_automated_message(
                    account, inbound_message.contact, rule.effective_message(),
                    conversation=inbound_message.conversation, reference=f"automation:keyword:{rule.pk}",
                    kind=Message.Kind.SERVICE,
                )
            return True  # first (highest-priority) match wins
    return False


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

def schedule_follow_up_checks(message: Message) -> None:
    """
    Arm every active follow-up rule this message qualifies for.

    A rule with no `trigger_templates` is unscoped and arms for any outbound
    message, template or not -- the original behaviour, unchanged for every
    rule that predates template scoping. A rule that *does* list templates
    only arms when this message was built from one of them: a plain-text
    inbox reply (message.template_id is None) never arms a template-scoped
    rule, and a message built from Template A never arms a rule scoped to
    Template B. This is what stops an account with several campaigns running
    from having every one of them nudge the same contact.
    """
    base = FollowUpRule.objects.filter(
        tenant_id=message.tenant_id, whatsapp_account_id=message.whatsapp_account_id, is_active=True,
    )
    if message.template_id:
        base = base.filter(Q(trigger_templates__isnull=True) | Q(trigger_templates__id=message.template_id))
    else:
        base = base.filter(trigger_templates__isnull=True)

    rules = list(base.distinct().order_by("delay_minutes", "pk")[: MAX_FOLLOW_UP_RULES_PER_MESSAGE + 1])
    if not rules:
        return

    if len(rules) > MAX_FOLLOW_UP_RULES_PER_MESSAGE:
        logger.warning(
            "Account %s has more than %s active follow-up rules; only the %s soonest are armed for message %s.",
            message.whatsapp_account_id, MAX_FOLLOW_UP_RULES_PER_MESSAGE, MAX_FOLLOW_UP_RULES_PER_MESSAGE, message.pk,
        )
        rules = rules[:MAX_FOLLOW_UP_RULES_PER_MESSAGE]

    from .tasks import check_and_send_follow_up

    for rule in rules:
        check_and_send_follow_up.apply_async(args=[message.pk, rule.pk], countdown=rule.delay_minutes * 60)


def run_follow_up_check(message_id: int, rule_id: int) -> None:
    """
    Send `rule`'s nudge if the contact still hasn't answered the message that
    armed it -- and if that message is still the last thing we said.

    The second condition is the one that is easy to miss. Follow-ups are armed
    per outbound message, so a contact who received three messages an hour
    apart has three timers running; without the staleness check the first two
    fire behind the third and the contact gets "just checking in!" twice more
    after a conversation that has already moved on.
    """
    try:
        message = Message.objects.select_related("whatsapp_account", "contact", "conversation").get(pk=message_id)
        rule = FollowUpRule.objects.select_related("template").get(pk=rule_id, is_active=True)
    except (Message.DoesNotExist, FollowUpRule.DoesNotExist):
        return

    if not message.contact_id or not message.sent_at:
        return

    # Someone who unsubscribed while the timer ran must not be nudged. The
    # send gate would block it anyway, but reaching the gate costs a credit
    # reservation and writes a SKIPPED row for a message we already know we
    # must not send.
    if not message.contact.subscribed:
        return

    replied = Message.objects.filter(
        whatsapp_account_id=message.whatsapp_account_id, contact_id=message.contact_id,
        direction=Message.Direction.INBOUND, queued_at__gt=message.sent_at,
    ).exists()
    if replied:
        return

    superseded = Message.objects.filter(
        whatsapp_account_id=message.whatsapp_account_id, contact_id=message.contact_id,
        direction=Message.Direction.OUTBOUND, sent_at__gt=message.sent_at,
    ).exists()
    if superseded:
        logger.info("Follow-up rule %s skipped for message %s: a later message superseded it.", rule.pk, message.pk)
        return

    if not _claim_rule_cooldown(rule, message.contact):
        return

    send_automated_message(
        message.whatsapp_account, message.contact, rule.effective_message(),
        conversation=message.conversation, reference=f"automation:followup:{rule.pk}",
    )


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------

_NODE_TYPES = {"TRIGGER", "MESSAGE", "WAIT", "CONDITION", "TAG", "AI_REPLY", "ASSIGN_AGENT", "END"}

_EDGE_FIELDS = ("next", "on_true", "on_false")

#: Fields a CONDITION node may branch on. Validated on save so a typo is a 400
#: on the workflow rather than a branch that silently always takes on_false.
_CONDITION_FIELDS = {"has_replied", "button_clicked", "has_tag"}


class TriggerEvent:
    """
    What starts a workflow.

    Until now the TRIGGER node was a start marker and nothing more: the only
    way to begin a run was POST /workflows/{id}/run, so a "workflow" was a
    sequence somebody had to launch by hand, one contact at a time. These are
    the events that start one on their own. MANUAL preserves that behaviour
    and stays the default, so every workflow saved before this existed keeps
    behaving exactly as it did.
    """

    MANUAL = "MANUAL"
    NEW_CONVERSATION = "NEW_CONVERSATION"
    INBOUND_MESSAGE = "INBOUND_MESSAGE"
    KEYWORD = "KEYWORD"
    BUTTON = "BUTTON"

    ALL = {MANUAL, NEW_CONVERSATION, INBOUND_MESSAGE, KEYWORD, BUTTON}

    #: Events an inbound message can fire, in the order receivers.py tries them.
    AUTOMATIC = (NEW_CONVERSATION, BUTTON, KEYWORD, INBOUND_MESSAGE)


_MATCH_TYPES = {choice.value for choice in AutomationRule.MatchType}


def trigger_config(nodes: list) -> dict:
    """The TRIGGER node's config with defaults filled in; {} if there isn't one."""
    for node in nodes or []:
        if isinstance(node, dict) and node.get("type") == "TRIGGER":
            config = node.get("trigger") or {}
            if not isinstance(config, dict):
                return {}
            return {
                "event": config.get("event") or TriggerEvent.MANUAL,
                "keyword": (config.get("keyword") or "").strip(),
                "match_type": config.get("match_type") or AutomationRule.MatchType.CONTAINS,
                "button_id": (config.get("button_id") or "").strip(),
                # Whether a contact already part-way through this workflow may
                # be started again. Off by default: re-triggering on every
                # message is how one keyword becomes five overlapping runs all
                # messaging the same person at once.
                "allow_restart": bool(config.get("allow_restart")),
            }
    return {}


def _validate_trigger(node: dict) -> None:
    config = node.get("trigger")
    if config is None:
        return  # no config at all == MANUAL, which is what every old row means
    if not isinstance(config, dict):
        raise WorkflowError(f"TRIGGER node '{node['id']}': 'trigger' must be an object.")

    event = config.get("event") or TriggerEvent.MANUAL
    if event not in TriggerEvent.ALL:
        raise WorkflowError(
            f"TRIGGER node '{node['id']}': unknown event '{event}'. "
            f"Expected one of {', '.join(sorted(TriggerEvent.ALL))}."
        )

    if event == TriggerEvent.KEYWORD:
        if not (config.get("keyword") or "").strip():
            raise WorkflowError(f"TRIGGER node '{node['id']}': a keyword trigger needs a 'keyword'.")
        match_type = config.get("match_type") or AutomationRule.MatchType.CONTAINS
        if match_type not in _MATCH_TYPES:
            raise WorkflowError(
                f"TRIGGER node '{node['id']}': unknown match_type '{match_type}'. "
                f"Expected one of {', '.join(sorted(_MATCH_TYPES))}."
            )

    if event == TriggerEvent.BUTTON and not (config.get("button_id") or "").strip():
        raise WorkflowError(f"TRIGGER node '{node['id']}': a button trigger needs a 'button_id'.")


def _reject_tight_cycles(by_id: dict) -> None:
    """
    Reject a graph that can loop without ever pausing.

    _advance() walks edges until it reaches a WAIT or an END, so a cycle whose
    every node is a MESSAGE/CONDITION/TAG is not a loop the engine escapes --
    it is a worker pinned at 100% CPU sending the same message to the same
    contact until somebody notices the credit balance. (Measured on this
    engine before the check existed: 51 sends and still going.)

    A cycle that passes *through* a WAIT is a different thing entirely -- a
    drip sequence that nudges until the contact answers -- and stays legal.
    So WAIT nodes are treated here as having no outgoing edge, which is
    exactly how execution treats them, and any cycle left in what remains
    cannot terminate and is refused.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node_id: WHITE for node_id in by_id}

    def edges(node):
        if node["type"] == "WAIT":
            return []  # execution stops here, so the walk does too
        return [node.get(field) for field in _EDGE_FIELDS if node.get(field) in by_id]

    for root in by_id:
        if colour[root] != WHITE:
            continue
        # Iterative DFS: a 200-node graph would be fine recursively, but the
        # stack depth of a validator should not depend on user input at all.
        colour[root] = GREY
        path = [root]
        stack = [(root, iter(edges(by_id[root])))]
        while stack:
            node_id, children = stack[-1]
            nxt = next(children, None)
            if nxt is None:
                colour[node_id] = BLACK
                stack.pop()
                path.pop()
                continue
            if colour[nxt] == GREY:
                loop = " -> ".join(path[path.index(nxt):] + [nxt])
                raise WorkflowError(
                    f"These steps loop forever with nothing to pause them: {loop}. "
                    f"Add a Wait step inside the loop, or break it."
                )
            if colour[nxt] == WHITE:
                colour[nxt] = GREY
                path.append(nxt)
                stack.append((nxt, iter(edges(by_id[nxt]))))


def validate_nodes(nodes: list) -> None:
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowError("A workflow needs at least one node.")
    if len(nodes) > MAX_NODES:
        raise WorkflowError(f"A workflow may have at most {MAX_NODES} nodes; this one has {len(nodes)}.")

    by_id = {}
    triggers = 0
    for node in nodes:
        if not isinstance(node, dict) or "id" not in node or "type" not in node:
            raise WorkflowError("Every node needs an 'id' and a 'type'.")
        if node["type"] not in _NODE_TYPES:
            raise WorkflowError(f"Unknown node type '{node['type']}'.")
        if node["id"] in by_id:
            raise WorkflowError(f"Duplicate node id '{node['id']}'.")
        by_id[node["id"]] = node
        if node["type"] == "TRIGGER":
            triggers += 1

    if triggers != 1:
        raise WorkflowError("A workflow needs exactly one TRIGGER node.")

    for node in nodes:
        for edge_field in _EDGE_FIELDS:
            target = node.get(edge_field)
            if target is not None and target not in by_id:
                raise WorkflowError(f"Node '{node['id']}' references unknown node '{target}'.")

        node_type = node["type"]

        if node_type == "TRIGGER":
            _validate_trigger(node)

        elif node_type == "WAIT":
            minutes = node.get("minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
                raise WorkflowError(f"WAIT node '{node['id']}' needs a numeric 'minutes'.")
            if minutes <= 0:
                raise WorkflowError(f"WAIT node '{node['id']}': 'minutes' must be greater than zero.")
            if minutes > MAX_WAIT_MINUTES:
                raise WorkflowError(
                    f"WAIT node '{node['id']}': 'minutes' may be at most {MAX_WAIT_MINUTES} (30 days)."
                )

        elif node_type == "CONDITION":
            condition = node.get("condition")
            field = condition.get("field") if isinstance(condition, dict) else None
            if not field:
                raise WorkflowError(f"CONDITION node '{node['id']}' needs a condition.field.")
            if field not in _CONDITION_FIELDS:
                raise WorkflowError(
                    f"CONDITION node '{node['id']}': unknown condition field '{field}'. "
                    f"Expected one of {', '.join(sorted(_CONDITION_FIELDS))}."
                )
            if field == "has_tag" and not condition.get("value"):
                raise WorkflowError(
                    f"CONDITION node '{node['id']}': a has_tag condition needs condition.value (a tag id)."
                )
            if not node.get("on_true") and not node.get("on_false"):
                # Both branches dangling means the run stops dead here, which
                # is never what the person drawing it meant.
                raise WorkflowError(f"CONDITION node '{node['id']}' needs at least one of on_true / on_false.")

        elif node_type == "MESSAGE":
            has_content = bool(
                node.get("template_id") or (node.get("body") or "").strip()
                or node.get("media_url") or node.get("media_items")
            )
            if not has_content:
                raise WorkflowError(f"MESSAGE node '{node['id']}' needs a template, a body, or media.")
            if node.get("buttons"):
                # Checked on save so a malformed button is a 400 on the workflow,
                # not a message that quietly goes out without its buttons.
                try:
                    clean_buttons(node["buttons"])
                except DjangoValidationError as exc:
                    raise WorkflowError(f"MESSAGE node '{node['id']}': {' '.join(exc.messages)}") from exc

        elif node_type == "TAG" and not node.get("tag_id"):
            raise WorkflowError(f"TAG node '{node['id']}' needs a 'tag_id'.")

        elif node_type == "ASSIGN_AGENT" and not node.get("agent_id"):
            raise WorkflowError(f"ASSIGN_AGENT node '{node['id']}' needs an 'agent_id'.")

    _reject_tight_cycles(by_id)


def _inbound_since_prompt(run: WorkflowRun):
    """
    This contact's inbound messages that count as an answer to this run.

    If a MESSAGE node already ran in this workflow, only messages after it was
    sent count. With no such anchor (e.g. a CONDITION straight off TRIGGER),
    the whole history counts.
    """
    queryset = Message.objects.filter(
        whatsapp_account_id=run.workflow.whatsapp_account_id, contact_id=run.contact_id,
        direction=Message.Direction.INBOUND,
    )
    last_prompt_at = run.context.get("last_prompt_at")
    if last_prompt_at:
        queryset = queryset.filter(queued_at__gt=last_prompt_at)
    return queryset


def _evaluate_condition(condition: dict, run: WorkflowRun) -> bool:
    field = condition.get("field")

    if field == "has_replied":
        return _inbound_since_prompt(run).exists()

    if field == "button_clicked":
        # `value` names one button id; without it the branch means "tapped any
        # of the buttons we offered" rather than a specific one.
        queryset = _inbound_since_prompt(run).exclude(replied_button_id="")
        wanted = condition.get("value")
        if wanted:
            queryset = queryset.filter(replied_button_id=str(wanted))
        return queryset.exists()

    if field == "has_tag":
        return run.contact.tags.filter(pk=condition.get("value")).exists()

    # Unreachable for anything saved through validate_nodes; a row written
    # before a condition field was retired can still land here.
    logger.warning("Workflow run %s: unknown condition field %r evaluated as false.", run.pk, field)
    return False


ACTIVE_RUN_STATUSES = (WorkflowRun.Status.RUNNING, WorkflowRun.Status.WAITING)


def active_run_for(workflow: Workflow, contact) -> WorkflowRun | None:
    return WorkflowRun.objects.filter(
        workflow=workflow, contact=contact, status__in=ACTIVE_RUN_STATUSES,
    ).order_by("-pk").first()


def _conversation_for(workflow: Workflow, contact, conversation=None):
    """
    The conversation a run should act in.

    ASSIGN_AGENT and AI_REPLY are both no-ops without one, and until now
    nothing ever set it -- start_workflow created every run with
    conversation=None, so two of the eight node types did nothing at all in
    production while looking perfectly healthy in the builder. The contact
    already has at most one conversation per account (there is a unique
    constraint on the pair), so there is exactly one right answer to find.
    """
    if conversation is not None:
        return conversation

    from apps.inbox.models import Conversation

    return Conversation.objects.filter(
        whatsapp_account_id=workflow.whatsapp_account_id, contact=contact,
    ).first()


def start_workflow(workflow: Workflow, contact, *, conversation=None, force: bool = False) -> WorkflowRun:
    """
    Begin a run of `workflow` for `contact`.

    Refuses when that contact is already part-way through this workflow,
    unless `force`. A contact in two concurrent runs of the same sequence gets
    every message in it twice, slightly out of step -- which is both the most
    obvious symptom of a broken automation and the fastest way to have a
    number reported.
    """
    if not workflow.is_active:
        raise WorkflowError("This workflow is not active.")
    validate_nodes(workflow.nodes)

    if not force:
        existing = active_run_for(workflow, contact)
        if existing is not None:
            raise WorkflowError(
                f"This contact is already in run #{existing.pk} of this workflow "
                f"({existing.get_status_display().lower()} at step '{existing.current_node_id}')."
            )

    trigger_node = next(n for n in workflow.nodes if n["type"] == "TRIGGER")
    run = WorkflowRun.objects.create(
        workflow=workflow, contact=contact, conversation=_conversation_for(workflow, contact, conversation),
        current_node_id=trigger_node.get("next", ""), status=WorkflowRun.Status.RUNNING,
    )
    _advance(run)
    return run


def resume_workflow(run: WorkflowRun) -> None:
    # Claimed with a conditional UPDATE, not a read-then-save: a run can now be
    # woken from two directions -- the WAIT node's scheduled task and a button
    # tap arriving early (see resume_runs_awaiting_button) -- and only one of
    # them may go on to advance it.
    claimed = WorkflowRun.objects.filter(pk=run.pk, status=WorkflowRun.Status.WAITING).update(
        status=WorkflowRun.Status.RUNNING, updated_at=timezone.now(),
    )
    if not claimed:
        return
    run.status = WorkflowRun.Status.RUNNING
    # A run started before the contact ever wrote in has no conversation yet;
    # by the time it wakes there usually is one, and the AI_REPLY /
    # ASSIGN_AGENT nodes further down the graph need it.
    if run.conversation_id is None:
        conversation = _conversation_for(run.workflow, run.contact)
        if conversation is not None:
            run.conversation = conversation
            WorkflowRun.objects.filter(pk=run.pk).update(conversation=conversation)
    _advance(run)


def resume_runs_awaiting_button(inbound_message: Message) -> None:
    """
    Wake any workflow that is parked on a WAIT whose next step asks which
    button was tapped.

    Without this, a workflow that offers buttons has to sit out its entire WAIT
    before noticing the contact already answered -- they tap "Yes" and hear
    nothing back for an hour. Only runs whose next node is a `button_clicked`
    CONDITION are woken, so an unrelated WAIT (a scheduled nudge, a pause
    between two messages) still runs its full course.
    """
    if not inbound_message.replied_button_id or not inbound_message.contact_id:
        return

    runs = WorkflowRun.objects.filter(
        contact_id=inbound_message.contact_id,
        status=WorkflowRun.Status.WAITING,
        workflow__whatsapp_account_id=inbound_message.whatsapp_account_id,
        workflow__is_active=True,
    ).select_related("workflow")

    for run in runs:
        node = next((n for n in run.workflow.nodes if n.get("id") == run.current_node_id), None)
        if not node or node.get("type") != "CONDITION":
            continue
        if node.get("condition", {}).get("field") != "button_clicked":
            continue
        logger.info(
            "Workflow run %s resumed early by button %r from contact %s.",
            run.pk, inbound_message.replied_button_id, run.contact_id,
        )
        resume_workflow(run)


def _finish(run: WorkflowRun, status, *, node_id=None, error: str = "") -> None:
    """Write a run's final state in one place, so no exit path forgets a field."""
    run.status = status
    run.error = error[:2000]
    if node_id is not None:
        run.current_node_id = node_id
    if status in (WorkflowRun.Status.COMPLETED, WorkflowRun.Status.FAILED):
        run.completed_at = timezone.now()
    run.save(update_fields=[
        "status", "error", "completed_at", "current_node_id", "context", "steps_taken", "updated_at",
    ])
    if status == WorkflowRun.Status.FAILED:
        logger.error("Workflow run %s failed at node %r: %s", run.pk, run.current_node_id, error)


def _advance(run: WorkflowRun) -> None:
    """
    Execute the graph from run.current_node_id until it pauses or finishes.

    Every exit from here is a *recorded* one. Before, an exception thrown
    part-way through -- the provider refusing a send, a template row deleted
    under the run -- escaped to the caller and left the row RUNNING with no
    explanation and nothing that would ever retry it; a run in that state was
    invisibly dead. Now the run is marked FAILED with the reason on it, which
    is both triageable and something sweep_stuck_runs can reason about.
    """
    try:
        _advance_inner(run)
    except Exception as exc:
        logger.exception("Workflow run %s crashed while advancing.", run.pk)
        _finish(run, WorkflowRun.Status.FAILED, error=f"{type(exc).__name__}: {exc}")


def _advance_inner(run: WorkflowRun) -> None:
    nodes_by_id = {n["id"]: n for n in run.workflow.nodes}
    node = nodes_by_id.get(run.current_node_id)
    steps_here = 0

    while node is not None:
        # Two budgets, because they catch two different failures. The
        # per-advance one catches a graph that loops with no WAIT in the cycle
        # (validate_nodes refuses to save one now, but rows predating that
        # check still exist). The lifetime one catches a cycle *through* a
        # WAIT, which is legal and can still run forever.
        steps_here += 1
        run.steps_taken += 1
        if steps_here > MAX_STEPS_PER_ADVANCE:
            _finish(run, WorkflowRun.Status.FAILED, node_id=node["id"], error=(
                f"Stopped after {MAX_STEPS_PER_ADVANCE} steps without pausing -- "
                f"this workflow loops with no Wait step to break it."
            ))
            return
        if run.steps_taken > MAX_TOTAL_STEPS:
            _finish(run, WorkflowRun.Status.FAILED, node_id=node["id"], error=(
                f"Stopped after {MAX_TOTAL_STEPS} total steps, the most one run may take."
            ))
            return

        node_type = node["type"]

        if node_type == "MESSAGE":
            spec = _resolve_node_message(node, run.workflow)
            send_automated_message(
                run.workflow.whatsapp_account, run.contact, spec,
                conversation=run.conversation, reference=f"workflow:{run.workflow_id}:{node['id']}",
            )
            run.context["last_prompt_at"] = timezone.now().isoformat()
            node = nodes_by_id.get(node.get("next"))

        elif node_type == "TAG":
            tag = ContactTag.objects.filter(tenant_id=run.workflow.tenant_id, pk=node.get("tag_id")).first()
            if tag:
                run.contact.tags.add(tag)
            else:
                logger.warning(
                    "Workflow run %s: TAG node %r points at tag %r, which is not this tenant's.",
                    run.pk, node["id"], node.get("tag_id"),
                )
            node = nodes_by_id.get(node.get("next"))

        elif node_type == "AI_REPLY":
            # Answers the contact's most recent message using the tenant's
            # knowledge base, then carries on down the graph. A workflow that
            # reaches this node has decided the AI should speak here, so the
            # usual "stay out of an assigned chat" rule does not apply -- but
            # everything else (handoff, reply limits, refusing to guess) does.
            _run_ai_node(run)
            node = nodes_by_id.get(node.get("next"))

        elif node_type == "ASSIGN_AGENT":
            _assign_agent(run, node)
            node = nodes_by_id.get(node.get("next"))

        elif node_type == "CONDITION":
            result = _evaluate_condition(node.get("condition", {}), run)
            node = nodes_by_id.get(node.get("on_true") if result else node.get("on_false"))

        elif node_type == "WAIT":
            run.current_node_id = node.get("next", "")
            run.status = WorkflowRun.Status.WAITING
            run.save(update_fields=["current_node_id", "status", "context", "steps_taken", "updated_at"])

            from .tasks import resume_workflow_run

            # Clamped rather than trusted: validate_nodes bounds this on save,
            # but a row written before it did can still carry a negative or
            # absurd value, and a negative countdown re-enters this function
            # immediately -- the loop the budgets above exist to prevent.
            minutes = node.get("minutes") or 0
            minutes = min(max(float(minutes), 0.0), float(MAX_WAIT_MINUTES))
            resume_workflow_run.apply_async(args=[run.pk], countdown=int(minutes * 60))
            return

        elif node_type == "END":
            _finish(run, WorkflowRun.Status.COMPLETED, node_id=node["id"])
            return

        else:
            _finish(run, WorkflowRun.Status.FAILED, node_id=node["id"],
                    error=f"Unknown node type {node_type!r}.")
            return

    # Ran off the end of the graph (a dangling reference slipped past validation) -- fail safe, don't loop forever.
    _finish(run, WorkflowRun.Status.COMPLETED)


def _assign_agent(run: WorkflowRun, node: dict) -> None:
    """
    Hand the conversation to a person.

    The agent id is re-checked against the tenant at run time, not just on
    save: an agent who has since left the tenant would otherwise keep being
    assigned chats they can no longer open, and a hand-crafted API call could
    point the node at somebody else's user id entirely.
    """
    if not run.conversation_id:
        logger.info("Workflow run %s reached ASSIGN_AGENT with no conversation to assign.", run.pk)
        return

    from django.contrib.auth import get_user_model

    from apps.inbox.models import Conversation

    agent_id = node.get("agent_id")
    if agent_id and not get_user_model().objects.filter(pk=agent_id, tenant_id=run.workflow.tenant_id).exists():
        logger.warning(
            "Workflow run %s: ASSIGN_AGENT node %r names user %r, who is not in this tenant. Left unassigned.",
            run.pk, node["id"], agent_id,
        )
        return

    Conversation.objects.filter(pk=run.conversation_id).update(assigned_agent_id=agent_id)


def _trigger_matches(config: dict, event: str, inbound_message: Message) -> bool:
    """Whether a workflow whose TRIGGER says `config` should start on this message."""
    if config.get("event") != event:
        return False

    if event == TriggerEvent.KEYWORD:
        probe = AutomationRule(
            trigger_type=AutomationRule.Trigger.KEYWORD,
            keyword=config.get("keyword", ""),
            keyword_match_type=config.get("match_type") or AutomationRule.MatchType.CONTAINS,
        )
        return probe.matches((inbound_message.body or "").strip())

    if event == TriggerEvent.BUTTON:
        return bool(inbound_message.replied_button_id) and config.get("button_id") == inbound_message.replied_button_id

    return True  # NEW_CONVERSATION / INBOUND_MESSAGE carry no extra condition


def start_workflows_for(inbound_message: Message, event: str) -> bool:
    """
    Start every active workflow on this account whose TRIGGER waits for `event`.

    Returns whether any run started, which is how the ladder in receivers.py
    keeps a single inbound message from being answered twice.

    One workflow failing to start never stops the others: a graph that no
    longer validates, or a contact already mid-run, is a problem with that
    workflow alone.
    """
    contact = inbound_message.contact
    if contact is None:
        return False

    account = inbound_message.whatsapp_account
    workflows = Workflow.objects.filter(
        tenant_id=account.tenant_id, whatsapp_account_id=account.pk, is_active=True,
    ).order_by("pk")

    started = False
    for workflow in workflows:
        config = trigger_config(workflow.nodes)
        if not config or not _trigger_matches(config, event, inbound_message):
            continue

        if not config.get("allow_restart") and active_run_for(workflow, contact) is not None:
            logger.info(
                "Workflow %s not restarted for contact %s: a run is already active.", workflow.pk, contact.pk,
            )
            started = True  # the contact *is* in this workflow; don't let the AI answer over it
            continue

        try:
            start_workflow(workflow, contact, conversation=inbound_message.conversation, force=True)
            started = True
        except WorkflowError as exc:
            logger.warning("Workflow %s could not start for contact %s: %s", workflow.pk, contact.pk, exc)
        except Exception:
            logger.exception("Workflow %s crashed while starting for contact %s.", workflow.pk, contact.pk)

    return started


def sweep_stuck_runs() -> dict:
    """
    Reclaim runs whose worker died mid-advance.

    A run is moved to RUNNING for exactly as long as _advance takes, so a row
    still RUNNING many minutes later means the process handling it went away
    -- a deploy, an OOM kill, a lost broker connection. Nothing else would
    ever look at it again: the resume task that would have re-queued it died
    with the worker. Failing it makes the stall visible on the run list
    instead of leaving a contact silently stranded mid-sequence.

    Returns a small summary so the periodic task can log what it did.
    """
    cutoff = timezone.now() - timedelta(minutes=STUCK_RUNNING_MINUTES)
    stuck = WorkflowRun.objects.filter(status=WorkflowRun.Status.RUNNING, updated_at__lt=cutoff)

    reclaimed = stuck.update(
        status=WorkflowRun.Status.FAILED,
        error=(
            f"Abandoned: still running {STUCK_RUNNING_MINUTES} minutes after its last step. "
            f"The worker handling it most likely restarted."
        ),
        completed_at=timezone.now(),
        updated_at=timezone.now(),
    )
    if reclaimed:
        logger.warning("Reclaimed %s workflow run(s) abandoned mid-step.", reclaimed)
    return {"reclaimed": reclaimed}


def _run_ai_node(run: WorkflowRun) -> None:
    """
    Let the AI agent answer inside a workflow, from the knowledge base the
    node names.

    A node may carry `knowledge_base` (an id). That is what makes "answer
    support questions from the support docs, and sales questions from the
    price list" one workflow rather than two agents -- the branch chooses the
    knowledge, and everything else about the agent stays the same.

    Failures are swallowed on purpose: a workflow that dies because the model
    was briefly unreachable would strand the contact mid-sequence, which is
    worse than one step going unanswered.
    """
    if run.conversation_id is None:
        logger.info("Workflow run %s reached an AI_REPLY node with no conversation to answer in.", run.pk)
        return

    from apps.ai.models import AIAgentConfig, KnowledgeBase
    from apps.ai.services import answer
    from apps.inbox.models import Conversation

    conversation = Conversation.objects.filter(pk=run.conversation_id).first()
    if conversation is None:
        return

    last_inbound = Message.objects.filter(
        conversation=conversation, direction=Message.Direction.INBOUND,
    ).order_by("-queued_at", "-id").first()
    if last_inbound is None:
        return

    node = _node_by_id(run).get(run.current_node_id) or {}
    override = node.get("knowledge_base")
    config = None
    if override:
        base = KnowledgeBase.objects.filter(pk=override, tenant_id=run.workflow.tenant_id).first()
        if base is not None:
            # An unsaved copy: this is one node's choice for one reply, not a
            # change to the tenant's agent.
            config = AIAgentConfig.for_tenant(run.workflow.tenant_id)
            config.knowledge_base = base

    try:
        answer(conversation, last_inbound, config=config)
    except Exception:
        logger.exception("AI_REPLY node failed in workflow run %s.", run.pk)


def _node_by_id(run: WorkflowRun) -> dict:
    return {node.get("id"): node for node in (run.workflow.nodes or []) if isinstance(node, dict)}


def _node_media_items(node: dict) -> list:
    """
    Attachments declared on a workflow MESSAGE node.

    Nodes are free-form JSON, so a node may carry either the single media_url
    it always could or a full media_items list. A malformed list is dropped
    rather than raised: a run that is already under way must not die on stored
    data the graph validator let through.
    """
    try:
        return clean_media_items(node.get("media_items"))
    except Exception:
        logger.warning("Ignoring malformed media_items on workflow node %s.", node.get("id"))
        return []


def _node_buttons(node: dict) -> list:
    """
    Interactive buttons declared on a workflow MESSAGE node.

    Cleaned defensively for the same reason as _node_media_items: nodes are
    free-form JSON, and a run already under way must not die on stored data
    the graph validator let through.
    """
    try:
        return clean_buttons(node.get("buttons"))
    except Exception:
        logger.warning("Ignoring malformed buttons on workflow node %s.", node.get("id"))
        return []


def _resolve_node_message(node: dict, workflow: Workflow) -> dict:
    if node.get("template_id"):
        from apps.templates_app.models import MessageTemplate

        template = MessageTemplate.objects.filter(tenant_id=workflow.tenant_id, pk=node["template_id"]).first()
        if template:
            return {
                "message_type": template.message_type, "body": template.body, "media_url": template.media_url,
                "media_items": resolve_media_items(spec_from_object(template)),
                "caption": template.caption, "filename": template.filename, "variables": template.variables,
                "buttons": template.buttons, "template_id": template.pk,
            }
    node_spec = {
        "message_type": node.get("message_type", "TEXT"), "body": node.get("body", ""),
        "media_url": node.get("media_url", ""), "caption": node.get("caption", ""),
        "filename": node.get("filename", ""), "variables": {}, "template_id": None,
        "media_items": _node_media_items(node),
        "buttons": _node_buttons(node),
    }
    node_spec["media_items"] = resolve_media_items(node_spec)
    return node_spec
