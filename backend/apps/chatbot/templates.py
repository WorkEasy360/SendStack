"""
Starter flows: a working conversation somebody can edit, not an empty canvas.

Every template here is **built from the same vocabulary the builder produces**
and goes through ``flow.validate`` before it is handed out, which is what stops
this file drifting into a set of shapes the engine cannot actually run. A
template that no longer validates is a failing test rather than a bot that
breaks on publish -- see ``TemplateTests``.

Two deliberate limits on what a template may contain:

* **No ids of anything.** A template naming a knowledge base, a campaign, an
  agent or a WhatsApp account would either point at another workspace's row or
  at nothing at all. Where a template wants one, it leaves the field blank and
  the node carries a label saying what to choose.
* **No secrets, and no API calls.** ``CALL_API`` needs a URL and credentials
  that belong to one business; a starter flow that shipped somebody else's
  endpoint would be worse than no starter flow.

Positions are set here rather than left to the canvas's auto-layout, because a
template is the first graph most people ever see and it should already look
like a diagram somebody drew.
"""

from . import flow as flow_module

#: Canvas spacing. A column is wide enough for the widest node the builder
#: draws plus the gap that makes an edge readable between two of them.
_COL = 320
_ROW = 170


def _at(column, row):
    return {"x": column * _COL, "y": row * _ROW}


def _start(next_id, *, column=0, row=0):
    return {"id": "start", "type": "START", "label": "Conversation starts",
            "next": next_id, "actions": [], "position": _at(column, row)}


def _message(node_id, label, body, next_id="", *, column=1, row=0, buttons=None):
    return {
        "id": node_id, "type": "MESSAGE", "label": label, "next": next_id,
        "position": _at(column, row),
        "actions": [{"type": "SEND_MESSAGE", "body": body, "buttons": buttons or []}],
    }


def _question(node_id, label, body, save_to, buttons, routes, next_id="", *, column=1, row=0):
    return {
        "id": node_id, "type": "QUESTION", "label": label, "next": next_id,
        "position": _at(column, row), "routes": routes,
        "actions": [{"type": "ASK_QUESTION", "body": body, "save_to": save_to, "buttons": buttons}],
    }


def _ask(node_id, label, body, save_to, next_id="", *, column=1, row=0):
    """A question with no buttons -- the customer types the answer."""
    return _question(node_id, label, body, save_to, [], [], next_id, column=column, row=row)


def _ai(node_id, label, prompt, next_id="", *, column=1, row=0):
    return {
        "id": node_id, "type": "AI", "label": label, "next": next_id, "position": _at(column, row),
        "actions": [{"type": "AI_REPLY", "prompt": prompt, "knowledge_base_id": None}],
    }


def _knowledge(node_id, label, prompt, next_id="", *, column=1, row=0):
    return {
        "id": node_id, "type": "KNOWLEDGE", "label": label, "next": next_id, "position": _at(column, row),
        "actions": [{"type": "KNOWLEDGE_ANSWER", "prompt": prompt, "knowledge_base_id": None}],
    }


def _handoff(node_id, label, message, *, column=2, row=0):
    return {
        "id": node_id, "type": "HANDOFF", "label": label, "next": "", "position": _at(column, row),
        "actions": [{"type": "HANDOFF", "message": message, "reason": "RULE_ACTION"}],
    }


def _tag(node_id, label, tag, next_id="", *, column=1, row=0):
    return {
        "id": node_id, "type": "TAG", "label": label, "next": next_id, "position": _at(column, row),
        "actions": [{"type": "ADD_TAG", "tag": tag}],
    }


def _end(node_id, label, body="", *, column=3, row=0):
    actions = [{"type": "SEND_MESSAGE", "body": body, "buttons": []}] if body else []
    actions.append({"type": "END_CONVERSATION", "message": "", "resolution": "BOT"})
    return {"id": node_id, "type": "END", "label": label, "next": "", "position": _at(column, row),
            "actions": actions}


def _button(button_id, title):
    return {"type": "REPLY", "id": button_id, "title": title, "url": "", "phone": ""}


# ---------------------------------------------------------------------------
# The templates
# ---------------------------------------------------------------------------

