"""
Whether a bot is fit to go in front of customers, and everything wrong with it.

``flow.validate`` already refuses a graph that cannot run -- a dangling edge, an
orphan, a loop with no pause in it -- and it does that by raising on the first
problem, which is right for a save: the author is looking at the thing they
just changed and one sentence is the fastest way back to work.

Publishing asks a different question. "Can this be saved?" has one answer;
"should this answer real customers?" has a list, and the person pressing
Publish wants all of it at once rather than one item per attempt. So this
module **collects** rather than raises, and separates two kinds of finding:

* a **problem** blocks the publish. The bot would visibly fail: a message with
  no text, an AI node on a bot with AI switched off, a number that cannot send.
* a **warning** does not. It is a shape that is usually a mistake but is
  sometimes deliberate -- an unrouted button, a branch that just stops -- and
  refusing those would make the builder argue with authors who know what they
  are doing.

Nothing here re-implements ``flow.validate``. It is called first, and its one
sentence becomes the first problem in the list when the graph is unrunnable.
"""

from . import actions as actions_module
from . import flow as flow_module
from .models import Chatbot


def _problem(message, *, node_id="", node_label=""):
    return {"severity": "problem", "message": message, "node_id": node_id, "node_label": node_label}


def _warning(message, *, node_id="", node_label=""):
    return {"severity": "warning", "message": message, "node_id": node_id, "node_label": node_label}


def report(chatbot, *, flow=None) -> dict:
    """
    Everything standing between this bot and a publish.

    ``flow`` lets the builder check a graph it has not saved yet, so the Publish
    button can be honest about a draft in the browser. Left out, the saved
    draft is read -- which is what the publish endpoint itself does, because
    the publish acts on the stored graph and must not be talked into going
    ahead by a payload describing a different one.
    """
    from . import services

    if flow is None:
        flow = services.draft_version(chatbot).flow or {"nodes": []}

    findings = []
    findings += _check_graph(chatbot, flow)
    findings += _check_channel(chatbot)
    findings += _check_something_to_say(chatbot, flow)

    problems = [item for item in findings if item["severity"] == "problem"]
    return {
        "ok": not problems,
        "problems": problems,
        "warnings": [item for item in findings if item["severity"] == "warning"],
    }


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

def _check_graph(chatbot, flow) -> list:
    nodes = (flow or {}).get("nodes") or []
    if not nodes:
        # Not a problem on its own: a bot can be rules-only, or AI-only, and
        # `_check_something_to_say` is what decides whether silence is fatal.
        return []

    try:
        cleaned = flow_module.validate(flow, tenant_id=chatbot.tenant_id, chatbot=chatbot)
    except ValueError as exc:
        # The graph cannot run at all, so per-node checks below would be
        # reading a shape that was never normalised. One accurate problem beats
        # a list built on assumptions.
        #
        # ``ValueError`` rather than ``FlowError`` because ``actions.validate``
        # runs inside ``flow.validate`` and raises ``ActionError``, and both
        # descend from it. A stored draft can fail either: a graph saved before
        # a validator was tightened, or one naming a knowledge base that has
        # since been deleted. Letting that reach the publish endpoint uncaught
        # turns "you cannot publish this yet" into a 500.
        return [_problem(str(exc))]

    findings = []
    for node in cleaned["nodes"]:
        findings += _check_node(chatbot, node)
    return findings


def _check_node(chatbot, node) -> list:
    node_id, kind = node["id"], node["type"]
    label = node.get("label") or kind
    findings = []

    def problem(message):
        findings.append(_problem(message, node_id=node_id, node_label=label))

    def warning(message):
        findings.append(_warning(message, node_id=node_id, node_label=label))

    for action in node.get("actions") or []:
        findings += _check_action(chatbot, action, node_id, label)

    if kind == flow_module.NodeType.QUESTION:
        findings += _check_question(node, node_id, label)

    elif kind == flow_module.NodeType.AI:
        if not chatbot.ai_enabled:
            problem("This node asks the AI to answer, but AI answering is switched off for this bot.")
        elif chatbot.bot_type == Chatbot.BotType.RULE_BASED:
            problem("This node asks the AI to answer, but this bot is set to rules only.")

    elif kind == flow_module.NodeType.KNOWLEDGE:
        if chatbot.knowledge_base_id is None and not any(
            action.get("knowledge_base_id") for action in node.get("actions") or []
        ):
            warning(
                "No knowledge base is chosen here or on the bot, so this will answer from the "
                "workspace default."
            )

    # A node that runs, says nothing and leads nowhere is almost always an edge
    # somebody meant to draw. Never fatal -- "say this and stop" is a real
    # ending -- but worth naming, because the alternative is a conversation
    # that goes quiet for a reason nobody can see.
    if (
        kind not in flow_module.NodeType.TERMINAL
        and kind not in flow_module.NodeType.WAITING
        and kind != flow_module.NodeType.START
        and not flow_module._edges(node)
    ):
        warning("Nothing follows this node, so the conversation stops here.")

    if kind == flow_module.NodeType.START:
        if not node.get("next"):
            findings.append(_problem("The Start node does not lead anywhere.", node_id=node_id, node_label=label))
        findings += _check_trigger(chatbot, node, node_id, label)

    return findings


