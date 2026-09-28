"""
The flow graph: a conversation as a set of nodes with edges between them.

A node is a position plus a list of actions, and every action is one of the
things ``actions.py`` already knows how to do. That is the whole trick: the
flow builder and the rule builder produce the same vocabulary, so a message
written in one behaves identically to the same message written in the other,
including its buttons, its variables and what it costs.

The graph is stored on ``ChatbotVersion.flow`` -- never only in front-end state
-- and validated on save. Validation is not a formality here: an unvalidated
graph can loop forever, can point at a node that does not exist, and can name
another tenant's knowledge base.

**When a conversation enters** is the START node's ``trigger``. A flow used to
begin on any message at all, which is right for a bot whose whole job is the
graph and wrong for one that also has rules and an AI -- "run the returns flow
when somebody says 'return'" had to be written as a rule that duplicated the
first node. The trigger is one field on the node that is already there, and it
speaks the vocabulary ``ChatbotRule`` already speaks, so the same matcher
decides both. See ``trigger_matches``.

**Where a conversation is** lives on ``ChatbotSession.current_node_id`` and
``awaiting_node_id``. A flow that asks a question stops and returns; the
customer's next message resumes it. There is no worker sitting on a socket
waiting, and a flow half-run when the process restarts simply carries on from
its stored position.
"""

import logging
import re

from . import actions as actions_module
from . import matching
from .models import ChatbotEvent, ChatbotRule

logger = logging.getLogger("sendstack")

#: Most nodes one graph may hold. A flow bigger than this is unmaintainable by
#: the person who has to edit it, long before it is expensive for us to run.
MAX_NODES = 200

#: Most nodes one inbound message may walk through. This is the loop backstop:
#: a graph with a cycle that contains no QUESTION node would otherwise run
#: until the worker died. Deliberately generous -- a real branchy flow can pass
#: a dozen conditions before it says anything.
MAX_STEPS_PER_TURN = 40

_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Most branches one question may fan out into. WhatsApp itself allows three
#: reply buttons or ten list rows, so anything above ten is already a question
#: no connection can ask -- the ceiling is here to bound the graph, not to
#: express the protocol.
MAX_ROUTES = 10


class NodeType:
    START = "START"
    MESSAGE = "MESSAGE"
    QUESTION = "QUESTION"
    CONDITION = "CONDITION"
    AI = "AI"
    KNOWLEDGE = "KNOWLEDGE"
    MEDIA = "MEDIA"
    DELAY = "DELAY"
    TAG = "TAG"
    VARIABLE = "VARIABLE"
    HANDOFF = "HANDOFF"
    TOOL = "TOOL"
    END = "END"

    ALL = (
        START, MESSAGE, QUESTION, CONDITION, AI, KNOWLEDGE, MEDIA, DELAY,
        TAG, VARIABLE, HANDOFF, TOOL, END,
    )
    #: Nodes with nothing after them.
    TERMINAL = frozenset({END})
    #: Nodes that stop and wait for the customer.
    WAITING = frozenset({QUESTION})


class TriggerType:
    """
    What starts a flow.

    Every member except the first two is a ``ChatbotRule.MatchType`` under
    another name, and ``_MATCH_TYPE`` below is the whole of the translation.
    That is deliberate: a keyword trigger and a keyword rule must claim exactly
    the same messages, including the whole-word rule and the regex timeout
    defence, and the only way to guarantee that is for them to be one matcher
    rather than two that agree today.
    """

    #: Any inbound message, which is what every flow written before triggers
    #: existed did -- so it is also what a missing trigger means.
    ANY_MESSAGE = "ANY_MESSAGE"
    #: The first message of a conversation, and nothing after it.
    NEW_CONVERSATION = "NEW_CONVERSATION"
    KEYWORD = "KEYWORD"
    CONTAINS = "CONTAINS"
    EXACT = "EXACT"
    STARTS_WITH = "STARTS_WITH"
    REGEX = "REGEX"
    BUTTON = "BUTTON"

    ALL = (
        ANY_MESSAGE, NEW_CONVERSATION, KEYWORD, CONTAINS, EXACT,
        STARTS_WITH, REGEX, BUTTON,
    )
    #: Triggers that are nothing without something to match against.
    NEEDS_PATTERN = frozenset({KEYWORD, CONTAINS, EXACT, STARTS_WITH, REGEX, BUTTON})