def _welcome_bot():
    return [
        _start("greet"),
        _message("greet", "Welcome", "Hi {{first_name}}, thanks for messaging us! 👋", "menu", column=1),
        _question(
            "menu", "Main menu", "What would you like to do?", "topic",
            [_button("about", "About us"), _button("hours", "Opening hours"), _button("agent", "Talk to us")],
            [{"value": "about", "next": "about_us"},
             {"value": "hours", "next": "our_hours"},
             {"value": "agent", "next": "to_human"}],
            next_id="anything_else", column=2, row=1,
        ),
        _message("about_us", "About us",
                 "We have been helping customers since 2015. Tell us what you need and we will help.",
                 "anything_else", column=3, row=0),
        _message("our_hours", "Opening hours", "We are open Monday to Saturday, 9am to 6pm.",
                 "anything_else", column=3, row=1),
        _handoff("to_human", "Hand to a person", "Of course - connecting you with a colleague now.",
                 column=3, row=2),
        _message("anything_else", "Anything else", "Is there anything else I can help with?",
                 column=4, row=1),
    ]


def _lead_generation():
    return [
        _start("intro"),
        _message("intro", "Introduce", "Hi {{first_name}}! Let me take a few details so we can help properly.",
                 "ask_name", column=1),
        _ask("ask_name", "Ask their name", "What name should we use?", "lead_name", "ask_city", column=2),
        _ask("ask_city", "Ask their city", "Which city are you in?", "city", "ask_budget", column=3),
        _question(
            "ask_budget", "Ask their budget", "Roughly what budget did you have in mind?", "budget",
            [_button("under_5k", "Under 5,000"), _button("over_5k", "5,000+"), _button("unsure", "Not sure yet")],
            [{"value": "over_5k", "next": "tag_premium"}, {"value": "under_5k", "next": "tag_standard"}],
            next_id="tag_standard", column=4,
        ),
        _tag("tag_premium", "Tag as premium", "Premium lead", "handover", column=5, row=0),
        _tag("tag_standard", "Tag as lead", "Lead", "thanks", column=5, row=1),
        _handoff("handover", "Hand to sales", "Thank you - one of our advisers will call you shortly.",
                 column=6, row=0),
        _end("thanks", "Thank them", "Thanks {{lead_name}}! We will be in touch about options in {{city}}.",
             column=6, row=1),
    ]


def _product_inquiry():
    return [
        _start("which"),
        _question(
            "which", "Which product", "Which range are you interested in?", "product",
            [_button("shoes", "Shoes"), _button("shirts", "Shirts"), _button("other", "Something else")],
            [{"value": "shoes", "next": "shoes_info"},
             {"value": "shirts", "next": "shirts_info"},
             {"value": "other", "next": "ask_other"}],
            next_id="ask_other", column=1,
        ),
        _message("shoes_info", "Shoes", "Our shoes start at 1,499 and come in sizes 5-12.",
                 "anything_else", column=2, row=0),
        _message("shirts_info", "Shirts", "Our shirts start at 899 in cotton and linen.",
                 "anything_else", column=2, row=1),
        _ask("ask_other", "Ask what they want", "Tell me what you are looking for and I will find it.",
             "product_request", "anything_else", column=2, row=2),
        _question(
            "anything_else", "Buy or ask", "Would you like to order, or ask a question?", "next_step",
            [_button("order", "Place an order"), _button("question", "Ask a question")],
            [{"value": "order", "next": "to_sales"}, {"value": "question", "next": "answer_it"}],
            next_id="answer_it", column=3, row=1,
        ),
        _handoff("to_sales", "Hand to sales", "Great - passing you to someone who can take the order.",
                 column=4, row=0),
        _knowledge("answer_it", "Answer from your documents",
                   "Answer the customer's product question from the documents.", column=4, row=2),
    ]


def _customer_support():
    return [
        _start("triage"),
        _question(
            "triage", "What do they need", "Sorry you are having trouble. What is it about?", "issue",
            [_button("order", "An order"), _button("payment", "A payment"), _button("other", "Something else")],
            [{"value": "order", "next": "order_help"},
             {"value": "payment", "next": "to_agent"},
             {"value": "other", "next": "ask_detail"}],
            next_id="ask_detail", column=1,
        ),
        _ask("order_help", "Ask the order number", "Sure - what is your order number?", "order_id",
             "look_it_up", column=2, row=0),
        _knowledge("look_it_up", "Answer from your documents",
                   "Answer the customer's question about order {{order_id}} from the documents.",
                   "resolved", column=3, row=0),
        _ask("ask_detail", "Ask for detail", "Tell me a little more and I will point you the right way.",
             "issue_detail", "look_it_up", column=2, row=2),
        _question(
            "resolved", "Did that help", "Did that answer your question?", "was_resolved",
            [_button("yes", "Yes, thanks"), _button("no", "No, I need a person")],
            [{"value": "yes", "next": "close_it"}, {"value": "no", "next": "to_agent"}],
            next_id="to_agent", column=4, row=0,
        ),
        _end("close_it", "Close", "Glad that helped. Message us any time!", column=5, row=0),
        _handoff("to_agent", "Hand to support", "Let me get a colleague who can help with this.",
                 column=5, row=2),
    ]