def _check_trigger(chatbot, node, node_id, label) -> list:
    """
    What a flow that starts on one thing leaves unanswered.

    Never a problem: a flow triggered on "refund" is exactly the right shape
    for a bot whose rules or AI answer everything else. It is worth saying out
    loud only when nothing *does* answer everything else, because the symptom
    then is a bot that appears to ignore most of what it is sent.
    """
    trigger = flow_module.trigger_of([node])
    if trigger["type"] in (flow_module.TriggerType.ANY_MESSAGE, flow_module.TriggerType.NEW_CONVERSATION):
        return []

    ai_on = chatbot.ai_enabled and chatbot.bot_type in (Chatbot.BotType.AI_BASED, Chatbot.BotType.HYBRID)
    if ai_on or chatbot.rules.filter(is_active=True).exists():
        return []

    return [_warning(
        f"This flow only starts {flow_module.describe_trigger(trigger)}. Nothing else answers this "
        "bot's other messages, so they will get the fallback message. Add a rule, switch AI "
        "answering on, or start the flow on any message.",
        node_id=node_id, node_label=label,
    )]


def _check_question(node, node_id, label) -> list:
    """
    A question's options, and where each of them goes.

    The rule this encodes: an option with no branch is fine *if* the node has a
    default path, because the answer then falls through to it. An option with
    no branch and no default is a dead end the customer reaches by tapping a
    button the bot itself offered, which is the failure the spec asks about by
    name.
    """
    findings = []
    routed = {route["value"] for route in node.get("routes") or []}
    has_default = bool(node.get("next"))

    for action in node.get("actions") or []:
        if action.get("type") != actions_module.ActionType.ASK_QUESTION:
            continue
        for button in action.get("buttons") or []:
            button_id = button.get("id")
            if not button_id or button_id in routed:
                continue
            title = button.get("title") or button_id
            if has_default:
                findings.append(_warning(
                    f"The '{title}' option has no branch of its own, so it follows the default path.",
                    node_id=node_id, node_label=label,
                ))
            else:
                findings.append(_problem(
                    f"The '{title}' option leads nowhere. Connect it, or give this question a "
                    "default path for answers you have not branched.",
                    node_id=node_id, node_label=label,
                ))
    return findings


def _check_action(chatbot, action, node_id, label) -> list:
    """
    What one step needs that saving it could not already have checked.

    Deliberately thin, and thinner than it first looks like it should be.
    ``actions.validate`` runs on every save: it already refuses a message with
    no body, a file step with no file, an API step with no URL, and any id that
    belongs to another workspace. Repeating those here would be dead code that
    reads like a safety net -- so what is left is the one class of problem a
    per-action validator cannot see, which is a step that is fine in itself and
    wrong *for this bot*.
    """
    kind = action.get("type")
    findings = []

    if kind in (actions_module.ActionType.AI_REPLY, actions_module.ActionType.KNOWLEDGE_ANSWER):
        if not chatbot.ai_enabled:
            findings.append(_problem(
                "This step needs the AI, which is switched off for this bot.",
                node_id=node_id, node_label=label,
            ))
        elif chatbot.bot_type == Chatbot.BotType.RULE_BASED:
            findings.append(_problem(
                "This step needs the AI, but this bot is set to rules only.",
                node_id=node_id, node_label=label,
            ))

    if kind == actions_module.ActionType.ASK_QUESTION and not (action.get("save_to") or "").strip():
        findings.append(_warning(
            "This question does not save the answer anywhere, so nothing later can use it.",
            node_id=node_id, node_label=label,
        ))

    return findings


# ---------------------------------------------------------------------------
# The bot itself
# ---------------------------------------------------------------------------

def _check_channel(chatbot) -> list:
    """
    Whether the number this bot answers on can actually send.

    Read through the same function the engine uses at send time, so what the
    Publish dialog says and what the trail records the night it fails are the
    same sentence.
    """
    from . import outbound

    problem = outbound.account_problem(chatbot.whatsapp_account)
    if not problem:
        return []

    # A number that is merely disconnected is a warning, not a blocker:
    # publishing before reconnecting the phone is a normal order to do things
    # in, and the bot simply waits. Having no number at all is fatal, because
    # there is nothing for the published version to attach to.
    if chatbot.whatsapp_account_id is None:
        return [_problem(problem)]
    return [_warning(problem)]


def _check_something_to_say(chatbot, flow) -> list:
    """
    The check that was already in the publish endpoint, kept whole.

    A bot with no rules, no graph, no welcome message and no AI is not a bot;
    publishing it puts a number in front of customers that answers nothing.
    """
    has_rules = chatbot.rules.filter(is_active=True).exists()
    has_flow = bool((flow or {}).get("nodes"))
    ai_on = chatbot.ai_enabled and chatbot.bot_type in (Chatbot.BotType.AI_BASED, Chatbot.BotType.HYBRID)

    if not has_rules and not has_flow and not ai_on and not chatbot.welcome_message.strip():
        return [_problem(
            "This bot has nothing to say yet. Add a rule, build a flow, write a welcome "
            "message, or switch AI answering on."
        )]
    if chatbot.bot_type == Chatbot.BotType.RULE_BASED and not has_rules and not has_flow:
        return [_problem("A rules-only bot needs at least one rule or a flow.")]
    return []