#: Trigger -> the rule match type that decides it. The two absent members are
#: the two that do not look at the message at all.
_MATCH_TYPE = {
    TriggerType.KEYWORD: ChatbotRule.MatchType.KEYWORD,
    TriggerType.CONTAINS: ChatbotRule.MatchType.CONTAINS,
    TriggerType.EXACT: ChatbotRule.MatchType.EXACT,
    TriggerType.STARTS_WITH: ChatbotRule.MatchType.STARTS_WITH,
    TriggerType.REGEX: ChatbotRule.MatchType.REGEX,
    TriggerType.BUTTON: ChatbotRule.MatchType.BUTTON,
}

#: A flow with no trigger of its own. Returned rather than stored, so a graph
#: saved before this field existed reads as "on any message" -- which is what
#: it did -- without a migration over everybody's flow JSON.
DEFAULT_TRIGGER = {"type": TriggerType.ANY_MESSAGE, "pattern": "", "case_sensitive": False}


#: Which action types each node may carry. A node type that accepted anything
#: would make the node type decorative; this is what lets the builder show the
#: right form for the node the author picked.
_ALLOWED_ACTIONS = {
    NodeType.START: set(),
    NodeType.MESSAGE: {actions_module.ActionType.SEND_MESSAGE},
    NodeType.QUESTION: {actions_module.ActionType.ASK_QUESTION},
    NodeType.CONDITION: set(),
    NodeType.AI: {actions_module.ActionType.AI_REPLY},
    NodeType.KNOWLEDGE: {actions_module.ActionType.KNOWLEDGE_ANSWER},
    NodeType.MEDIA: {actions_module.ActionType.SEND_MEDIA},
    NodeType.DELAY: set(),
    NodeType.TAG: {actions_module.ActionType.ADD_TAG, actions_module.ActionType.REMOVE_TAG},
    NodeType.VARIABLE: {actions_module.ActionType.SET_VARIABLE},
    NodeType.HANDOFF: {actions_module.ActionType.HANDOFF, actions_module.ActionType.ASSIGN_AGENT},
    NodeType.TOOL: {
        actions_module.ActionType.CALL_TOOL, actions_module.ActionType.CALL_API,
        actions_module.ActionType.TRIGGER_CAMPAIGN, actions_module.ActionType.TRIGGER_AUTOMATION,
    },
    NodeType.END: {actions_module.ActionType.END_CONVERSATION, actions_module.ActionType.SEND_MESSAGE},
}

#: What a CONDITION may test, and how. A closed list rather than an expression
#: language: an expression a customer writes is code this deployment runs, and
#: "add eval" is how a configuration format becomes a vulnerability.
CONDITION_FIELDS = (
    "last_message", "button_clicked", "language", "variable", "contact_tag",
    "ai_replies", "bot_replies", "messages_received", "state", "within_hours",
)
CONDITION_OPERATORS = (
    "equals", "not_equals", "contains", "not_contains", "starts_with",
    "matches", "exists", "not_exists", "greater_than", "less_than",
)


