"""
What each kind of scheduled job actually does when its time comes.

A registry rather than an ``if kind == ...`` ladder in the runner, so that
apps.scheduling never imports apps.campaigns. The dependency runs the other
way -- campaigns registers itself -- which is what keeps the scheduler a
general mechanism instead of a second campaign module.

A handler is called with the ``ScheduledJob`` and is expected to either
return normally (the job is COMPLETED) or raise (the job is retried, and
FAILED once its attempts are spent). It must be safe to call twice: see
apps/scheduling/handlers.py for what that means in practice.
"""

_HANDLERS = {}


class UnknownJobKind(Exception):
    """No handler is registered for this job's kind."""


class JobPrecondition(Exception):
    """
    A scheduled job arrived at its moment and the world had moved on.

    Distinct from a failure, and the distinction matters: nothing is broken
    and retrying would change nothing. The campaign was cancelled, the
    workspace was suspended, the subscription lapsed. The job is COMPLETED --
    it did exactly what it was scheduled to do, which was to look and decide --
    and the reason is kept on the row so somebody can see why nothing sent.
    """


def register(kind: str):
    """Decorator: bind a callable to a ``ScheduledJob.Kind``."""

    def decorator(fn):
        _HANDLERS[kind] = fn
        return fn

    return decorator


def handler_for(kind: str):
    try:
        return _HANDLERS[kind]
    except KeyError as exc:
        raise UnknownJobKind(kind) from exc


def registered_kinds() -> tuple:
    return tuple(sorted(_HANDLERS))
