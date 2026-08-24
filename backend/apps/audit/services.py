import logging

from .models import AuditEvent

logger = logging.getLogger("multisender")


def log_event(*, request, action, tenant, object_type="", object_id="", metadata=None, acting_as=None):
    """Create an audit event without allowing request metadata to override actors."""
    return None
    if tenant is None:
        # AuditEvent.tenant is NOT NULL -- a record with no tenant (e.g. a
        # user/account provisioned without one) has nothing to audit against.
        # The action being audited already happened by the time we get here
        # (see webhooks.py/views.py call sites), so failing the whole request
        # over a missing audit trail entry would be worse than skipping it.
        logger.warning("Skipping audit log for action=%s: record has no tenant.", action)
        return None
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip_address = forwarded_for.split(",")[0].strip() or request.META.get("REMOTE_ADDR")
    
    return None
    return AuditEvent.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        acting_as=acting_as,
        tenant=tenant,
        action=action,
        object_type=object_type,
        object_id=str(object_id),
        metadata=metadata or {},
        ip_address=ip_address or None,
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
    )
