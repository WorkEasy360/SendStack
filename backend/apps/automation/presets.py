"""
Automation without the word "automation".

The workflow builder in this app is powerful and most people will never use
it, because "TRIGGER -> CONDITION -> DELAY -> ACTION" is a diagram of how the
engine works, not a description of anything a shop owner wants. What they want
is a sentence: *follow up with people who don't reply*.

A preset is that sentence, plus the two or three questions that genuinely
cannot be guessed, plus the mapping onto the engine that already exists. It
creates an ordinary AutomationRule or FollowUpRule -- the same row the advanced
screen creates, editable there afterwards, obeying the same compliance gate,
send limits and cooldowns. Nothing here is a second engine, and nothing here
can send a message the normal path would have refused.

Presets deliberately only cover what the proven engines already do. "Message
everyone who went quiet for 30 days" is a campaign to a smart segment, not an
automation, and "wish them happy birthday" needs a date field nobody has
filled in -- shipping half an engine for either would be worse than not
offering them.
"""

from apps.automation.models import AutomationRule, FollowUpRule

#: Wait presets, in minutes. Offered as a dropdown rather than a number box
#: because "4320" is not a length of time anybody thinks in.
DELAY_CHOICES = (
    (30, "30 minutes"),
    (60, "1 hour"),
    (240, "4 hours"),
    (1440, "1 day"),
    (2880, "2 days"),
    (4320, "3 days"),
    (10080, "1 week"),
    (20160, "2 weeks"),
)


def _delay_field(default, label="How long should we wait?", help_text=""):
    return {
        "key": "delay_minutes",
        "label": label,
        "type": "choice",
        "default": default,
        "required": True,
        "help": help_text,
        "options": [{"value": minutes, "label": text} for minutes, text in DELAY_CHOICES],
    }


def _message_field(default, label="What should we send?"):
    return {
        "key": "message_body",
        "label": label,
        "type": "textarea",
        "default": default,
        "required": True,
        "help": "You can use {{name}} for the customer's name.",
    }


def _template_scope_field(label, help_text):
    return {
        "key": "trigger_templates",
        "label": label,
        "type": "templates",
        "default": [],
        "required": False,
        "help": help_text,
    }


_ACCOUNT_FIELD = {
    "key": "whatsapp_account",
    "label": "Which WhatsApp number?",
    "type": "account",
    "default": None,
    "required": True,
    "help": "",
}


