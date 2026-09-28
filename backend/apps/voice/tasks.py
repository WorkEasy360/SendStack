import logging

from django.conf import settings
from django.utils import timezone

from config.celery import app

logger = logging.getLogger("sendstack")


@app.task
def place_call_task(session_id):
    """
    Dial one call.

    Never retried. A retry cannot tell "the carrier refused" from "the carrier
    accepted and our connection dropped", and the second one means calling a
    person's phone twice -- `services.place_call` records the failure and
    stops instead.
    """
    from .models import CallSession
    from .services import place_call

    session = CallSession.objects.filter(pk=session_id).select_related("contact").first()
    if session is None:
        return
    if session.status != CallSession.Status.QUEUED:
        # Already dialled, cancelled, or finished. A duplicate task delivery
        # must not ring somebody a second time.
        return

    try:
        place_call(session)
    except Exception:
        logger.exception("Placing call %s failed.", session_id)
        CallSession.objects.filter(pk=session_id, status=CallSession.Status.QUEUED).update(
            status=CallSession.Status.FAILED,
            outcome=CallSession.Outcome.ERROR,
            error="The call could not be placed.",
            ended_at=timezone.now(),
        )


@app.task
def place_calls_task(session_ids):
    """
    Dial a batch, spread out.

    Calls are paced rather than fired at once: carriers rate-limit concurrent
    originations, and a hundred simultaneous calls from one number is also
    what a spam campaign looks like to a carrier's own filtering.
    """
    interval = getattr(settings, "VOICE_CALL_INTERVAL_SECONDS", 5)
    for index, session_id in enumerate(session_ids):
        place_call_task.apply_async(args=[session_id], countdown=index * interval)


@app.task
def summarise_call_task(session_id):
    """Write the after-call summary. Best effort -- the transcript is the record."""
    from . import agent
    from .models import CallSession, VoiceAgentConfig

    session = CallSession.objects.filter(pk=session_id).first()
    if session is None or session.summary:
        return

    config = VoiceAgentConfig.for_tenant(session.tenant_id)
    if not agent.api_key(config):
        return

    try:
        summary = agent.summarise(config, session)
    except Exception:
        logger.exception("Summarising call %s failed.", session_id)
        return

    if summary:
        CallSession.objects.filter(pk=session_id).update(summary=summary)


@app.task
def reap_stale_calls_task():
    """
    Close out calls the carrier stopped talking to us about.

    A session left IN_PROGRESS forever is usually a webhook that never
    arrived. Left alone it holds a slot in "live calls" and makes the daily
    limit wrong, so anything older than its own ceiling is finished here.
    """
    from .models import CallSession, VoiceAgentConfig

    cutoff_default = getattr(settings, "VOICE_STALE_CALL_SECONDS", 1800)
    live = CallSession.objects.filter(
        status__in=(CallSession.Status.QUEUED, CallSession.Status.RINGING, CallSession.Status.IN_PROGRESS),
    ).select_related("tenant")

    now = timezone.now()
    for session in live:
        config = VoiceAgentConfig.for_tenant(session.tenant_id)
        ceiling = config.max_call_seconds or cutoff_default
        started = session.answered_at or session.started_at or session.created_at
        if (now - started).total_seconds() < max(ceiling * 2, cutoff_default):
            continue

        CallSession.objects.filter(pk=session.pk).update(
            status=CallSession.Status.FAILED,
            outcome=session.outcome or CallSession.Outcome.ERROR,
            error="The carrier stopped reporting on this call.",
            ended_at=now,
        )
        logger.warning("Reaped stale call %s.", session.pk)