class FlowError(ValueError):
    """Raised for a graph that cannot be saved. The message is shown to its author."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(flow, *, tenant_id, chatbot=None) -> dict:
    """
    Check and normalise one graph, or raise ``FlowError``.

    What is enforced, and why each one is here rather than left to run time:

    * exactly one START, so "where does this begin" has one answer;
    * unique, well-formed node ids, so an edge names one node;
    * every edge points at a node that exists, so a flow cannot silently stop;
    * every node is reachable from START, because an unreachable node is
      almost always an edge somebody forgot to draw rather than a deliberate
      orphan, and it is far cheaper to say so now;
    * no cycle that could spin without ever waiting for the customer -- see
      ``_reject_tight_cycles``;
    * every action validated by ``actions.validate``, which is where the
      cross-tenant id checks live.
    """
    if flow in (None, {}, []):
        return {"nodes": []}
    if isinstance(flow, list):
        flow = {"nodes": flow}
    if not isinstance(flow, dict):
        raise FlowError("A flow must be an object with a 'nodes' list.")

    nodes = flow.get("nodes") or []
    if not isinstance(nodes, list):
        raise FlowError("'nodes' must be a list.")
    if not nodes:
        return {"nodes": []}
    if len(nodes) > MAX_NODES:
        raise FlowError(f"A flow may hold at most {MAX_NODES} nodes.")

    cleaned = []
    seen_ids = set()
    starts = 0

    for index, node in enumerate(nodes, start=1):
        if not isinstance(node, dict):
            raise FlowError(f"Node {index} is not an object.")

        node_id = str(node.get("id") or "").strip()
        if not _NODE_ID.match(node_id):
            raise FlowError(f"Node {index} needs an id of letters, numbers, dashes or underscores.")
        if node_id in seen_ids:
            raise FlowError(f"Two nodes share the id '{node_id}'.")
        seen_ids.add(node_id)

        node_type = str(node.get("type") or "").strip().upper()
        if node_type not in NodeType.ALL:
            raise FlowError(f"Node '{node_id}': '{node_type or 'missing'}' is not a known node type.")
        if node_type == NodeType.START:
            starts += 1

        node_actions = actions_module.validate(
            node.get("actions") or [], tenant_id=tenant_id, chatbot=chatbot,
        )
        allowed = _ALLOWED_ACTIONS[node_type]
        for action in node_actions:
            if action["type"] not in allowed:
                raise FlowError(
                    f"Node '{node_id}' is a {node_type} node, which cannot do '{action['type']}'."
                )

        entry = {
            "id": node_id,
            "type": node_type,
            "label": str(node.get("label") or "")[:120],
            "actions": node_actions,
            "next": str(node.get("next") or "").strip(),
            "position": _position(node.get("position")),
        }

        if node_type == NodeType.START:
            entry["trigger"] = _validate_trigger(node.get("trigger"), node_id)
        elif node_type == NodeType.CONDITION:
            entry["condition"] = _validate_condition(node.get("condition") or {}, node_id)
            entry["true_next"] = str(node.get("true_next") or "").strip()
            entry["false_next"] = str(node.get("false_next") or "").strip()
            if not entry["true_next"] and not entry["false_next"]:
                raise FlowError(f"Node '{node_id}' is a condition with no branches.")
        elif node_type == NodeType.DELAY:
            seconds = int(node.get("seconds") or 0)
            if not 0 < seconds <= 120:
                raise FlowError(f"Node '{node_id}': a delay must be between 1 and 120 seconds.")
            entry["seconds"] = seconds
        elif node_type == NodeType.QUESTION:
            if not node_actions:
                raise FlowError(f"Node '{node_id}' is a question with nothing to ask.")
            entry["routes"] = _validate_routes(node.get("routes"), node_actions, node_id)

        cleaned.append(entry)

    if starts != 1:
        raise FlowError(
            "A flow needs exactly one Start node." if starts == 0 else "A flow may only have one Start node."
        )

    by_id = {node["id"]: node for node in cleaned}
    _check_edges(cleaned, by_id)
    _check_reachable(cleaned, by_id)
    _reject_tight_cycles(cleaned, by_id)
    return {"nodes": cleaned}


def _validate_routes(routes, node_actions, node_id) -> list:
    """
    Check the per-option branches hanging off one question.

    A route is ``{"value": <button id>, "next": <node id>}`` and means "if they
    chose this, go there". It exists so a three-button question is three edges
    on the canvas rather than three CONDITION nodes downstream of it -- the
    graph then says what the conversation does, which is the entire point of
    drawing it.

    **Routes match on the button's id, never on its label.** A tap arrives as
    ``Message.replied_button_id``, and on a QR-linked number -- where a question
    goes out as a numbered list -- ``automation.resolve_option_reply`` has
    already turned the typed "2" into that same id before the engine sees the
    message. Matching on the label instead would break the moment somebody
    fixed a typo in a live flow, and would never have worked on a numbered
    reply at all.

    Every route's value is checked against the ids the question actually
    offers. A route pointing at a button that no longer exists is the exact
    "this button goes nowhere" mistake publish validation is meant to catch,
    and catching it here means it is caught on save, in front of the person who
    just made it.
    """
    if routes in (None, ""):
        return []
    if not isinstance(routes, list):
        raise FlowError(f"Node '{node_id}': routes must be a list.")
    if len(routes) > MAX_ROUTES:
        raise FlowError(f"Node '{node_id}' has more than {MAX_ROUTES} branches.")

    offered = set()
    for action in node_actions:
        if action["type"] == actions_module.ActionType.ASK_QUESTION:
            offered.update(
                button["id"] for button in action.get("buttons") or [] if button.get("id")
            )

    cleaned, seen = [], set()
    for index, route in enumerate(routes, start=1):
        if not isinstance(route, dict):
            raise FlowError(f"Node '{node_id}': branch {index} is not an object.")

        value = str(route.get("value") or "").strip()
        if not value:
            raise FlowError(f"Node '{node_id}': branch {index} does not say which answer it is for.")
        if value in seen:
            raise FlowError(f"Node '{node_id}': two branches both handle '{value}'.")
        seen.add(value)

        if offered and value not in offered:
            raise FlowError(
                f"Node '{node_id}': branch {index} is for '{value}', which is not one of the "
                "options this question offers. Rebuild the branch after changing the buttons."
            )

        target = str(route.get("next") or "").strip()
        if not target:
            raise FlowError(f"Node '{node_id}': the '{value}' branch does not lead anywhere.")

        cleaned.append({"value": value[:64], "next": target})
    return cleaned


def _position(value):
    """Where the node sits on the canvas. Cosmetic, so a bad value is dropped rather than fatal."""
    if not isinstance(value, dict):
        return {"x": 0, "y": 0}
    try:
        return {"x": float(value.get("x", 0)), "y": float(value.get("y", 0))}
    except (TypeError, ValueError):
        return {"x": 0, "y": 0}


def _edges(node) -> list:
    """
    Every node id this one can hand control to.

    One function, used by the dangling-edge check, the reachability check and
    the cycle detector alike -- which is what makes a new kind of branch safe
    to add. A question's per-option routes are edges in exactly the sense those
    three care about, so listing them here is the whole of what "the loop
    checker understands buttons" required.
    """
    edges = [node.get("next")]
    if node["type"] == NodeType.CONDITION:
        edges += [node.get("true_next"), node.get("false_next")]
    elif node["type"] == NodeType.QUESTION:
        edges += [route.get("next") for route in node.get("routes") or []]
    return [edge for edge in edges if edge]


def _check_edges(nodes, by_id) -> None:
    for node in nodes:
        for edge in _edges(node):
            if edge not in by_id:
                raise FlowError(f"Node '{node['id']}' points at '{edge}', which does not exist.")
        if node["type"] in NodeType.TERMINAL and node.get("next"):
            raise FlowError(f"Node '{node['id']}' is an End node, so nothing can follow it.")


def _check_reachable(nodes, by_id) -> None:
    start = next(node for node in nodes if node["type"] == NodeType.START)
    seen, stack = {start["id"]}, [start["id"]]
    while stack:
        for edge in _edges(by_id[stack.pop()]):
            if edge not in seen:
                seen.add(edge)
                stack.append(edge)

    orphans = [node["id"] for node in nodes if node["id"] not in seen]
    if orphans:
        raise FlowError(
            "These nodes cannot be reached from the Start node: " + ", ".join(sorted(orphans)[:8]) + "."
        )


def _reject_tight_cycles(nodes, by_id) -> None:
    """
    Refuse a loop that never stops to listen.

    A cycle is legitimate -- "ask again if the answer was not one of the
    options" is a loop, and a good one. What is not legitimate is a cycle whose
    every node runs without waiting: the engine would walk it until
    ``MAX_STEPS_PER_TURN`` stopped it, having sent forty messages on the way.
    So the rule is per-cycle rather than blanket: a cycle must contain at least
    one node that waits for the customer.

    Detected with an iterative depth-first search rather than recursion,
    because a 200-node graph laid out as a chain would otherwise be a stack
    overflow somebody could cause from a form.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node["id"]: WHITE for node in nodes}

    for root in list(colour):
        if colour[root] != WHITE:
            continue
        # (node id, iterator over its edges) -- the explicit stack that
        # replaces recursion. `path` mirrors it so a back edge can name the
        # cycle it closes.
        stack = [(root, iter(_edges(by_id[root])))]
        colour[root] = GREY
        path = [root]

        while stack:
            node_id, edges = stack[-1]
            advanced = False
            for edge in edges:
                if colour[edge] == GREY:
                    cycle = path[path.index(edge):]
                    if not any(by_id[member]["type"] in NodeType.WAITING for member in cycle):
                        raise FlowError(
                            "This loop would run forever without waiting for the customer: "
                            + " -> ".join(cycle + [edge])
                            + ". Put a Question node in it."
                        )
                elif colour[edge] == WHITE:
                    colour[edge] = GREY
                    path.append(edge)
                    stack.append((edge, iter(_edges(by_id[edge]))))
                    advanced = True
                    break
            if not advanced:
                colour[node_id] = BLACK
                stack.pop()
                path.pop()