PRESETS = {
    "knowledge_auto_reply": {
        "name": "Answer questions from my documents",
        "goal": "Upload your price list, FAQ and policies, and let customers ask about them on WhatsApp.",
        "why": "The same handful of questions arrive every day, and the answers are already written down.",
        "creates": "KNOWLEDGE",
        "icon": "BookOpen",
        "fields": [
            {
                "key": "knowledge_base",
                "label": "Which knowledge base should it answer from?",
                "type": "knowledge_base",
                "default": None,
                "required": True,
                "help": "Only documents in this base are ever used to answer.",
            },
            {
                "key": "reply_scope",
                "label": "When should it reply?",
                "type": "choice",
                "default": "UNASSIGNED",
                "required": True,
                "help": "",
                "options": [
                    {"value": "UNASSIGNED", "label": "Only when nobody on your team has the chat"},
                    {"value": "EVERY", "label": "Every incoming message"},
                    {"value": "OUTSIDE_HOURS", "label": "Only outside your business hours"},
                ],
            },
            {
                "key": "fallback_action",
                "label": "If the answer isn't in your documents",
                "type": "choice",
                "default": "HANDOFF",
                "required": True,
                "help": "It never guesses. This is what the customer gets instead.",
                "options": [
                    {"value": "HANDOFF", "label": "Hand the chat to a person"},
                    {"value": "SAY_UNKNOWN", "label": "Say we don't have that information"},
                    {"value": "ASK_SUPPORT", "label": "Ask them to contact support"},
                    {"value": "NOTIFY_TEAM", "label": "Tell them we'll get back to them, and notify the team"},
                ],
            },
        ],
    },
    "welcome_new_lead": {
        "name": "Welcome every new customer",
        "goal": "The first time somebody messages you, answer them straight away.",
        "why": "Most people decide whether a business is worth talking to in the first minute.",
        "creates": "WELCOME",
        "icon": "HandHeart",
        "fields": [
            _ACCOUNT_FIELD,
            _message_field(
                "Hi {{name}}! Thanks for getting in touch. Someone from our team will reply shortly. "
                "In the meantime, what can we help you with?",
                label="What should we say?",
            ),
        ],
    },
    "no_reply_follow_up": {
        "name": "Follow up when nobody replies",
        "goal": "Nudge people who went quiet after you messaged them.",
        "why": "Most sales are lost to silence, not to a no.",
        "creates": "FOLLOW_UP",
        "icon": "BellRing",
        "fields": [
            _ACCOUNT_FIELD,
            _delay_field(1440, "How long should we wait for a reply?"),
            _message_field("Hi {{name}}, just checking you saw my last message. Happy to help if you have questions!"),
            _template_scope_field(
                "Only after certain messages?",
                "Leave this empty to follow up after everything you send. Pick templates to keep this nudge "
                "tied to one campaign.",
            ),
        ],
    },
    "quote_follow_up": {
        "name": "Follow up after a quotation",
        "goal": "Three days after you send a quote, check whether they need anything.",
        "why": "A quote with no follow-up is a decision you left to somebody else.",
        "creates": "FOLLOW_UP",
        "icon": "FileText",
        "fields": [
            _ACCOUNT_FIELD,
            _template_scope_field(
                "Which message is the quotation?",
                "Pick the template you send quotes with, so this only fires after a quote.",
            ),
            _delay_field(4320),
            _message_field(
                "Hi {{name}}, did you get a chance to look at the quote we sent? "
                "Happy to go through it or adjust anything."
            ),
        ],
    },
    "demo_follow_up": {
        "name": "Follow up after a demo",
        "goal": "Check in the day after you show somebody the product.",
        "why": "Interest fades fast; the day after is when they still remember it.",
        "creates": "FOLLOW_UP",
        "icon": "MonitorPlay",
        "fields": [
            _ACCOUNT_FIELD,
            _template_scope_field("Which message follows the demo?", "Pick the template you send around demos."),
            _delay_field(1440),
            _message_field("Hi {{name}}, thanks for your time yesterday! Any questions after seeing it in action?"),
        ],
    },
    "payment_reminder": {
        "name": "Remind about a pending payment",
        "goal": "A polite nudge a few days after the invoice goes out.",
        "why": "Chasing payment is the job people put off; this one does not forget.",
        "creates": "FOLLOW_UP",
        "icon": "Receipt",
        "fields": [
            _ACCOUNT_FIELD,
            _template_scope_field("Which message is the invoice?", "Pick the template you send invoices with."),
            _delay_field(4320),
            _message_field(
                "Hi {{name}}, a gentle reminder about the pending payment. Let us know if you need the details again."
            ),
        ],
    },
    "post_purchase_check_in": {
        "name": "Check in after a purchase",
        "goal": "A week after they buy, ask how it is going.",
        "why": "The cheapest new sale is to somebody who already bought once.",
        "creates": "FOLLOW_UP",
        "icon": "PackageCheck",
        "fields": [
            _ACCOUNT_FIELD,
            _template_scope_field("Which message confirms the order?", "Pick your order-confirmation template."),
            _delay_field(10080),
            _message_field("Hi {{name}}, hope everything arrived well! Anything we can help with?"),
        ],
    },
    "review_request": {
        "name": "Ask for a review",
        "goal": "A few days after delivery, ask happy customers to say so.",
        "why": "Reviews come from asking; almost nobody volunteers one.",
        "creates": "FOLLOW_UP",
        "icon": "Star",
        "fields": [
            _ACCOUNT_FIELD,
            _template_scope_field("Which message goes out on delivery?", "Pick your delivery/completion template."),
            _delay_field(4320),
            _message_field(
                "Hi {{name}}, thanks for choosing us! If you have a minute, a quick review would mean a lot."
            ),
        ],
    },
    "price_question_reply": {
        "name": "Answer price questions instantly",
        "goal": "When somebody asks what it costs, reply immediately instead of hours later.",
        "why": "A price question is the strongest buying signal you get on WhatsApp.",
        "creates": "KEYWORD",
        "icon": "Tag",
        "fields": [
            _ACCOUNT_FIELD,
            {
                "key": "keyword",
                "label": "Which word should we watch for?",
                "type": "text",
                "default": "price",
                "required": True,
                "help": "One word or phrase. Matched anywhere in their message, ignoring case.",
            },
            _message_field(
                "Hi {{name}}! Happy to share pricing. Could you tell me roughly what quantity you need "
                "so I can send the right rate?",
                label="What should we reply?",
            ),
        ],
    },
    "catalogue_request": {
        "name": "Send the catalogue on request",
        "goal": "Reply with your catalogue whenever somebody asks for it.",
        "why": "It is the same answer every time, and it should not wait for an agent.",
        "creates": "KEYWORD",
        "icon": "BookOpen",
        "fields": [
            _ACCOUNT_FIELD,
            {
                "key": "keyword",
                "label": "Which word should we watch for?",
                "type": "text",
                "default": "catalogue",
                "required": True,
                "help": "One word or phrase. Matched anywhere in their message, ignoring case.",
            },
            _message_field(
                "Hi {{name}}! Here is our latest catalogue. Let me know if anything catches your eye.",
                label="What should we reply?",
            ),
        ],
    },
}


def catalog() -> list:
    """Every preset, in the order they are offered."""
    return [{"key": key, **{field: value for field, value in preset.items()}} for key, preset in PRESETS.items()]


def get(key):
    return PRESETS.get(key)


