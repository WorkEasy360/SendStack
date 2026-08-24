"""
The call engine: what happens, in what order, on a live phone call.

Each function here answers one carrier webhook by deciding what should happen
next and returning it as provider-neutral directives (see
``providers.base``). Nothing in this module knows what TwiML is.

Two rules shape the whole file. The first is that a call is *live*: there is a
person holding a phone, and every failure path has to end in something being
said to them rather than in an exception. So a model outage becomes "sorry,
could you say that again", a tool crash becomes a tool result, and an
unrecognised state becomes a polite goodbye -- never a 500, which the carrier
would turn into a dropped call. The second is that the transcript is written
as the call happens, because a call that drops half way through must still
leave behind everything that was said before it dropped.
"""

import logging

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from apps.ai.providers.anthropic import AIProviderError

from . import agent, tools
from .models import CallSession, CallTurn, VoiceAgentConfig
from .providers.base import Hangup, Listen, Pause, Speak, Transfer, VoiceProviderError
from .providers.registry import get_provider

logger = logging.getLogger("multisender")

# Said when the model could not be reached at all. Deliberately a request to
# repeat rather than an apology for a technical fault: it keeps the call alive
# and usually gets a second, cleaner utterance out of the person.
RECOVERY_LINE = "Sorry, I didn't catch that. Could you say it again?"
SILENCE_LINE = "Are you still there?"
GOODBYE_LINE = "Thanks for your time. Goodbye."


class VoiceNotConfigured(Exception):
    """Raised when a call is asked for that could not possibly be placed."""


# ---------------------------------------------------------------------------
# Configuration and URLs
# ---------------------------------------------------------------------------

def is_configured(config=None, tenant_id=None) -> bool:
    config = config or VoiceAgentConfig.for_tenant(tenant_id)
    return bool(config.is_enabled and config.has_carrier_credentials and agent.api_key(config))


def public_base_url() -> str:
    """
    The origin the carrier will call us back on.

    Distinct from BACKEND_PUBLIC_URL, which under docker-compose is the
    internal http://backend:8000 -- a name that only resolves inside the
    Docker network and that Twilio therefore cannot reach. A tunnel or public
    hostname goes in VOICE_PUBLIC_BASE_URL.
    """
    base = (getattr(settings, "VOICE_PUBLIC_BASE_URL", "") or "").strip()
    if not base:
        base = (getattr(settings, "BACKEND_PUBLIC_URL", "") or "").strip()
    return base.rstrip("/")


def callback_url(session, kind: str) -> str:
    """
    One of this call's webhook URLs.

    The unguessable token is in the path rather than the query string because
    the carrier's signature covers the URL exactly as given -- keeping the
    query string empty removes an entire class of signature mismatch caused by
    proxies reordering parameters.
    """
    return f"{public_base_url()}/voice/{kind}/{session.webhook_token}/"


# ---------------------------------------------------------------------------
# Starting a call
# ---------------------------------------------------------------------------

def calls_placed_today(tenant_id) -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return CallSession.objects.filter(tenant_id=tenant_id, created_at__gte=start).count()


def start_call(tenant, to_number: str, *, goal="", context=None, contact=None, created_by=None,
               config=None) -> CallSession:
    """
    Create the call and hand it to the worker to dial.

    The row exists before the carrier is told anything, because the webhook
    URL the carrier is given has to resolve to something. A session sitting in
    QUEUED with no provider_call_id is therefore a call that was never placed.
    """
    config = config or VoiceAgentConfig.for_tenant(tenant.pk if hasattr(tenant, "pk") else tenant)

    if not config.is_enabled:
        raise VoiceNotConfigured("The voice agent is switched off.")
    if not config.has_carrier_credentials:
        raise VoiceNotConfigured("The voice agent has no carrier credentials.")
    if not agent.api_key(config):
        raise VoiceNotConfigured("The voice agent has no model API key.")
    if not public_base_url():
        raise VoiceNotConfigured(
            "VOICE_PUBLIC_BASE_URL is not set, so the carrier has no address to call back on."
        )

    tenant_id = tenant.pk if hasattr(tenant, "pk") else tenant
    if config.daily_call_limit and calls_placed_today(tenant_id) >= config.daily_call_limit:
        raise VoiceNotConfigured(f"The daily limit of {config.daily_call_limit} calls has been reached.")

    session = CallSession.objects.create(
        tenant_id=tenant_id,
        contact=contact,
        created_by=created_by,
        to_number=to_number,
        from_number=config.from_number,
        provider=config.provider,
        goal=goal or "",
        context=context or {},
    )

    from .tasks import place_call_task

    place_call_task.delay(session.pk)
    return session