def _validate_trigger(trigger, node_id) -> dict:
    """
    Check the one thing on a START node, or raise ``FlowError``.

    Absent is legal and means "on any message": every graph drawn before
    triggers existed started that way, and refusing them on their next save
    would have been a migration disguised as a validation rule.

    A pattern trigger with no pattern is refused here rather than being left to
    match nothing at run time, because a flow that silently never starts is the
    single hardest chatbot fault to diagnose from the outside.
    """
    if trigger in (None, "", {}):
        return dict(DEFAULT_TRIGGER)
    if not isinstance(trigger, dict):
        raise FlowError(f"Node '{node_id}': the trigger must be an object.")

    kind = str(trigger.get("type") or "").strip().upper()
    if kind not in TriggerType.ALL:
        raise FlowError(
            f"Node '{node_id}': '{kind or 'missing'}' is not something that can start a flow."
        )

    pattern = str(trigger.get("pattern") or "").strip()
    case_sensitive = bool(trigger.get("case_sensitive"))

    if kind not in TriggerType.NEEDS_PATTERN:
        # Dropped rather than kept: a pattern left behind by switching the
        # trigger type would reappear if somebody switched back, having been
        # invisible in between.
        return {"type": kind, "pattern": "", "case_sensitive": False}

    if not pattern:
        raise FlowError(
            f"Node '{node_id}': say what this flow starts on -- "
            + ("the id of the option that starts it." if kind == TriggerType.BUTTON
               else "the words to look for.")
        )
    if len(pattern) > matching.MAX_PATTERN_LENGTH:
        raise FlowError(
            f"Node '{node_id}': a trigger may be at most {matching.MAX_PATTERN_LENGTH} characters."
        )
    if kind == TriggerType.KEYWORD and not matching.keywords(pattern):
        raise FlowError(f"Node '{node_id}': list at least one word this flow starts on.")
    if kind == TriggerType.REGEX:
        try:
            matching.compile_pattern(pattern, case_sensitive=case_sensitive)
        except matching.PatternError as exc:
            raise FlowError(f"Node '{node_id}': {exc}") from exc

    return {"type": kind, "pattern": pattern, "case_sensitive": case_sensitive}


