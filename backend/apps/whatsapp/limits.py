"""
Send limits and warmup for one WhatsApp number.

The rule this enforces is simple and the reason for it is not negotiable: a
number that sends far more today than it has ever sent before is what WhatsApp
bans, and a banned number takes the customer's whole channel with it. So every
account has a daily and an hourly ceiling, and a number that has only just been
connected is walked up to that ceiling over its first days instead of being
handed it on day one.

A message that runs into a ceiling is **held, never dropped** -- see
apps.messaging.gating, which turns a refusal here into a re-queue timed for
when the window reopens. Nobody loses a recipient because the number was busy.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AccountSendQuota, SendingPolicy, WhatsAppAccount

logger = logging.getLogger("multisender")


@dataclass
class SlotDecision:
    """The answer to "may this number send one more message right now?"."""

    allowed: bool
    reason: str = ""
    #: Seconds until it is worth asking again. Zero when the answer will not
    #: change on its own (a human has to lift an indefinite pause).
    retry_after: float = 0.0
    scope: str = ""
    detail: dict = field(default_factory=dict)


def _ladder() -> list:
    """The warmup ramp as a list of daily allowances, day 1 first."""
    raw = getattr(settings, "WHATSAPP_WARMUP_LADDER", None) or []
    return [int(step) for step in raw if int(step) > 0]


def _default_limit(name: str):
    """A configured ceiling, or None for 'no ceiling' (which is what 0 means)."""
    value = int(getattr(settings, name, 0) or 0)
    return value or None


def policy_for(account) -> SendingPolicy:
    """
    This account's sending policy, creating it on first use.

    The warmup clock starts from the number's *first send* where it has one,
    not from when this row happened to be created. Without that, switching the
    feature on would put every long-established number in the deployment back
    on day one of a ramp and throttle it to 50 messages -- the ramp exists for
    numbers with no history, and a number with months of history has none of
    the risk it protects against.
    """
    policy = SendingPolicy.objects.filter(account=account).first()
    if policy is not None:
        return policy

    from apps.messaging.models import Message

    first_send = (
        Message.objects.filter(whatsapp_account=account, direction=Message.Direction.OUTBOUND, sent_at__isnull=False)
        .order_by("sent_at")
        .values_list("sent_at", flat=True)
        .first()
    )
    started = first_send or account.last_connected_at or account.created_at or timezone.now()

    policy, _ = SendingPolicy.objects.get_or_create(
        account=account,
        defaults={
            "warmup_enabled": bool(getattr(settings, "WHATSAPP_WARMUP_ENABLED", True)),
            "warmup_started_on": timezone.localdate(started),
        },
    )
    return policy


def warmup_state(account, policy, today=None) -> tuple:
    """
    ``(day_number, daily_allowance)`` while a number is ramping up, else ``(None, None)``.

    Only QR/device-linked numbers ramp. A Cloud API number is metered by Meta
    itself, in tiers tied to the business's own quality rating, and layering a
    second invented ramp on top of that would throttle a number Meta has
    already cleared to send more.
    """
    ladder = _ladder()
    if not ladder or not policy.warmup_enabled or not getattr(settings, "WHATSAPP_WARMUP_ENABLED", True):
        return None, None
    if account.provider != WhatsAppAccount.Provider.QR_PROVIDER:
        return None, None

    today = today or timezone.localdate()
    started = policy.warmup_started_on or today
    index = max((today - started).days, 0)
    if index >= len(ladder):
        return None, None
    return index + 1, ladder[index]


def effective_limits(account, policy=None, today=None) -> dict:
    """
    The ceilings actually in force for this number right now.

    An explicitly configured limit and the warmup allowance are combined by
    taking the *lower* of the two, so setting a number's own limit can tighten
    the ramp but never bypass it -- the ramp is a safety floor, and a limit
    typed into a form should not be able to switch it off by accident.
    """
    policy = policy or policy_for(account)
    daily = policy.daily_limit or _default_limit("WHATSAPP_DAILY_SEND_LIMIT")
    hourly = policy.hourly_limit or _default_limit("WHATSAPP_HOURLY_SEND_LIMIT")

    warmup_day, warmup_limit = warmup_state(account, policy, today)
    if warmup_limit is not None:
        daily = warmup_limit if daily is None else min(daily, warmup_limit)

    return {
        "daily_limit": daily,
        "hourly_limit": hourly,
        "warmup_day": warmup_day,
        "warmup_limit": warmup_limit,
        "warmup_days_total": len(_ladder()) if warmup_day else None,
    }


def _windows(now, day_zone=None) -> dict:
    """
    The start of the day and hour windows this moment falls in.

    The day boundary is the tenant's local midnight, not UTC's: "500 a day" is
    a decision someone made about their own working day, and a limit that
    resets at 5:30am local time reads as a bug to everyone who relies on it.
    """
    local = now.astimezone(day_zone) if day_zone else now
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        AccountSendQuota.Scope.DAY: day_start.astimezone(dt_timezone.utc),
        AccountSendQuota.Scope.HOUR: now.replace(minute=0, second=0, microsecond=0),
    }


def usage(account, now=None, day_zone=None) -> dict:
    """How much of each window this number has already spent. Read-only."""
    now = now or timezone.now()
    windows = _windows(now, day_zone)
    counts = {
        (row["scope"], row["window_start"]): row["sent_count"]
        for row in AccountSendQuota.objects.filter(
            account=account, window_start__in=list(windows.values()),
        ).values("scope", "window_start", "sent_count")
    }
    return {
        "daily_sent": counts.get((AccountSendQuota.Scope.DAY, windows[AccountSendQuota.Scope.DAY]), 0),
        "hourly_sent": counts.get((AccountSendQuota.Scope.HOUR, windows[AccountSendQuota.Scope.HOUR]), 0),
    }


def _seconds_until(moment, now) -> float:
    return max((moment - now).total_seconds(), 1.0)


def reserve_send_slot(account, policy=None, now=None, day_zone=None) -> SlotDecision:
    """
    Take one send slot from this number's day and hour windows, or refuse.

    Both counters are locked and checked inside one transaction before either
    is incremented, so a refused message never consumes part of an allowance,
    and two workers cannot both be told they have the last slot. The rows are
    always locked day-then-hour: a fixed order is what stops two workers
    holding one lock each and deadlocking on the other.
    """
    now = now or timezone.now()
    policy = policy or policy_for(account)
    limits = effective_limits(account, policy)
    windows = _windows(now, day_zone)

    if limits["daily_limit"] is None and limits["hourly_limit"] is None:
        return SlotDecision(allowed=True, detail=limits)

    ordered = [
        (AccountSendQuota.Scope.DAY, limits["daily_limit"], windows[AccountSendQuota.Scope.DAY], timedelta(days=1)),
        (AccountSendQuota.Scope.HOUR, limits["hourly_limit"], windows[AccountSendQuota.Scope.HOUR], timedelta(hours=1)),
    ]

    with transaction.atomic():
        rows = []
        for scope, limit, window_start, span in ordered:
            if limit is None:
                continue
            AccountSendQuota.objects.get_or_create(account=account, scope=scope, window_start=window_start)
            row = AccountSendQuota.objects.select_for_update().get(
                account=account, scope=scope, window_start=window_start,
            )
            if row.sent_count >= limit:
                return SlotDecision(
                    allowed=False,
                    reason=(
                        f"{account.name} has reached its "
                        f"{'daily' if scope == AccountSendQuota.Scope.DAY else 'hourly'} "
                        f"limit of {limit} messages."
                    ),
                    retry_after=_seconds_until(window_start + span, now),
                    scope=scope,
                    detail=limits,
                )
            rows.append(row)

        for row in rows:
            row.sent_count += 1
        AccountSendQuota.objects.bulk_update(rows, ["sent_count"])

    return SlotDecision(allowed=True, detail=limits)
