"""
Deterministic crash injection for the send path, for tests only.

A distributed send has an *ambiguous failure window*: the stretch between
"the provider has the message" and "our database records that it does". The
only way to test recovery from that window is to be able to kill the worker at
an exact point in it. Real process death (``os._exit``) cannot be asserted
against from inside the same test process, so this raises instead -- a
``SimulatedWorkerLost`` that propagates like an ``OperationalError`` would and
leaves the database in exactly the state a ``SIGKILL`` would have.

``crash_at`` is a no-op unless ``settings.FAILPOINTS`` names the point, so it
is inert in every deployment. Production code may call it freely on the send
path; the cost when disabled is one set membership test.

    from core.failpoints import crash_at

    crash_at("AFTER_PROVIDER_ACCEPT")   # nothing, unless a test armed it

Tests arm it with the context manager::

    with failpoint("AFTER_PROVIDER_ACCEPT"):
        send_message_task(message.id)   # raises SimulatedWorkerLost there
"""

from contextlib import contextmanager

from django.conf import settings
from django.test import override_settings

#: Every point on the send path a test may crash at. Ordered as they occur.
POINTS = (
    "AFTER_CLAIM",            # message flipped to SENDING, attempt row written, nothing sent
    "AFTER_LEASE",            # lease renewed mid-processing
    "BEFORE_PROVIDER_CALL",   # about to hand the message to the provider
    "AFTER_PROVIDER_CALL",    # provider call returned (success or failure) -- result not yet persisted
    "AFTER_PROVIDER_ACCEPT",  # provider accepted, provider_message_id in hand, row still SENDING
    "BEFORE_DB_UPDATE",       # inside the persist transaction, before the UPDATE
    "AFTER_DB_UPDATE",        # row now SENT/RECOVERY_REQUIRED, before credits/signals
    "BEFORE_WEBHOOK",         # a delivery webhook is about to be processed
)


class SimulatedWorkerLost(BaseException):
    """
    Stands in for a worker being killed at a failpoint. Never raised in production.

    Subclasses ``BaseException``, not ``Exception``, on purpose: a ``SIGKILL``
    is not something application code gets to catch, so neither is this. A
    ``try/except Exception`` on the send path -- the kind that turns an
    unexpected error into RECOVERY_REQUIRED -- must let this through untouched,
    so the test sees the same abrupt stop a real OOM would produce.
    """


def crash_at(point: str) -> None:
    """Raise ``SimulatedWorkerLost`` if a test has armed ``point``; otherwise do nothing."""
    armed = getattr(settings, "FAILPOINTS", None) or ()
    if point in armed:
        raise SimulatedWorkerLost(f"Simulated worker loss at {point}.")


@contextmanager
def failpoint(*points: str):
    """Test helper: arm one or more failpoints for the duration of the block."""
    unknown = set(points) - set(POINTS)
    if unknown:
        raise ValueError(f"Unknown failpoint(s): {sorted(unknown)}")
    with override_settings(FAILPOINTS=set(points)):
        yield