def _validate_condition(condition, node_id) -> dict:
    if not isinstance(condition, dict):
        raise FlowError(f"Node '{node_id}': the condition must be an object.")

    field = str(condition.get("field") or "").strip().lower()
    if field not in CONDITION_FIELDS:
        raise FlowError(
            f"Node '{node_id}': '{field or 'missing'}' is not something a condition can test. "
            f"Choose one of: {', '.join(CONDITION_FIELDS)}."
        )

    operator = str(condition.get("operator") or "equals").strip().lower()
    if operator not in CONDITION_OPERATORS:
        raise FlowError(f"Node '{node_id}': '{operator}' is not a known comparison.")

    name = str(condition.get("name") or "").strip()
    if field == "variable":
        from . import variables

        if not name:
            raise FlowError(f"Node '{node_id}': name the variable to test.")
        try:
            name = variables.normalise_name(name)
        except variables.VariableError as exc:
            raise FlowError(f"Node '{node_id}': {exc}") from exc

    value = condition.get("value")
    if operator == "matches":
        try:
            matching.compile_pattern(str(value or ""))
        except matching.PatternError as exc:
            raise FlowError(f"Node '{node_id}': {exc}") from exc

    return {"field": field, "operator": operator, "name": name, "value": value}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def nodes_of(version) -> list:
    """The graph a session is running, as a list. Never raises on a malformed column."""
    flow = getattr(version, "flow", None) or {}
    nodes = flow.get("nodes") if isinstance(flow, dict) else flow
    return nodes if isinstance(nodes, list) else []


