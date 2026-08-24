"""
"42 leads haven't replied in 3 days. Create a follow-up automation?"

Every recommendation here is a counted fact about this tenant's own data plus
a gap in their setup. There is no scoring, no model and no judgment: if the
number is over the floor and the rule that would handle it does not exist, it
is offered.

Two rules make this safe to show:

* **Nothing here sends anything.** A recommendation is a link to a form the
  user still has to fill in and submit. The system noticing an opportunity and
  the system acting on it are different products, and only one of them is
  safe to ship without asking.
* **It stays quiet when it has nothing to say.** Each check has a floor below
  which it does not appear, so a new account is not greeted with six
  suggestions about eleven contacts.
"""

from datetime import timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import AutomationRule, FollowUpRule

#: Below these counts a suggestion is noise rather than an opportunity.
MIN_QUIET_LEADS = 5
MIN_NEW_CONVERSATIONS = 3
MIN_PRICE_QUESTIONS = 3
MIN_UNASSIGNED = 5
MIN_DEALS_WITHOUT_TASK = 3
MIN_COLD_LEADS = 10

#: What counts as somebody asking about money. Deliberately short: a false
#: positive here costs a suggestion nobody takes, and a long list of near-
#: synonyms starts matching ordinary conversation.
PRICE_WORDS = ("price", "cost", "rate", "quote", "quotation", "charges", "kitna", "kitne")


def _has_active(model, tenant, **filters) -> bool:
    return model.objects.filter(tenant=tenant, is_active=True, **filters).exists()


def build(tenant, *, user=None) -> list:
    from apps.contacts.models import Contact
    from apps.crm import segments as crm_segments
    from apps.crm.models import Deal, Task
    from apps.inbox.models import Conversation
    from apps.messaging.models import Message

    now = timezone.now()
    out = []

    # 1. People went quiet and nothing chases them.
    quiet = crm_segments.resolve(tenant, "no_reply_3d").count()
    if quiet >= MIN_QUIET_LEADS and not _has_active(FollowUpRule, tenant):
        out.append({
            "key": "no_reply_follow_up",
            "title": f"{quiet} contacts haven't replied in 3 days",
            "detail": "Nothing follows up with them right now. A single nudge recovers more of these than anything else you can set up.",
            "count": quiet,
            "action": "preset",
            "preset": "no_reply_follow_up",
            "action_label": "Set up a follow-up",
            "tone": "amber",
        })

    # 2. New conversations arriving with no welcome message.
    new_conversations = Conversation.objects.filter(tenant=tenant, created_at__gte=now - timedelta(days=7)).count()
    if new_conversations >= MIN_NEW_CONVERSATIONS and not _has_active(
        AutomationRule, tenant, trigger_type=AutomationRule.Trigger.WELCOME
    ):
        out.append({
            "key": "welcome_new_lead",
            "title": f"{new_conversations} new people messaged you this week",
            "detail": "None of them got an instant reply. A welcome message answers in seconds instead of hours.",
            "count": new_conversations,
            "action": "preset",
            "preset": "welcome_new_lead",
            "action_label": "Set up a welcome message",
            "tone": "blue",
        })

    # 3. Price questions arriving with no keyword rule to answer them.
    price_filter = Q()
    for word in PRICE_WORDS:
        price_filter |= Q(body__icontains=word)
    price_questions = Message.objects.filter(
        tenant=tenant, direction=Message.Direction.INBOUND, queued_at__gte=now - timedelta(days=14),
    ).filter(price_filter).count()
    if price_questions >= MIN_PRICE_QUESTIONS:
        answered = AutomationRule.objects.filter(
            tenant=tenant, is_active=True, trigger_type=AutomationRule.Trigger.KEYWORD,
        ).filter(Q(keyword__icontains="price") | Q(keyword__icontains="cost") | Q(keyword__icontains="rate")).exists()
        if not answered:
            out.append({
                "key": "price_question_reply",
                "title": f"{price_questions} people asked about pricing",
                "detail": "A price question is the strongest buying signal on WhatsApp, and none of these got an instant answer.",
                "count": price_questions,
                "action": "preset",
                "preset": "price_question_reply",
                "action_label": "Answer price questions",
                "tone": "green",
            })

    # 4. Leads arriving with nobody owning them.
    unassigned = Contact.objects.filter(
        tenant=tenant, owner__isnull=True, created_at__gte=now - timedelta(days=14),
    ).exclude(lifecycle_status=Contact.Lifecycle.INACTIVE).count()
    if unassigned >= MIN_UNASSIGNED:
        out.append({
            "key": "assign_owners",
            "title": f"{unassigned} new contacts have no owner",
            "detail": "Nobody is responsible for following these up, so nobody will.",
            "count": unassigned,
            "action": "link",
            "link": "/contacts?smart=unassigned",
            "action_label": "Assign them",
            "tone": "gray",
        })

    # 5. Open deals with nothing scheduled on them.
    open_task = Task.objects.filter(deal_id=OuterRef("pk"), status=Task.Status.OPEN)
    idle_deals = Deal.objects.filter(tenant=tenant, status=Deal.Status.OPEN).filter(~Exists(open_task)).count()
    if idle_deals >= MIN_DEALS_WITHOUT_TASK:
        out.append({
            "key": "deals_without_next_step",
            "title": f"{idle_deals} open deals have no next step",
            "detail": "A deal with nothing scheduled against it is a deal that quietly stops moving.",
            "count": idle_deals,
            "action": "link",
            "link": "/pipeline",
            "action_label": "Review the pipeline",
            "tone": "violet",
        })

    # 6. A cold list worth one re-engagement campaign.
    cold = crm_segments.resolve(tenant, "no_reply_7d").count()
    if cold >= MIN_COLD_LEADS:
        out.append({
            "key": "reengage_cold",
            "title": f"{cold} leads have gone cold",
            "detail": "A week of silence. One re-engagement message to the whole group is usually worth more than chasing them one at a time.",
            "count": cold,
            "action": "link",
            "link": "/contacts?smart=no_reply_7d",
            "action_label": "Message them",
            "tone": "amber",
        })

    return out
