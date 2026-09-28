"""
The chatbot's background work.

Nothing expensive happens in the webhook. The inbound request validates,
normalises, persists and queues -- and every model call, every retrieval and
every provider send happens here, on a worker, where taking four seconds costs
nobody a timeout.
"""

import logging

from config.celery import app
from core.backoff import retry_delay

logger = logging.getLogger("sendstack")


#: How many times a message waits for the conversation ahead of it to finish.
#: Generous, because each wait is short and the alternative is dropping a
#: customer's second message -- and a burst of five messages in one breath is
#: an ordinary thing for a person to do.
MAX_BUSY_RETRIES = 10

#: How long to wait before trying again. Short: the turn ahead is usually a
#: rule match taking milliseconds, and only occasionally a model call.
BUSY_RETRY_SECONDS = 3


@app.task(bind=True, max_retries=MAX_BUSY_RETRIES)
def process_inbound_message_task(self, message_id, correlation_id=""):
    """
    Run one inbound message through the chatbot engine.

    Retried, and retrying is *safe* rather than merely tolerated: the engine
    claims the message under a row lock and refuses to answer one it has
    already answered, so a retry after a database blip resumes rather than
    duplicates. That property is the reason this task can be retried at all --
    without it, a retry would be a second reply on somebody's phone.

    Two kinds of retry, deliberately told apart:

    * **Busy** -- another message on the same conversation is mid-turn. Common,
      cheap, and not a failure: somebody typed twice. Retried soon and often,
      which is what turns "three messages in a breath" into three answers in
      order rather than three racing workers.
    * **Anything else** -- a real error. Retried twice with backoff, then given
      up on, because a message nobody can process is not improved by a fourth
      attempt.
    """
    from . import engine

    try:
        return engine.handle(message_id, correlation_id=correlation_id)
    except engine.Busy as exc:
        if self.request.retries < MAX_BUSY_RETRIES:
            raise self.retry(exc=exc, countdown=BUSY_RETRY_SECONDS)
        logger.warning(
            "Message %s waited out its turn budget behind other messages on the same conversation.",
            message_id,
        )
        return "BUSY"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Chatbot processing crashed for message %s.", message_id)
        if self.request.retries < 2:
            raise self.retry(exc=exc, countdown=retry_delay(self.request.retries, base=15))
        return "ERROR"


@app.task
def deliver_delayed_reply_task(session_id, token, spec, reference="", correlation_id=""):
    """
    Send a reply that was composed a few seconds ago.

    The token is the whole point. A reply is scheduled with the session's
    delivery token as it stood at the time; anything that should cancel it -- an
    agent taking over, the conversation closing, the customer sending another
    message -- bumps that token. A stale token here means the answer is no
    longer wanted, and the right thing to do is nothing.

    Deliberately not a Celery revoke: revocation is advisory, a worker that has
    already been handed the task will run it regardless, and "the message went
    out after the agent took over" is exactly the failure that must not happen.
    """
    from .models import ChatbotEvent, ChatbotSession
    from .trace import Trace

    session = (
        ChatbotSession.objects
        .select_related("conversation", "conversation__contact", "conversation__whatsapp_account", "chatbot", "version")
        .filter(pk=session_id)
        .first()
    )
    if session is None:
        return "GONE"

    trace = Trace(
        tenant_id=session.tenant_id, conversation_id=session.conversation_id,
        chatbot_id=session.chatbot_id, correlation_id=correlation_id or None,
    )
    try:
        if session.delivery_token != token:
            trace.skipped(
                ChatbotEvent.Step.SEND,
                "Cancelled -- the conversation moved on before this reply was due.",
                scheduled_token=token, current_token=session.delivery_token,
            )
            return "CANCELLED"

        if not session.bot_may_speak:
            trace.skipped(
                ChatbotEvent.Step.SEND,
                f"Cancelled -- the conversation is now {session.get_state_display().lower()}.",
                state=session.state,
            )
            return "CANCELLED"

        from . import engine, outbound, services

        conversation = session.conversation
        problem = outbound.account_problem(conversation.whatsapp_account)
        if problem:
            trace.failed(ChatbotEvent.Step.SEND, problem)
            return "BLOCKED"

        # A minimal context: this is a send, not a decision, so nothing that
        # needs the inbound message is touched. `_DeliveryContext` exists
        # because `outbound.send_now` reasonably expects the same shape the
        # engine gives it.
        ctx = _DeliveryContext(session, trace)
        message = outbound.send_now(ctx, spec, reference=reference or f"chatbot:delayed:{session.pk}")
        return "SENT" if message is not None else "FAILED"
    finally:
        trace.flush()