def start_node(nodes):
    return next((node for node in nodes if node.get("type") == NodeType.START), None)


def node_by_id(nodes, node_id):
    return next((node for node in nodes if node.get("id") == node_id), None)


# ---------------------------------------------------------------------------
# What starts a flow
# ---------------------------------------------------------------------------

def trigger_of(nodes) -> dict:
    """The trigger on this graph's START node, defaulted for a graph saved without one."""
    start = start_node(nodes) or {}
    trigger = start.get("trigger")
    if not isinstance(trigger, dict) or not trigger.get("type"):
        return dict(DEFAULT_TRIGGER)
    return {
        "type": trigger.get("type"),
        "pattern": trigger.get("pattern") or "",
        "case_sensitive": bool(trigger.get("case_sensitive")),
    }


def trigger_matches(trigger, *, text: str, button_id: str = "", is_new_conversation: bool = False) -> bool:
    """
    Whether this message is the one that starts the flow.

    Pure, over primitives, so the builder's trigger tester and the engine run
    the same function -- the same reason ``matching.rule_matches`` is shaped
    this way, and this delegates to it for every trigger that looks at what the
    customer actually said.
    """
    kind = (trigger or {}).get("type") or TriggerType.ANY_MESSAGE
    if kind == TriggerType.ANY_MESSAGE:
        return True
    if kind == TriggerType.NEW_CONVERSATION:
        return bool(is_new_conversation)

    match_type = _MATCH_TYPE.get(kind)
    if match_type is None:
        # An unknown trigger type -- a graph hand-edited in the admin, or one
        # from a newer export. It starts on nothing rather than on everything:
        # a flow that stays quiet is recoverable, a flow that answers every
        # message when it was meant to answer one word is not.
        logger.warning("Chatbot flow has an unknown trigger type %r; the flow will not start.", kind)
        return False

    return matching.rule_matches(
        {
            "match_type": match_type,
            "pattern": trigger.get("pattern") or "",
            "case_sensitive": bool(trigger.get("case_sensitive")),
        },
        text=text,
        button_id=button_id,
    )


def describe_trigger(trigger) -> str:
    """One sentence naming what starts this flow, for the trail and the builder."""
    kind = (trigger or {}).get("type") or TriggerType.ANY_MESSAGE
    pattern = (trigger or {}).get("pattern") or ""

    if kind == TriggerType.ANY_MESSAGE:
        return "on any message"
    if kind == TriggerType.NEW_CONVERSATION:
        return "on the first message of a conversation"
    if kind == TriggerType.KEYWORD:
        return "when the message mentions " + " or ".join(
            f"'{word}'" for word in matching.keywords(pattern)[:5]
        )
    if kind == TriggerType.CONTAINS:
        return f"when the message contains '{pattern}'"
    if kind == TriggerType.EXACT:
        return f"when the message is exactly '{pattern}'"
    if kind == TriggerType.STARTS_WITH:
        return f"when the message starts with '{pattern}'"
    if kind == TriggerType.REGEX:
        return f"when the message matches the pattern '{pattern}'"
    if kind == TriggerType.BUTTON:
        return f"when the '{pattern}' option is tapped"
    return "on a trigger this version does not understand"


