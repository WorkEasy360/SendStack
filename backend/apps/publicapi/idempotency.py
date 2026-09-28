"""
Making a retried request safe to retry.

A messaging API without this is a messaging API that sends twice whenever a
client's connection drops after we accepted the request but before the
response reached them -- which is exactly the moment a well-written client
retries. The client cannot tell that case apart from "the request never
arrived", so the server has to.

The contract:

* the caller sends ``Idempotency-Key: <anything unique to this operation>``;
* the **first** request runs normally and its response is stored;
* a **repeat with the same body** gets that stored response back verbatim,
  with ``Idempotency-Replayed: true``, and nothing is sent;
* a **repeat with a different body** is refused with IDEMPOTENCY_CONFLICT,
  because that is a bug in the caller and answering it with the old result
  would hide the bug behind a plausible success;
* a **concurrent** repeat, arriving while the first is still in flight, is
  refused with IDEMPOTENCY_IN_PROGRESS and a 409. This is the case that
  actually prevents the duplicate send: two workers retrying at once are far
  commoner than one worker retrying twice.

The claim is a unique-constraint insert, not a read-then-write. Two concurrent
requests both find nothing, both try to insert, and PostgreSQL lets exactly
one win -- which is the only version of this that is correct without a lock.
"""

import hashlib
import json
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .errors import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_IN_PROGRESS,
    INVALID_REQUEST,
    PublicAPIError,
)
from .models import IdempotencyRecord

logger = logging.getLogger("sendstack")

HEADER = "HTTP_IDEMPOTENCY_KEY"
MAX_KEY_LENGTH = 255

#: How long a stored result answers a repeat. 24 hours is long enough to cover
#: any retry a queue or a cron will still be making, and short enough that the
#: table stays small; it is swept by apps.publicapi.tasks.prune_idempotency.
RETENTION_HOURS = 24


def fingerprint(payload) -> str:
    """
    A stable hash of the request body.

    `sort_keys` matters: two JSON objects with the same content and different
    key order are the same request, and a client that rebuilds its payload from
    a dict between retries will legitimately produce both orderings.
    """
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def key_from(request) -> str:
    key = (request.META.get(HEADER) or "").strip()
    if key and len(key) > MAX_KEY_LENGTH:
        raise PublicAPIError(
            INVALID_REQUEST,
            f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters.",
        )
    return key


def claim(request, api_key, endpoint: str, payload):
    """
    Try to claim this idempotency key for this request.

    Returns ``(record, replay)``:

    * ``(None, None)``   -- no key was sent; the caller proceeds unprotected.
    * ``(record, None)`` -- claimed. The caller proceeds and must finish by
      calling `complete` or `abandon`.
    * ``(record, response_dict)`` -- a completed record exists; the caller
      returns the stored response and does nothing else.
    """
    key = key_from(request)
    if not key:
        return None, None

    digest = fingerprint(payload)
    now = timezone.now()

    existing = IdempotencyRecord.objects.filter(tenant=api_key.tenant, key=key).first()
    if existing is not None and existing.expires_at > now:
        return _replay(existing, endpoint, digest)

    if existing is not None:
        # Expired. Reclaiming the row rather than inserting beside it, because
        # the unique constraint is on (tenant, key) and a fresh insert would
        # collide with the corpse.
        IdempotencyRecord.objects.filter(pk=existing.pk).delete()

    try:
        with transaction.atomic():
            record = IdempotencyRecord.objects.create(
                tenant=api_key.tenant,
                api_key=api_key,
                key=key,
                endpoint=endpoint,
                fingerprint=digest,
                state=IdempotencyRecord.State.IN_PROGRESS,
                expires_at=now + timezone.timedelta(hours=RETENTION_HOURS),
            )
    except IntegrityError:
        # Lost the race. Whoever won is either still running or has finished;
        # re-reading tells us which, and `_replay` answers both correctly.
        winner = IdempotencyRecord.objects.filter(tenant=api_key.tenant, key=key).first()
        if winner is None:
            # Vanishingly unlikely -- the winner was deleted between our
            # failed insert and this read. Treat it as unprotected rather than
            # failing a request that has done nothing wrong.
            logger.warning("Idempotency key %r vanished during a race; proceeding unprotected.", key)
            return None, None
        return _replay(winner, endpoint, digest)

    return record, None


def _replay(record, endpoint: str, digest: str):
    """Decide what an existing record means for the request now arriving."""
    if record.fingerprint != digest or record.endpoint != endpoint:
        raise PublicAPIError(
            IDEMPOTENCY_CONFLICT,
            "This Idempotency-Key was already used for a different request. "
            "Use a new key for a new request.",
            status_code=409,
        )

    if record.state == IdempotencyRecord.State.IN_PROGRESS:
        raise PublicAPIError(
            IDEMPOTENCY_IN_PROGRESS,
            "A request with this Idempotency-Key is still being processed. Retry shortly.",
            status_code=409,
            extra={"retry_after": 2},
        )

    return record, {"status_code": record.status_code or 200, "body": record.response_body}


def complete(record, status_code: int, body) -> None:
    """Store the finished response so the next retry gets it back."""
    if record is None:
        return
    IdempotencyRecord.objects.filter(pk=record.pk).update(
        state=IdempotencyRecord.State.COMPLETED,
        status_code=status_code,
        response_body=body if isinstance(body, dict) else {"data": body},
        completed_at=timezone.now(),
    )


def abandon(record) -> None:
    """
    Release a claim whose request failed.

    Deleted rather than marked failed, so the caller may retry the same key and
    have it actually run. A failed request sent nothing, so there is nothing to
    protect against repeating -- and leaving the claim behind would lock the
    caller out of their own key for a day over a validation error.
    """
    if record is None:
        return
    IdempotencyRecord.objects.filter(pk=record.pk, state=IdempotencyRecord.State.IN_PROGRESS).delete()
