"""
The errors an entitlement refusal comes back as.

Kept in their own module so that every app can raise one without importing
``apps.subscriptions.services`` (which imports contacts, messaging, whatsapp
and accounts to count usage, and would make an import cycle out of half the
product).

Both are DRF exceptions, so a view that lets one escape produces the ordinary
``{"detail": ...}`` body the frontend already reads (`apiErrorMessage`), plus
a machine-readable ``code`` for the parts of the UI that want to offer an
upgrade rather than print a sentence. **403** rather than 402: the product has
no payment gateway, nothing is being asked for money at this moment, and the
accurate statement is "this account may not do that".
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class EntitlementError(APIException):
    """Base: this account's plan does not permit the thing it just asked for."""

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Your plan does not include this."
    default_code = "entitlement_denied"

    #: Merged into the response body alongside `detail`. Subclasses add their
    #: own; anything passed to __init__ wins.
    extra: dict = {}

    def __init__(self, detail=None, **extra):
        super().__init__(detail)
        self.extra = {"code": self.default_code, **type(self).extra, **extra}

    def as_dict(self) -> dict:
        """The full body, for the places that answer with a Response instead of raising."""
        return {"detail": str(self.detail), **self.extra}


class FeatureNotAvailable(EntitlementError):
    """A plan feature switch is off (`ai_agent`, `workflows`, ...)."""

    default_detail = "This feature is not included in your current plan."
    default_code = "FEATURE_NOT_AVAILABLE"

    def __init__(self, feature: str, detail=None, **extra):
        super().__init__(detail or self.default_detail, feature=feature, **extra)


class QuotaExceeded(EntitlementError):
    """A plan limit (`contacts`, `devices`, `users`, `messages_per_month`) is full."""

    default_detail = "You have reached one of your plan's limits."
    default_code = "QUOTA_EXCEEDED"

    def __init__(self, quota: str, detail=None, **extra):
        super().__init__(detail or self.default_detail, quota=quota, **extra)


class EntitlementUnavailable(EntitlementError):
    """
    The plan could not be read, so nothing may be spent against it.

    Separate from the two above because it is not the customer's fault and
    the answer is "try again", not "upgrade". It is what fails *closed* looks
    like -- see `apps.subscriptions.services.check_quota`.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = (
        "We could not confirm your plan's limits just now. Nothing was changed -- please try again in a moment."
    )
    default_code = "ENTITLEMENT_UNAVAILABLE"