class _DeliveryContext:
    """
    The attributes the outbound layer reads, for a send with no turn behind it.

    Only ``send_now`` is called on this today -- the pause has already been
    served by the time this task runs, so delaying it again would be wrong.
    ``extra_delay`` and ``chatbot_settings`` are carried anyway because
    ``__slots__`` turns "somebody later routes this through ``deliver``" from a
    wrong answer into an AttributeError in production.
    """

    __slots__ = (
        "session", "conversation", "contact", "account", "chatbot", "chatbot_settings",
        "trace", "replies_sent", "spoke", "extra_delay",
    )

    def __init__(self, session, trace):
        from . import services

        self.session = session
        self.conversation = session.conversation
        self.contact = session.conversation.contact
        self.account = session.conversation.whatsapp_account
        self.chatbot = session.chatbot
        self.chatbot_settings = services.settings_of(session.version, session.chatbot)
        self.trace = trace
        self.replies_sent = 0
        self.spoke = False
        self.extra_delay = 0


@app.task
def summarise_session_task(session_id):
    """
    Compress the older half of a long thread into a few lines.

    Queued after the customer's answer has already gone out: nobody is waiting
    on this, and it must never add a model call's latency to the reply that
    triggered it.
    """
    from . import ai_reply
    from .models import ChatbotSession

    session = ChatbotSession.objects.select_related("conversation", "chatbot").filter(pk=session_id).first()
    if session is None:
        return ""
    return ai_reply.summarise(session)


@app.task
def expire_idle_sessions_task():
    """
    Close conversations nobody has touched in a long time.

    Run from beat. Without it, a workspace's "active conversations" figure only
    ever grows: somebody who asked one question in March is not an open
    conversation in September, and counting them as one makes the whole
    overview useless.

    Marked ABANDONED rather than RESOLVED, because nobody resolved them.
    """
    from django.utils import timezone

    from .models import Chatbot, ChatbotSession

    closed = 0
    live_states = [
        ChatbotSession.State.NEW, ChatbotSession.State.BOT_ACTIVE,
        ChatbotSession.State.WAITING_FOR_CUSTOMER,
    ]
    # Per bot, because the timeout is a per-bot setting. Only bots that have a
    # timeout configured are considered at all.
    for chatbot in Chatbot.objects.filter(conversation_timeout_minutes__gt=0).only("pk", "conversation_timeout_minutes"):
        cutoff = timezone.now() - timezone.timedelta(minutes=chatbot.conversation_timeout_minutes)
        closed += ChatbotSession.objects.filter(
            chatbot=chatbot, state__in=live_states, updated_at__lt=cutoff,
        ).update(
            state=ChatbotSession.State.CLOSED,
            resolution=ChatbotSession.Resolution.ABANDONED,
            ended_at=timezone.now(),
        )
    if closed:
        logger.info("Closed %s idle chatbot sessions.", closed)
    return closed


@app.task
def prune_chatbot_events_task(days=None):
    """
    Delete debug trace older than the retention window.

    This is the fastest-growing table in the feature -- roughly a dozen rows per
    inbound message -- and it exists to answer "why did that happen?", a
    question nobody asks about last quarter. Deleted in batches so a first run
    on a large table does not hold one transaction open across millions of rows.
    """
    from django.conf import settings
    from django.utils import timezone

    from .models import ChatbotEvent

    window = int(days if days is not None else getattr(settings, "CHATBOT_EVENT_RETENTION_DAYS", 30))
    if window <= 0:
        return 0

    cutoff = timezone.now() - timezone.timedelta(days=window)
    removed = 0
    while True:
        batch = list(
            ChatbotEvent.objects.filter(created_at__lt=cutoff).values_list("pk", flat=True)[:5000]
        )
        if not batch:
            break
        removed += ChatbotEvent.objects.filter(pk__in=batch).delete()[0]
    if removed:
        logger.info("Pruned %s chatbot trace rows older than %s days.", removed, window)
    return removed