def start_inbound_call(config, *, caller_number: str, provider_call_id: str) -> CallSession:
    """
    Somebody rang the tenant's number.

    `to_number` is the person on the other end, not the number they dialled --
    it is the field every other part of this app treats as "who we are talking
    to", and the WhatsApp follow-up tool sends there.
    """
    contact = _contact_for(config.tenant_id, caller_number)
    return CallSession.objects.create(
        tenant_id=config.tenant_id,
        contact=contact,
        direction=CallSession.Direction.INBOUND,
        provider=config.provider,
        provider_call_id=provider_call_id,
        to_number=caller_number,
        from_number=config.from_number,
        goal=config.default_goal or "",
        status=CallSession.Status.IN_PROGRESS,
        started_at=timezone.now(),
    )


def _contact_for(tenant_id, phone: str):
    """
    The contact this number belongs to, created if they are new.

    A caller with no record is still worth keeping -- the transcript and
    anything the agent captures need somewhere to live. Creation is attributed
    to the tenant's earliest user because `Contact.created_by` cannot be null;
    where a tenant somehow has no users at all the call simply runs without a
    contact rather than failing.
    """
    from apps.contacts.models import Contact

    contact = Contact.objects.filter(tenant_id=tenant_id, phone=phone).first()
    if contact is not None:
        return contact

    from django.contrib.auth import get_user_model

    owner = get_user_model().objects.filter(tenant_id=tenant_id).order_by("pk").first()
    if owner is None:
        return None

    contact, _ = Contact.objects.get_or_create(
        tenant_id=tenant_id, phone=phone,
        defaults={"created_by": owner, "source": "voice"},
    )
    return contact


def place_call(session) -> None:
    """Tell the carrier to dial. Called from the worker, never from a request."""
    config = VoiceAgentConfig.for_tenant(session.tenant_id)
    provider = get_provider(config)

    try:
        handle = provider.place_call(
            config, session,
            answer_url=callback_url(session, "answer"),
            status_url=callback_url(session, "status"),
        )
    except VoiceProviderError as exc:
        # An ambiguous failure may already be ringing somebody's phone. It is
        # recorded as failed but must never be retried, because the cost of
        # being wrong is calling a person twice.
        session.status = CallSession.Status.FAILED
        session.outcome = CallSession.Outcome.ERROR
        session.error = str(exc)[:500]
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "outcome", "error", "ended_at"])
        logger.warning("Could not place call %s: %s", session.pk, exc)
        return

    session.provider_call_id = handle.provider_call_id
    session.status = CallSession.Status.RINGING
    session.started_at = timezone.now()
    session.save(update_fields=["provider_call_id", "status", "started_at"])


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

