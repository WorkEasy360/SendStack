import logging

from .models import AuditEvent

logger = logging.getLogger("sendstack")


def log_event(*, request, action, tenant, object_type="", object_id="", metadata=None, acting_as=None):
    """
    Create an audit event without allowing request metadata to override actors.

    The actor is taken from the authenticated request and never from the body:
    an audit row a caller can address to somebody else is worse than no audit
    row, because it looks like evidence.

    Best-effort by design. Every call site here is *after* the thing being
    recorded has already happened, so a failure to write the trail must not
    turn a successful action into a 500 -- it is logged loudly and the request
    still succeeds.
    """
    if tenant is None:
        # AuditEvent.tenant is NOT NULL -- a record with no tenant (e.g. a
        # user/account provisioned without one) has nothing to audit against.
        # The action being audited already happened by the time we get here
        # (see webhooks.py/views.py call sites), so failing the whole request
        # over a missing audit trail entry would be worse than skipping it.
        logger.warning("Skipping audit log for action=%s: record has no tenant.", action)
        return None

    try:
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "") if request is not None else ""
        ip_address = forwarded_for.split(",")[0].strip() or (
            request.META.get("REMOTE_ADDR") if request is not None else None
        )
        user = getattr(request, "user", None) if request is not None else None
        actor = user if (user is not None and user.is_authenticated) else None

        return AuditEvent.objects.create(
            actor=actor,
            acting_as=acting_as,
            tenant=tenant,
            action=action,
            object_type=object_type,
            object_id=str(object_id),
            metadata=metadata or {},
            ip_address=ip_address or None,
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:1000] if request is not None else ""),
        )
    except Exception:  # noqa: BLE001 - see the docstring: never fail the caller
        logger.exception("Could not write the audit event for action=%s.", action)
        return None
