"""
Catching the moment a background job gives up, wherever it gives up.

Celery emits ``task_failure`` for a task that raises out of its handler, and
that is the only signal a task's *final* failure is guaranteed to produce --
whether it exhausted ``max_retries``, raised something the handler chose not to
retry, or died on its first attempt with no retry policy at all. A ``retry`` in
progress does not fire it, which is exactly the distinction wanted here: a job
that will be tried again is not yet a problem.

The handler is deliberately defensive to the point of paranoia. It runs inside
the worker's own failure path, so an exception raised *here* would replace the
real failure with this module's failure -- turning a recoverable customer
problem into a lost one and a confusing traceback. Everything is wrapped, and a
recorder that cannot write simply logs and stands aside.

What is stored is bounded and safe by construction: task name, queue,
correlation id, exception type, a trimmed message, and a short repr of the
arguments. Never the arguments themselves. Today's tasks are called with
integer primary keys, but a task added next year might take a phone number or
a message body, and an operations table must not quietly become a second copy
of customer content.
"""

import logging

from celery.signals import task_failure

logger = logging.getLogger("sendstack")

#: Longest rendering of a task's arguments that is kept.
_MAX_ARGUMENTS = 500

#: Argument names whose value is never rendered, whatever it is. A task
#: keyword called `password` or `token` has no business in an operations table
#: even in a repr.
_REDACTED_KEYS = ("password", "token", "secret", "key", "authorization", "body", "message")


def _safe_arguments(args, kwargs) -> str:
    """
    A short, redacted rendering of what the task was called with.

    Positional arguments are rendered by *type and value only where the value
    is a number* -- every task in this codebase is called with primary keys, so
    that preserves the useful part ("which import was it") while a string
    argument becomes its type. Keyword arguments keep their names, which are
    what make a row readable, with anything sensitive-looking replaced.
    """
    try:
        rendered = []
        for value in (args or ()):
            if isinstance(value, (int, float, bool)) or value is None:
                rendered.append(repr(value))
            else:
                rendered.append(f"<{type(value).__name__}>")
        for name, value in sorted((kwargs or {}).items()):
            lowered = name.lower()
            if any(marker in lowered for marker in _REDACTED_KEYS):
                rendered.append(f"{name}=<redacted>")
            elif isinstance(value, (int, float, bool)) or value is None:
                rendered.append(f"{name}={value!r}")
            else:
                rendered.append(f"{name}=<{type(value).__name__}>")
        return ", ".join(rendered)[:_MAX_ARGUMENTS]
    except Exception:  # noqa: BLE001 - a repr must never be the thing that breaks
        return ""


def _tenant_id_from(args, kwargs):
    """
    The workspace this job belonged to, if the task said so.

    Only an explicit ``tenant_id`` keyword is trusted. Guessing -- loading the
    object the task names and reading its tenant -- would mean a database query
    inside a failure handler that may be failing *because* of the database.
    """
    value = (kwargs or {}).get("tenant_id")
    return value if isinstance(value, int) else None


def record_failure(*, task_name, task_id="", queue="", args=None, kwargs=None,
                   exception=None, attempts=0, request_id=""):
    """Write one DeadLetter row. Returns it, or None if it could not be written."""
    from apps.health.models import DeadLetter

    try:
        detail = ""
        exception_type = ""
        if exception is not None:
            exception_type = type(exception).__name__[:255]
            detail = str(exception)[: DeadLetter.MAX_DETAIL]

        return DeadLetter.objects.create(
            tenant_id=_tenant_id_from(args, kwargs),
            task_name=(task_name or "")[:255],
            task_id=(task_id or "")[:255],
            queue=(queue or "")[:64],
            arguments=_safe_arguments(args, kwargs),
            exception_type=exception_type,
            detail=detail,
            attempts=attempts or 0,
            request_id=(request_id or "")[:64],
        )
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception(
            "could not record a dead-lettered task",
            extra={"event": "deadletter.write_failed", "task": task_name},
        )
        return None


@task_failure.connect
def _on_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None,
                     einfo=None, **_):
    """
    Celery's terminal-failure hook.

    ``task_failure`` fires once, when a task raises out of the handler for the
    last time. A ``self.retry()`` raises ``Retry``, which Celery handles
    separately and which never reaches here -- so this is the give-up moment
    and not every attempt along the way.
    """
    try:
        from core.request_context import get_request_id

        # "-" is the ContextVar's default and means "no id here"; storing it
        # would make every platform job look as though it shared one.
        correlation_id = get_request_id()
        if correlation_id == "-":
            correlation_id = ""

        request = getattr(sender, "request", None)
        queue = ""
        delivery_info = getattr(request, "delivery_info", None) or {}
        if isinstance(delivery_info, dict):
            queue = delivery_info.get("routing_key") or ""

        record_failure(
            task_name=getattr(sender, "name", "") or "",
            task_id=task_id or "",
            queue=queue,
            args=args,
            kwargs=kwargs,
            exception=exception,
            # `retries` counts retries, so the number of *attempts* is one more.
            attempts=(getattr(request, "retries", 0) or 0) + 1,
            request_id=correlation_id,
        )
    except Exception:  # noqa: BLE001 - never replace the real failure with this one
        logger.exception("dead-letter handler raised", extra={"event": "deadletter.handler_failed"})