def _faq_bot():
    return [
        _start("answer"),
        _knowledge("answer", "Answer from your documents",
                   "Answer the customer's question using the documents. If the documents do not "
                   "cover it, say so plainly.",
                   "helpful", column=1),
        _question(
            "helpful", "Was that useful", "Did that answer it?", "was_helpful",
            [_button("yes", "Yes"), _button("no", "No")],
            [{"value": "yes", "next": "done"}, {"value": "no", "next": "to_agent"}],
            next_id="to_agent", column=2,
        ),
        _end("done", "Close", "Happy to help!", column=3, row=0),
        _handoff("to_agent", "Hand to a person", "Let me find someone who can answer that properly.",
                 column=3, row=1),
    ]


def _appointment_booking():
    return [
        _start("intro"),
        _message("intro", "Offer to book", "Happy to book you in, {{first_name}}.", "ask_service", column=1),
        _question(
            "ask_service", "Which service", "Which appointment would you like?", "service",
            [_button("consult", "Consultation"), _button("followup", "Follow-up")],
            [{"value": "consult", "next": "ask_day"}, {"value": "followup", "next": "ask_day"}],
            next_id="ask_day", column=2,
        ),
        _ask("ask_day", "Which day", "Which day suits you? (for example: Monday 14th)", "preferred_day",
             "ask_time", column=3),
        _ask("ask_time", "What time", "And roughly what time?", "preferred_time", "confirm", column=4),
        _tag("confirm", "Tag as booking request", "Booking request", "to_team", column=5),
        _handoff("to_team", "Hand to the team",
                 "Thank you - we have your request for {{preferred_day}} at {{preferred_time}} and "
                 "will confirm shortly.",
                 column=6),
    ]


def _order_tracking():
    return [
        _start("ask_order"),
        _ask("ask_order", "Ask the order number", "Happy to check. What is your order number?", "order_id",
             "look_up", column=1),
        _knowledge("look_up", "Look it up",
                   "Answer the customer's question about order {{order_id}} from the documents.",
                   "still_stuck", column=2),
        _question(
            "still_stuck", "Anything else", "Does that cover it?", "was_resolved",
            [_button("yes", "Yes, thanks"), _button("no", "I still need help")],
            [{"value": "yes", "next": "done"}, {"value": "no", "next": "to_agent"}],
            next_id="to_agent", column=3,
        ),
        _end("done", "Close", "Thanks for your order!", column=4, row=0),
        _handoff("to_agent", "Hand to support", "Let me get a colleague to look into this order.",
                 column=4, row=1),
    ]


def _human_handoff():
    return [
        _start("check"),
        _question(
            "check", "Bot or person", "Would you like me to help, or shall I fetch a colleague?", "prefers",
            [_button("bot", "You can help"), _button("human", "A person, please")],
            [{"value": "bot", "next": "let_me_try"}, {"value": "human", "next": "to_agent"}],
            next_id="let_me_try", column=1,
        ),
        _ai("let_me_try", "Try to answer", "Answer the customer's question briefly and helpfully.",
            "did_it_work", column=2, row=0),
        _question(
            "did_it_work", "Did that work", "Did that help?", "was_helpful",
            [_button("yes", "Yes"), _button("no", "No")],
            [{"value": "yes", "next": "done"}, {"value": "no", "next": "to_agent"}],
            next_id="to_agent", column=3, row=0,
        ),
        _end("done", "Close", "Glad to help!", column=4, row=0),
        _handoff("to_agent", "Hand to a person", "Of course - connecting you now.", column=4, row=1),
    ]


def _ai_support_bot():
    return [
        _start("answer"),
        _ai("answer", "AI answers",
            "You are a support assistant. Answer briefly and accurately. If you are not sure, "
            "say so and offer to fetch a colleague.",
            "check", column=1),
        _question(
            "check", "Did that help", "Did that answer your question?", "was_helpful",
            [_button("yes", "Yes"), _button("no", "Not quite")],
            [{"value": "yes", "next": "done"}, {"value": "no", "next": "to_agent"}],
            next_id="to_agent", column=2,
        ),
        _end("done", "Close", "Any time!", column=3, row=0),
        _handoff("to_agent", "Hand to a person", "No problem - let me get a colleague.", column=3, row=1),
    ]


