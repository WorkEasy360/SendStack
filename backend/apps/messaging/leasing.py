"""
The send lease: claiming a message for one worker, for a bounded time.

Everything here is written for a world where the worker holding a message can
die at any instant -- between the claim and the provider call, between the
provider accepting the message and the row recording it, anywhere. The three
mechanisms that make that survivable are the same ones apps/scheduling uses
for ScheduledJob, one level up:

* **The claim is a conditional UPDATE, not a decision.** ``UPDATE ... WHERE
  status = 'QUEUED'`` either changes exactly one row or none. Two workers
  racing the same message: one flips it to SENDING, the other's UPDATE matches
  nothing and it does no work. There is no window in which both believe they
  own it.
* **The lease expires.** A SENDING row whose ``lease_expires_at`` is in the
  past has no live owner. The recovery sweep is then free to reconcile it --
  which is the only reason a redeploy in the middle of a campaign is survivable.
* **Every write is guarded on still owning the lease.** A slow worker whose
  lease was reclaimed by the sweep cannot come back and overwrite the newer
  state: its ``UPDATE ... WHERE lease_owner = <me>`` matches nothing.

None of this makes a send happen *exactly* once -- nothing can, across a
process boundary and a network. It makes it happen *at least* once, makes a
second attempt safe where the provider supports idempotency, and makes the
genuinely-ambiguous case explicit (RECOVERY_REQUIRED) instead of silently
wrong.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.scheduling.services import worker_identity  # hostname:pid, reused

from core.failpoints import crash_at

from .models import Message, MessageSendAttempt

logger = logging.getLogger("sendstack")


def lease_seconds() -> int:
    """
    How long a worker owns a message before the sweep may reconcile it.

    Must comfortably exceed the longest a single message's provider work can
    legitimately take: the QR bridge's own HTTP timeout is 45s, the Cloud
    API's 15s, and a multi-attachment message is a few of those in series with
    a lease renewal between each. 180s is generous by design -- a lease that
    is too short "recovers" a send that is still in progress, which is the one
    thing this must not do.
    """
    try:
        return max(int(getattr(settings, "SEND_LEASE_SECONDS", 180)), 30)
    except (TypeError, ValueError):
        return 180


def _idempotency_key(message: Message, attempt_number: int) -> str:
    """
    Deterministic identity for one physical send attempt.

    ``tenant:campaign:message:attempt`` -- never random, so a retry keeps the
    logical identity and only the trailing number moves. This same string is
    the wire correlation id: ``send_operation_id`` to the QR bridge,
    ``biz_opaque_callback_data`` to the Cloud API.
    """
    campaign = message.campaign_id if message.campaign_id else "x"
    return f"{message.tenant_id}:{campaign}:{message.pk}:{attempt_number}"


@transaction.atomic
def claim_for_send(message: Message, *, task_id: str = "") -> MessageSendAttempt | None:
    """
    Take ownership of a QUEUED message and open an attempt row.

    Returns the attempt, or None if the message was not QUEUED (another worker
    already claimed it, or it was cancelled/resolved out from under us). The
    caller must stop when this returns None.
    """
    worker = worker_identity()
    now = timezone.now()
    expires = now + timedelta(seconds=lease_seconds())

    claimed = Message.objects.filter(pk=message.pk, status=Message.Status.QUEUED).update(
        status=Message.Status.SENDING,
        lease_owner=worker,
        lease_expires_at=expires,
        lease_heartbeat_at=now,
        updated_at=now,
    )
    if not claimed:
        return None

    # Safe against the unique (message, attempt_number) constraint because the
    # conditional UPDATE above guarantees exactly one worker reaches this line
    # for a given (message, QUEUED->SENDING) transition.
    last = (
        MessageSendAttempt.objects.filter(message_id=message.pk)
        .aggregate(n=Max("attempt_number"))
        .get("n")
        or 0
    )
    attempt_number = last + 1

    attempt = MessageSendAttempt.objects.create(
        message_id=message.pk,
        tenant_id=message.tenant_id,
        campaign_id=message.campaign_id,
        attempt_number=attempt_number,
        worker_id=worker,
        task_id=task_id or "",
        account_id=message.whatsapp_account_id,
        provider=getattr(message.whatsapp_account, "provider", "") or "",
        idempotency_key=_idempotency_key(message, attempt_number),
        status=MessageSendAttempt.Status.CLAIMED,
        started_at=now,
        lease_expires_at=expires,
        last_heartbeat_at=now,
    )

    # Keep the in-memory copy consistent for the rest of this task.
    message.status = Message.Status.SENDING
    message.lease_owner = worker
    message.lease_expires_at = expires
    message.lease_heartbeat_at = now

    logger.info(
        "MESSAGE_CLAIMED",
        extra={
            "event": "message.claimed",
            "message_id": message.pk,
            "attempt_id": attempt.pk,
            "operation_id": attempt.idempotency_key,
            "task_id": task_id,
            "campaign_id": message.campaign_id,
            "tenant_id": message.tenant_id,
            "provider": attempt.provider,
            "account_id": message.whatsapp_account_id,
            "worker_id": worker,
        },
    )
    return attempt


def renew_lease(message: Message, attempt: MessageSendAttempt) -> bool:
    """
    Extend the lease while a multi-step send is in progress.

    Returns False when the lease is no longer ours -- the sweep reclaimed it
    because we were too slow. A caller that sees False must abort: continuing
    to send would race the recovery worker. There is no background thread; the
    renewal points are the seams between provider calls in a multi-attachment
    message, which is where the wall-clock time actually goes.
    """
    now = timezone.now()
    expires = now + timedelta(seconds=lease_seconds())
    renewed = Message.objects.filter(
        pk=message.pk, status=Message.Status.SENDING, lease_owner=message.lease_owner,
    ).update(lease_expires_at=expires, lease_heartbeat_at=now, updated_at=now)
    if not renewed:
        logger.warning(
            "LEASE_EXPIRED",
            extra={
                "event": "message.lease_expired",
                "message_id": message.pk,
                "attempt_id": attempt.pk,
                "operation_id": attempt.idempotency_key,
                "worker_id": message.lease_owner,
            },
        )
        return False
    message.lease_expires_at = expires
    message.lease_heartbeat_at = now
    MessageSendAttempt.objects.filter(pk=attempt.pk).update(
        lease_expires_at=expires, last_heartbeat_at=now,
    )
    logger.debug("LEASE_RENEWED", extra={"event": "message.lease_renewed", "message_id": message.pk})
    crash_at("AFTER_LEASE")
    return True


def lease_is_ours(message: Message) -> bool:
    """Whether this worker still holds the lease on this message (a SENDING row it owns)."""
    return Message.objects.filter(
        pk=message.pk, status=Message.Status.SENDING, lease_owner=message.lease_owner,
    ).exists()


def advance_attempt(attempt: MessageSendAttempt, *, status: str = None, **fields) -> bool:
    """
    Move an attempt forward under its optimistic-concurrency guard.

    Returns False if the row moved underneath us (the sweep reclaimed it), in
    which case the caller must not touch the message either.
    """
    expected = attempt.state_version
    if status is not None:
        fields["status"] = status
    fields["state_version"] = expected + 1
    updated = MessageSendAttempt.objects.filter(pk=attempt.pk, state_version=expected).update(**fields)
    if not updated:
        return False
    attempt.state_version = expected + 1
    for key, value in fields.items():
        setattr(attempt, key, value)
    return True
