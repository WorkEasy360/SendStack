"""
Scope checking, and the one rule that makes it trustworthy: default deny.

A public view declares what it needs::

    class MessageSendView(PublicAPIView):
        required_scopes = [scopes.MESSAGES_SEND]

and `HasApiScope` refuses anything the key does not hold. A view that declares
*nothing* is refused too -- see below. That inversion is the whole point: on a
platform where forgetting a decorator silently publishes an endpoint, the
forgetting is the vulnerability.
"""

import logging

from rest_framework.permissions import BasePermission

from .errors import INSUFFICIENT_PERMISSION, PublicAPIError

logger = logging.getLogger("sendstack")


class HasApiScope(BasePermission):
    """
    Every scope in ``view.required_scopes`` must be present on the key.

    ALL of them, not any: a view that touches two resources needs permission
    for both, and "any" would let a read-only key reach a write path that
    happens to also read.
    """

    def has_permission(self, request, view):
        api_key = getattr(request, "auth", None)
        if api_key is None:
            return False

        required = getattr(view, "required_scopes", None)
        if required is None:
            # Not "allow everything" -- a bug. A public view with no declared
            # scopes is an endpoint nobody decided the permissions for, and
            # the safe reading of an undecided permission is no.
            logger.error(
                "Public API view %s declares no required_scopes; refusing the request.",
                view.__class__.__name__,
            )
            raise PublicAPIError(
                INSUFFICIENT_PERMISSION,
                "This endpoint is not available.",
                status_code=403,
            )

        missing = [scope for scope in required if not api_key.has_scope(scope)]
        if missing:
            raise PublicAPIError(
                INSUFFICIENT_PERMISSION,
                f"This API key is missing the required scope(s): {', '.join(missing)}.",
                status_code=403,
                extra={"required_scopes": list(required), "missing_scopes": missing},
            )
        return True