def _feedback_collection():
    return [
        _start("ask_score"),
        _question(
            "ask_score", "Ask for a rating", "How did we do today?", "rating",
            [_button("good", "Great"), _button("ok", "Okay"), _button("bad", "Not good")],
            [{"value": "good", "next": "thanks_good"},
             {"value": "ok", "next": "ask_why"},
             {"value": "bad", "next": "ask_why"}],
            next_id="ask_why", column=1,
        ),
        _message("thanks_good", "Thank them", "That is lovely to hear - thank you! 🙏", "done", column=2, row=0),
        _ask("ask_why", "Ask what to fix", "Thank you. What would have made it better?", "feedback",
             "tag_it", column=2, row=1),
        _tag("tag_it", "Tag the contact", "Gave feedback", "close_bad", column=3, row=1),
        _message("close_bad", "Acknowledge", "Thank you - we have passed this to the team.", "done",
                 column=4, row=1),
        _end("done", "Close", column=5, row=0),
    ]


#: The catalogue. ``bot`` carries the handful of chatbot settings a template
#: implies -- an FAQ bot wants AI on, a menu bot does not -- and is applied only
#: when creating a *new* bot from a template, never when dropping a template
#: into one that already exists.
CATALOGUE = [
    {
        "id": "welcome_bot", "name": "Welcome bot", "category": "Getting started",
        "description": "Greets the customer and offers a short menu.",
        "build": _welcome_bot, "bot": {"bot_type": "RULE_BASED", "ai_enabled": False},
    },
    {
        "id": "lead_generation", "name": "Lead generation", "category": "Sales",
        "description": "Collects name, city and budget, then tags and hands over.",
        "build": _lead_generation, "bot": {"bot_type": "RULE_BASED", "ai_enabled": False},
    },
    {
        "id": "product_inquiry", "name": "Product inquiry", "category": "Sales",
        "description": "Branches by product range and routes buyers to sales.",
        "build": _product_inquiry, "bot": {"bot_type": "HYBRID", "ai_enabled": True},
    },
    {
        "id": "customer_support", "name": "Customer support", "category": "Support",
        "description": "Triages the issue, answers from your documents, escalates when it cannot.",
        "build": _customer_support, "bot": {"bot_type": "HYBRID", "ai_enabled": True},
    },
    {
        "id": "faq_bot", "name": "FAQ bot", "category": "Support",
        "description": "Answers from your knowledge base and offers a person when it cannot.",
        "build": _faq_bot, "bot": {"bot_type": "HYBRID", "ai_enabled": True},
    },
    {
        "id": "appointment_booking", "name": "Appointment booking", "category": "Operations",
        "description": "Takes a service, a day and a time, then hands the request to your team.",
        "build": _appointment_booking, "bot": {"bot_type": "RULE_BASED", "ai_enabled": False},
    },
    {
        "id": "order_tracking", "name": "Order tracking", "category": "Operations",
        "description": "Takes an order number and answers from your documents.",
        "build": _order_tracking, "bot": {"bot_type": "HYBRID", "ai_enabled": True},
    },
    {
        "id": "human_handoff", "name": "Human handoff", "category": "Support",
        "description": "Lets the customer choose the bot or a person, and escalates cleanly.",
        "build": _human_handoff, "bot": {"bot_type": "HYBRID", "ai_enabled": True},
    },
    {
        "id": "ai_support_bot", "name": "AI support bot", "category": "Support",
        "description": "The AI answers, then checks whether it actually helped.",
        "build": _ai_support_bot, "bot": {"bot_type": "AI_BASED", "ai_enabled": True},
    },
    {
        "id": "feedback_collection", "name": "Feedback collection", "category": "Operations",
        "description": "Asks how it went and captures what to fix.",
        "build": _feedback_collection, "bot": {"bot_type": "RULE_BASED", "ai_enabled": False},
    },
]

_BY_ID = {entry["id"]: entry for entry in CATALOGUE}


def listing() -> list:
    """The catalogue as the picker shows it, without the builders."""
    return [
        {key: entry[key] for key in ("id", "name", "category", "description")}
        for entry in CATALOGUE
    ]


def get(template_id: str):
    return _BY_ID.get(str(template_id or "").strip())


def build_flow(template_id: str, *, tenant_id: int) -> dict:
    """
    One template's graph, validated exactly as a hand-drawn one would be.

    Running it through ``flow.validate`` rather than trusting this file is the
    point: a template is data, and data that reaches ``ChatbotVersion.flow``
    without passing the same gate as everything else is how an unrunnable graph
    gets published.
    """
    entry = get(template_id)
    if entry is None:
        raise KeyError(template_id)
    return flow_module.validate({"nodes": entry["build"]()}, tenant_id=tenant_id)
