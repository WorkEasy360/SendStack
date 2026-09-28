"""
Watching a number for the signs that precede losing it.

Two things happen here. Failures are counted as they come in, and a number
that starts failing at an unusual rate stops sending by itself -- because the
alternative is a campaign that keeps hammering a number WhatsApp has already
started refusing, which is exactly how a temporary block becomes a permanent
one. And everything known about a number is folded into one score, so "is this
number in trouble" is a question the devices page can answer at a glance
instead of one that only shows up in the logs after the damage is done.

An automatic pause always has an end time: a blip that clears on its own must
not need a human to notice it. A suspected ban does not, because that one
genuinely does.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .limits import effective_limits, policy_for, usage
from .models import AccountHealthEvent, WhatsAppAccount

logger = logging.getLogger("sendstack")

#: Provider errors that mean "this number is blocked", not "this send failed".
#: Matched case-insensitively against the provider's own error text, which is
#: the only signal either provider gives us -- Baileys reports a logged-out
#: socket and the Cloud API answers 401/403 once a number is restricted.
_BAN_HINTS = (
    "logged out", "loggedout", "logged-out", "banned", "is not authorized",
    "unauthorized", "forbidden", "403", "401", "account restricted", "policy violation",
)


def record_event(account, event, detail="", severity=AccountHealthEvent.Severity.INFO) -> AccountHealthEvent:
    logger.log(
        logging.WARNING if severity != AccountHealthEvent.Severity.INFO else logging.INFO,
        "Account %s (id=%s) health event: %s -- %s", account.name, account.pk, event, detail or "no detail",
    )
    return AccountHealthEvent.objects.create(
        account=account, event=event, detail=(detail or "")[:500], severity=severity,
    )


def pause_sending(account, reason, *, minutes=None, event=AccountHealthEvent.Event.PAUSED, policy=None) -> None:
    """
    Stop this number sending.

    `minutes=None` is an indefinite pause and needs a human to lift it, which
    is right for a suspected ban and for a pause someone applied by hand. An
    automatic pause passes a duration so that the number resumes on its own
    once the cause has had time to clear.
    """
    policy = policy or policy_for(account)
    now = timezone.now()
    policy.is_paused = True
    policy.paused_at = now
    policy.paused_until = now + timedelta(minutes=minutes) if minutes else None
    policy.pause_reason = (reason or "")[:255]
    policy.save(update_fields=["is_paused", "paused_at", "paused_until", "pause_reason", "updated_at"])

    record_event(
        account, event, reason,
        severity=(
            AccountHealthEvent.Severity.CRITICAL
            if event == AccountHealthEvent.Event.BAN_SUSPECTED
            else AccountHealthEvent.Severity.WARNING
        ),
    )


def resume_sending(account, *, detail="", policy=None) -> None:
    policy = policy or policy_for(account)
    if not policy.is_paused:
        return
    policy.is_paused = False
    policy.paused_until = None
    policy.pause_reason = ""
    policy.save(update_fields=["is_paused", "paused_until", "pause_reason", "updated_at"])
    record_event(account, AccountHealthEvent.Event.RESUMED, detail or "Sending resumed.")


def pause_state(account, policy=None, now=None) -> tuple:
    """
    ``(is_paused, seconds_until_it_lifts)`` for this number.

    A pause whose end time has passed is cleared here rather than by a
    scheduled job: this is consulted before every send, so it is the earliest
    possible moment anybody could notice, and it means the feature needs no
    beat schedule to work.
    """
    policy = policy or policy_for(account)
    if not policy.is_paused:
        return False, 0.0

    now = now or timezone.now()
    if policy.paused_until and policy.paused_until <= now:
        resume_sending(account, detail="Automatic pause expired.", policy=policy)
        return False, 0.0

    remaining = (policy.paused_until - now).total_seconds() if policy.paused_until else 0.0
    return True, max(remaining, 0.0)


def failure_stats(account, since=None, now=None) -> dict:
    """Sent vs failed for this number over a window, from the message log itself."""
    from apps.messaging.models import Message

    now = now or timezone.now()
    since = since or now - timedelta(hours=24)
    outbound = Message.objects.filter(whatsapp_account=account, direction=Message.Direction.OUTBOUND)

    sent = outbound.filter(sent_at__gte=since).count()
    failed = outbound.filter(status=Message.Status.FAILED, failed_at__gte=since).count()
    total = sent + failed
    return {"sent": sent, "failed": failed, "total": total, "failure_rate": (failed / total) if total else 0.0}


def note_send_failure(account, error="") -> None:
    """
    React to one failed send.

    Called after the message has been marked FAILED, so the failure it is
    reacting to is already counted in the window it reads.
    """
    policy = policy_for(account)

    if error and any(hint in error.casefold() for hint in _BAN_HINTS):
        if not policy.is_paused:
            pause_sending(
                account,
                f"Sending stopped: the provider rejected this number ({error.strip()[:150]}). "
                "Check the number on the WhatsApp app before sending again.",
                minutes=None, event=AccountHealthEvent.Event.BAN_SUSPECTED, policy=policy,
            )
        return

    if not policy.auto_pause_enabled or not getattr(settings, "WHATSAPP_AUTO_PAUSE_ENABLED", True):
        return
    if policy.is_paused:
        return

    window_minutes = int(getattr(settings, "WHATSAPP_FAILURE_WINDOW_MINUTES", 30))
    minimum = int(getattr(settings, "WHATSAPP_FAILURE_SAMPLE_MIN", 10))
    threshold = float(getattr(settings, "WHATSAPP_FAILURE_RATE_THRESHOLD", 0.5))

    stats = failure_stats(account, since=timezone.now() - timedelta(minutes=window_minutes))
    # The minimum sample is what stops the first two failures on a quiet
    # account from pausing it: at low volume a 100% failure rate is one
    # message, and that is a coincidence rather than a pattern.
    if stats["total"] < minimum or stats["failure_rate"] < threshold:
        return

    pause_sending(
        account,
        f"{stats['failed']} of the last {stats['total']} messages failed in {window_minutes} minutes. "
        "Sending paused automatically to protect the number.",
        minutes=int(getattr(settings, "WHATSAPP_AUTO_PAUSE_MINUTES", 60)),
        event=AccountHealthEvent.Event.FAILURE_SPIKE, policy=policy,
    )


def note_limit_reached(account, decision) -> None:
    """
    Record that a number ran into its ceiling -- at most once per window.

    Without the guard this would write one row per held message, which on a
    large campaign is thousands of identical rows saying the same thing.
    """
    window = timezone.now() - timedelta(seconds=max(decision.retry_after, 60))
    already = AccountHealthEvent.objects.filter(
        account=account, event=AccountHealthEvent.Event.LIMIT_REACHED, created_at__gte=window,
    ).exists()
    if not already:
        record_event(
            account, AccountHealthEvent.Event.LIMIT_REACHED, decision.reason,
            severity=AccountHealthEvent.Severity.INFO,
        )


def health_snapshot(account) -> dict:
    """
    Everything known about how this number is doing, as one score plus its parts.

    Computed on demand rather than stored: every input is a live count, and a
    cached score that disagrees with the numbers printed next to it is worse
    than no score at all.
    """
    policy = policy_for(account)
    now = timezone.now()
    paused, pause_seconds = pause_state(account, policy=policy, now=now)
    limits = effective_limits(account, policy)
    spent = usage(account, now=now)
    stats = failure_stats(account, now=now)

    disconnects = AccountHealthEvent.objects.filter(
        account=account, event=AccountHealthEvent.Event.DISCONNECTED, created_at__gte=now - timedelta(hours=24),
    ).count()

    score = 100
    reasons = []
    if account.status == WhatsAppAccount.Status.ERROR:
        score -= 50
        reasons.append("The connection is in an error state.")
    elif account.status != WhatsAppAccount.Status.CONNECTED:
        score -= 35
        reasons.append("The number is not connected.")

    if stats["failure_rate"]:
        penalty = round(stats["failure_rate"] * 60)
        score -= penalty
        reasons.append(f"{round(stats['failure_rate'] * 100)}% of the last 24 hours of sends failed.")

    if paused:
        score -= 25
        reasons.append(policy.pause_reason or "Sending is paused.")

    if disconnects:
        score -= min(disconnects * 5, 15)
        reasons.append(f"Disconnected {disconnects} time(s) in the last 24 hours.")

    score = max(0, min(100, score))
    grade = "healthy" if score >= 80 else ("watch" if score >= 50 else "at_risk")

    def remaining(limit, used):
        return None if limit is None else max(limit - used, 0)

    return {
        "score": score,
        "grade": grade,
        "reasons": reasons,
        "is_paused": paused,
        "pause_reason": policy.pause_reason,
        "paused_until": policy.paused_until,
        "resumes_in_seconds": round(pause_seconds) if paused and policy.paused_until else None,
        "daily_limit": limits["daily_limit"],
        "daily_sent": spent["daily_sent"],
        "daily_remaining": remaining(limits["daily_limit"], spent["daily_sent"]),
        "hourly_limit": limits["hourly_limit"],
        "hourly_sent": spent["hourly_sent"],
        "hourly_remaining": remaining(limits["hourly_limit"], spent["hourly_sent"]),
        "warmup_active": limits["warmup_day"] is not None,
        "warmup_day": limits["warmup_day"],
        "warmup_days_total": limits["warmup_days_total"],
        "sent_24h": stats["sent"],
        "failed_24h": stats["failed"],
        "failure_rate_24h": round(stats["failure_rate"], 4),
        "disconnects_24h": disconnects,
    }