def record(session, role, text, *, confidence=None, latency_ms=0) -> CallTurn:
    """Append to the transcript. The ordinal is the call's own running count."""
    ordinal = CallTurn.objects.filter(session=session).count()
    return CallTurn.objects.create(
        session=session, ordinal=ordinal, role=role, text=(text or "")[:8000],
        confidence=confidence, latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------

def on_answer(session, turn) -> list:
    """
    The call connected. Greet, or leave a message and go.

    `turn.answered_by` is only populated when the tenant configured a voicemail
    message, because answering-machine detection costs several seconds of dead
    air on every call including the ones a human picks up.
    """
    config = VoiceAgentConfig.for_tenant(session.tenant_id)

    if session.answered_at is None:
        session.answered_at = timezone.now()
        session.status = CallSession.Status.IN_PROGRESS
        session.save(update_fields=["answered_at", "status"])

    if turn.answered_by.startswith(("machine", "fax")):
        message = config.voicemail_message.strip()
        record(session, CallTurn.Role.SYSTEM, f"Answered by {turn.answered_by}.")
        finish(session, CallSession.Outcome.VOICEMAIL)
        if not message:
            return [Hangup()]
        record(session, CallTurn.Role.AGENT, message)
        # A beat first: machines cut off whatever is said in the moment the
        # beep ends, so the first words would otherwise be the missing ones.
        return [Pause(seconds=1), Hangup(lines=[message])]

    greeting = _greeting(session, config)
    record(session, CallTurn.Role.AGENT, greeting)
    return [_listen(session, config, [greeting])]


def _greeting(session, config) -> str:
    """
    How the agent opens.

    An inbound caller has already chosen to ring, so "how can I help" is
    right; an outbound call has to explain itself before anything else, which
    is a different sentence and a different setting.
    """
    if session.direction == CallSession.Direction.INBOUND and config.inbound_greeting.strip():
        return config.inbound_greeting.strip()
    return config.greeting.strip() or "Hello."


def on_turn(session, turn) -> list:
    """One exchange: they said something, the agent answers."""
    config = VoiceAgentConfig.for_tenant(session.tenant_id)

    if not turn.speech:
        return _on_silence(session, config)

    session.silent_turns = 0
    session.turns_used = F("turns_used") + 1
    session.save(update_fields=["silent_turns", "turns_used"])
    session.refresh_from_db(fields=["turns_used"])

    record(session, CallTurn.Role.CALLER, turn.speech, confidence=turn.confidence)

    limit_line = _limit_reached(session, config)
    if limit_line:
        record(session, CallTurn.Role.AGENT, limit_line)
        finish(session, CallSession.Outcome.LIMIT_REACHED)
        return [Hangup(lines=[limit_line])]

    try:
        result = agent.run_turn(config, session, turn.speech)
    except AIProviderError as exc:
        # The person is still holding the phone. Keep the call alive and ask
        # them to repeat themselves -- by the time a retry would have run they
        # will have said it again anyway.
        logger.warning("Model call failed on call %s: %s", session.pk, exc)
        record(session, CallTurn.Role.SYSTEM, f"Model error: {exc}"[:500])
        record(session, CallTurn.Role.AGENT, RECOVERY_LINE)
        return [_listen(session, config, [RECOVERY_LINE])]

    CallSession.objects.filter(pk=session.pk).update(
        input_tokens=F("input_tokens") + result.input_tokens,
        output_tokens=F("output_tokens") + result.output_tokens,
    )
    for name, outcome in result.tool_log:
        record(session, CallTurn.Role.TOOL, f"{name}: {outcome}"[:2000])

    speech = result.speech or RECOVERY_LINE
    record(session, CallTurn.Role.AGENT, speech, latency_ms=result.latency_ms)

    if result.terminal == tools.TRANSFER:
        finish(session, CallSession.Outcome.TRANSFERRED)
        return [Transfer(
            number=config.transfer_number,
            caller_id=config.from_number,
            timeout_seconds=getattr(settings, "VOICE_TRANSFER_TIMEOUT_SECONDS", 30),
            lines=[speech],
        )]

    if result.terminal == tools.END_CALL:
        finish(session, CallSession.Outcome.AGENT_ENDED)
        return [Hangup(lines=[speech])]

    return [_listen(session, config, [speech])]


def _on_silence(session, config) -> list:
    """
    Nobody spoke. Ask once, then stop.

    Silence is the normal shape of a voicemail nobody configured a message
    for, and of a call somebody put down without hanging up. Re-prompting for
    ever would hold the line open and bill the tenant for it.
    """
    session.silent_turns = F("silent_turns") + 1
    session.save(update_fields=["silent_turns"])
    session.refresh_from_db(fields=["silent_turns"])

    if session.silent_turns > max(config.max_silent_turns, 1):
        record(session, CallTurn.Role.SYSTEM, "No speech detected; ending the call.")
        finish(session, CallSession.Outcome.NO_SPEECH)
        return [Hangup(lines=[GOODBYE_LINE])]

    record(session, CallTurn.Role.AGENT, SILENCE_LINE)
    return [_listen(session, config, [SILENCE_LINE])]


def _limit_reached(session, config) -> str:
    """The closing line when a ceiling is hit, or "" when there is room left."""
    if config.max_turns and session.turns_used > config.max_turns:
        return "I've taken enough of your time for now. Someone will follow up with you. Goodbye."

    if config.max_call_seconds and session.answered_at:
        elapsed = (timezone.now() - session.answered_at).total_seconds()
        if elapsed >= config.max_call_seconds:
            return "I need to let you go now, but someone will follow up with you. Goodbye."
    return ""


def _listen(session, config, lines) -> Listen:
    return Listen(
        action_url=callback_url(session, "turn"),
        lines=lines,
        timeout_seconds=config.silence_timeout_seconds,
        language=config.language,
        hints=config.speech_hints.strip(),
    )


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------

def finish(session, outcome) -> None:
    """
    Record why the call ended.

    Only the first outcome sticks. The agent deciding to hang up and the
    carrier reporting the line closed are the same event seen twice, and the
    agent's reason is the one worth keeping.
    """
    if session.outcome:
        return
    session.outcome = outcome
    session.save(update_fields=["outcome"])


def on_status(session, update) -> None:
    """A lifecycle webhook: ringing, answered, finished, recorded."""
    if update.recording_url and not session.recording_url:
        session.recording_url = update.recording_url
        session.save(update_fields=["recording_url"])

    if not update.status:
        return

    fields = ["status"]
    session.status = update.status

    if update.duration_seconds and not session.duration_seconds:
        session.duration_seconds = update.duration_seconds
        fields.append("duration_seconds")
    if update.error:
        session.error = update.error
        fields.append("error")

    terminal = update.status in (
        CallSession.Status.COMPLETED, CallSession.Status.FAILED, CallSession.Status.BUSY,
        CallSession.Status.NO_ANSWER, CallSession.Status.CANCELED,
    )
    if terminal and session.ended_at is None:
        session.ended_at = timezone.now()
        fields.append("ended_at")

    session.save(update_fields=fields)

    if not terminal:
        return

    if not session.outcome:
        finish(session, {
            CallSession.Status.COMPLETED: CallSession.Outcome.CALLER_ENDED,
            CallSession.Status.NO_ANSWER: CallSession.Outcome.NO_SPEECH,
        }.get(update.status, CallSession.Outcome.ERROR))

    # Summarising is a second model call and the carrier is waiting on this
    # webhook, so it goes to the worker.
    if session.turns_used:
        from .tasks import summarise_call_task

        summarise_call_task.delay(session.pk)


def hang_up(session) -> None:
    """End a call from our side -- the cancel button, or a supervisor."""
    config = VoiceAgentConfig.for_tenant(session.tenant_id)
    try:
        get_provider(config).end_call(config, session)
    except VoiceProviderError as exc:
        logger.warning("Could not hang up call %s: %s", session.pk, exc)

    if session.is_live:
        session.status = CallSession.Status.CANCELED
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_at"])
    finish(session, CallSession.Outcome.AGENT_ENDED)


def render(session, directives) -> tuple[str, str]:
    """The carrier's document for these directives, and its content type."""
    config = VoiceAgentConfig.for_tenant(session.tenant_id)
    provider = get_provider(config)
    return provider.render(directives, voice=config.voice, language=config.language), provider.content_type


def safe_render(session, directives) -> tuple[str, str]:
    """
    `render`, but a failure still produces something the carrier can play.

    A malformed document is a dropped call, so the fallback is a bare goodbye
    built without touching the provider that just failed.
    """
    try:
        return render(session, directives)
    except Exception:
        logger.exception("Could not render call directives for call %s.", session.pk)
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Say>{GOODBYE_LINE}</Say><Hangup/></Response>"
        )
        return body, "text/xml"