def should_start(ctx) -> bool:
    """
    Whether this turn is the one that enters the flow, writing the reason either way.

    Consulted only when a conversation is *not* already somewhere in the graph
    -- a flow half-run carries on regardless of what started it, because the
    customer is mid-conversation and the trigger has already done its job.
    """
    trigger = trigger_of(nodes_of(ctx.version))
    matched = trigger_matches(
        trigger,
        text=ctx.text,
        button_id=ctx.button_id,
        # The same test the welcome message uses: this session's first message.
        is_new_conversation=ctx.session.messages_received <= 1,
    )
    if not matched:
        ctx.trace.skipped(
            ChatbotEvent.Step.FLOW,
            f"The flow starts {describe_trigger(trigger)}, and this message is not it.",
            trigger=trigger["type"],
        )
    return matched


def run(ctx, *, from_node_id: str = "") -> str:
    """
    Walk the graph from one node until it waits, ends, or runs out of steps.

    Returns ``"AWAIT"``, ``"END"``, ``"HANDOFF"``, ``"TRANSFER"``, ``"DONE"``
    (the graph ran off the end of a branch, which is a legitimate way to
    finish) or ``""`` when there is no graph to run at all.

    The position is written to the session at every hop rather than at the end,
    so a worker that dies half-way through leaves a conversation parked
    somewhere real instead of back at the start.
    """
    from .models import ChatbotSession

    nodes = nodes_of(ctx.version)
    if not nodes:
        return ""

    node = node_by_id(nodes, from_node_id) if from_node_id else start_node(nodes)
    if node is None:
        ctx.trace.skipped(ChatbotEvent.Step.FLOW, "The flow has no starting point.")
        return ""

    steps = 0
    while node is not None:
        steps += 1
        if steps > MAX_STEPS_PER_TURN:
            ctx.trace.failed(
                ChatbotEvent.Step.FLOW,
                f"Stopped after {MAX_STEPS_PER_TURN} steps -- this flow appears to loop.",
                node_id=node["id"],
            )
            return "DONE"

        ChatbotSession.objects.filter(pk=ctx.session.pk).update(current_node_id=node["id"])
        ctx.session.current_node_id = node["id"]
        ctx.trace.step(
            ChatbotEvent.Step.FLOW, node.get("label") or node["type"],
            node_id=node["id"], node_type=node["type"],
        )

        node_type = node["type"]

        if node_type == NodeType.CONDITION:
            branch = evaluate_condition(node["condition"], ctx)
            ctx.trace.step(
                ChatbotEvent.Step.FLOW, f"Condition was {branch}.",
                node_id=node["id"], field=node["condition"]["field"], result=branch,
            )
            node = node_by_id(nodes, node["true_next"] if branch else node["false_next"])
            continue

        if node_type == NodeType.DELAY:
            # Applied to the *next* message rather than by sleeping, because a
            # worker asleep for two minutes is a worker not answering anybody
            # else. See ``EngineContext.extra_delay``.
            ctx.extra_delay += int(node.get("seconds") or 0)
            node = node_by_id(nodes, node.get("next"))
            continue

        outcome = actions_module.run(ctx, node.get("actions") or [], reference=f"chatbot:flow:{node['id']}")

        if node_type in NodeType.WAITING or outcome == "AWAIT":
            ChatbotSession.objects.filter(pk=ctx.session.pk).update(
                awaiting_node_id=node["id"], state=ChatbotSession.State.WAITING_FOR_CUSTOMER,
            )
            ctx.session.awaiting_node_id = node["id"]
            ctx.session.state = ChatbotSession.State.WAITING_FOR_CUSTOMER
            return "AWAIT"

        if outcome in ("HANDOFF", "TRANSFER", "END"):
            return outcome
        if node_type in NodeType.TERMINAL:
            from . import services

            services.resolve_session(ctx.session, ChatbotSession.Resolution.BOT)
            return "END"

        node = node_by_id(nodes, node.get("next"))

    # Ran off the end of a branch without an End node. Legitimate -- a branch
    # that says one thing and stops is a normal shape -- so the session simply
    # waits for whatever the customer says next.
    ChatbotSession.objects.filter(pk=ctx.session.pk).update(
        current_node_id="", awaiting_node_id="", state=ChatbotSession.State.BOT_ACTIVE,
    )
    ctx.session.current_node_id = ""
    ctx.session.awaiting_node_id = ""
    return "DONE"