class PresetError(Exception):
    """Raised when a preset cannot be turned into a rule from these answers."""


def activate(key, *, tenant, account, answers: dict, actor=None):
    """
    Build the real rule this preset describes.

    Returns ``(instance, kind)`` where kind is "RULE" or "FOLLOW_UP", so the
    caller knows which serializer and which page the new row belongs to.
    Everything created here is an ordinary row: the advanced screens list it,
    edit it and delete it with no idea a preset was involved.
    """
    preset = get(key)
    if preset is None:
        raise PresetError("Unknown automation preset.")

    creates = preset["creates"]

    # The knowledge-base preset configures the agent rather than writing a
    # rule: the thing that answers is already built, and what the user is
    # choosing is which documents it may use and what it does when they do
    # not cover the question.
    if creates == "KNOWLEDGE":
        return _activate_knowledge(tenant, answers), "KNOWLEDGE"

    body = (answers.get("message_body") or "").strip()
    if not body:
        raise PresetError("Write the message this automation should send.")

    name = (answers.get("name") or preset["name"]).strip()[:150]

    if creates in ("WELCOME", "KEYWORD"):
        keyword = (answers.get("keyword") or "").strip()
        if creates == "KEYWORD" and not keyword:
            raise PresetError("Choose the word this automation should watch for.")
        rule = AutomationRule.objects.create(
            tenant=tenant, whatsapp_account=account, name=name,
            trigger_type=(
                AutomationRule.Trigger.WELCOME if creates == "WELCOME" else AutomationRule.Trigger.KEYWORD
            ),
            keyword=keyword,
            keyword_match_type=AutomationRule.MatchType.CONTAINS,
            message_type=AutomationRule.MessageType.TEXT,
            message_body=body,
            # A keyword rule with no cooldown answers a customer who types the
            # same word five times five identical times. One hour is long
            # enough to read as a person and short enough not to swallow a
            # genuine second question the next day.
            cooldown_seconds=3600 if creates == "KEYWORD" else 0,
            is_active=True,
        )
        return rule, "RULE"

    if creates == "FOLLOW_UP":
        try:
            delay = int(answers.get("delay_minutes") or 1440)
        except (TypeError, ValueError):
            raise PresetError("Choose how long to wait.") from None
        if delay < 1:
            raise PresetError("Choose how long to wait.")

        rule = FollowUpRule.objects.create(
            tenant=tenant, whatsapp_account=account, name=name,
            delay_minutes=delay,
            message_type=FollowUpRule.MessageType.TEXT,
            message_body=body,
            is_active=True,
        )
        template_ids = answers.get("trigger_templates") or []
        if template_ids:
            from apps.templates_app.models import MessageTemplate

            templates = list(MessageTemplate.objects.filter(tenant=tenant, pk__in=template_ids))
            if len(templates) != len({int(t) for t in template_ids}):
                rule.delete()
                raise PresetError("Those templates do not all belong to you.")
            rule.trigger_templates.set(templates)
        return rule, "FOLLOW_UP"

    raise PresetError("This preset cannot be activated yet.")


def _activate_knowledge(tenant, answers: dict):
    """
    Point the AI agent at a knowledge base and switch it on.

    Refuses when the base has nothing readable in it: switching on an agent
    with an empty knowledge base means every customer gets the fallback, which
    looks exactly like the feature being broken.
    """
    from apps.ai.models import AIAgentConfig, KnowledgeBase, KnowledgeDocument

    base = KnowledgeBase.objects.filter(pk=answers.get("knowledge_base"), tenant=tenant).first()
    if base is None:
        raise PresetError("Choose one of your own knowledge bases.")

    ready = KnowledgeDocument.objects.filter(
        knowledge_base=base, is_active=True, status=KnowledgeDocument.Status.READY,
    ).exists()
    if not ready:
        raise PresetError(
            f'"{base.name}" has no readable documents yet. Upload one and wait for it to finish processing.'
        )

    scope = answers.get("reply_scope") or AIAgentConfig.ReplyScope.UNASSIGNED
    if scope not in AIAgentConfig.ReplyScope.values:
        raise PresetError("Choose when the assistant should reply.")

    fallback = answers.get("fallback_action") or AIAgentConfig.Fallback.HANDOFF
    if fallback not in AIAgentConfig.Fallback.values:
        raise PresetError("Choose what should happen when the answer is not in your documents.")

    config, _ = AIAgentConfig.objects.get_or_create(tenant=tenant)
    config.knowledge_base = base
    config.reply_scope = scope
    config.fallback_action = fallback
    # Non-negotiable for this preset: the whole promise is "answers from your
    # documents", and an agent allowed to answer from general knowledge is a
    # different, riskier product wearing the same switch.
    config.answer_from_knowledge_only = True
    config.is_enabled = True
    config.save(update_fields=[
        "knowledge_base", "reply_scope", "fallback_action",
        "answer_from_knowledge_only", "is_enabled", "updated_at",
    ])
    return config