def resume(ctx) -> str:
    """
    Continue a flow that was waiting on the customer.

    By the time this runs the answer has already been written into its variable
    (see ``engine._consume_pending_answer``), so a condition immediately after
    the question can test what was just said.
    """
    from .models import ChatbotSession

    nodes = nodes_of(ctx.version)
    node = node_by_id(nodes, ctx.session.awaiting_node_id)
    if node is None:
        ChatbotSession.objects.filter(pk=ctx.session.pk).update(awaiting_node_id="")
        ctx.session.awaiting_node_id = ""
        return ""

    # The answer itself has already been stored by
    # ``engine._consume_pending_answer``, which runs for a rule-asked question
    # too. This is only the position half.
    ChatbotSession.objects.filter(pk=ctx.session.pk).update(awaiting_node_id="")
    ctx.session.awaiting_node_id = ""
    ctx.trace.step(ChatbotEvent.Step.FLOW, "Resumed after a question.", node_id=node["id"])

    following = node_by_id(nodes, _answer_target(node, ctx))
    if following is None:
        ChatbotSession.objects.filter(pk=ctx.session.pk).update(
            current_node_id="", state=ChatbotSession.State.BOT_ACTIVE,
        )
        return "DONE"
    return run(ctx, from_node_id=following["id"])


def _answer_target(node, ctx) -> str:
    """
    Which node the customer's answer leads to.

    The option they chose wins; ``next`` is the "anything else" path. That
    ordering is what makes an unrouted answer fall through to the rest of the
    flow rather than stopping dead -- a question with three buttons and a
    free-text answer is a normal thing for somebody to send, and the author
    almost always wants it handled by whatever follows.

    ``ctx.button_id`` is empty when they typed something the option resolver
    could not match to a choice, and an empty id must never match a route.
    """
    chosen = ctx.button_id
    if chosen:
        for route in node.get("routes") or []:
            if route.get("value") == chosen:
                ctx.trace.step(
                    ChatbotEvent.Step.FLOW, f"They chose '{chosen}'.",
                    node_id=node["id"], route=chosen, next_node_id=route.get("next"),
                )
                return route.get("next") or ""
    return node.get("next") or ""


def evaluate_condition(condition, ctx) -> bool:
    """
    Test one condition against the live conversation. Never raises.

    A condition that cannot be evaluated is False rather than an exception: a
    branch nobody can compute should take the "no" path and let the flow carry
    on, not end the conversation.
    """
    try:
        actual = _condition_value(condition, ctx)
    except Exception:  # noqa: BLE001
        logger.exception("Could not read '%s' for a chatbot condition.", condition.get("field"))
        return False

    operator = condition["operator"]
    expected = condition.get("value")

    if operator == "exists":
        return actual not in (None, "", [])
    if operator == "not_exists":
        return actual in (None, "", [])

    if operator in ("greater_than", "less_than"):
        try:
            left, right = float(actual or 0), float(expected or 0)
        except (TypeError, ValueError):
            return False
        return left > right if operator == "greater_than" else left < right

    left = str(actual if actual is not None else "").lower()
    right = str(expected if expected is not None else "").lower()

    if operator == "equals":
        return left == right
    if operator == "not_equals":
        return left != right
    if operator == "contains":
        return right in left
    if operator == "not_contains":
        return right not in left
    if operator == "starts_with":
        return left.startswith(right)
    if operator == "matches":
        return matching._regex_matches(str(expected or ""), str(actual or ""), case_sensitive=False)
    return False


def _condition_value(condition, ctx):
    field = condition["field"]
    session = ctx.session

    if field == "last_message":
        return ctx.text
    if field == "button_clicked":
        return ctx.button_id
    if field == "language":
        return ctx.reply_language
    if field == "variable":
        return (session.variables or {}).get(condition["name"])
    if field == "contact_tag":
        return ", ".join(ctx.contact.tags.values_list("name", flat=True))
    if field == "ai_replies":
        return session.ai_replies
    if field == "bot_replies":
        return session.bot_replies
    if field == "messages_received":
        return session.messages_received
    if field == "state":
        return session.state
    if field == "within_hours":
        from . import services

        return "yes" if services.is_within_hours(ctx.chatbot, ctx.version) else "no"
    return None
