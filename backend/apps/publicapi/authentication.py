"""
Resolving ``Authorization: Bearer ss_live_...`` into a tenant, a user and a
set of permissions.

This is the whole of the chain the brief describes::

    API key -> owner/tenant -> user -> permissions -> allowed accounts -> send

with each link checked here except the last two, which belong to the request
that uses them (see permissions.py and views/base.py). What matters is that
every link is checked in *one* place: an authentication class every public
view shares, rather than a check each view has to remember.

**Session and cookie authentication are deliberately absent.** A public view
lists this class and nothing else, so a browser session -- including an
admin's -- cannot reach these endpoints at all. That is what stops the public
API from inheriting the SPA's CSRF surface and the SPA's login as an
alternative credential.
"""

import logging

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication

from . import keys as key_utils
from .errors import (
    DISABLED_API_KEY,
    EXPIRED_API_KEY,
    INVALID_API_KEY,
    REVOKED_API_KEY,
    WORKSPACE_SUSPENDED,
    PublicAPIError,
)
from .models import ApiKey

logger = logging.getLogger("sendstack")


class ApiKeyAuthentication(BaseAuthentication):
    """
    Authenticate a request from an API key, or refuse it.

    Returns ``(user, api_key)``, so `request.user` is the key's owner -- which
    is what makes credit reservation, audit actor and tenant scoping work
    through exactly the same code the SPA uses -- and `request.auth` is the
    ApiKey itself.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header:
            # No credential at all. Returning None (rather than raising) lets
            # DRF produce its own "not authenticated" path, which our handler
            # renders as INVALID_API_KEY with a WWW-Authenticate header.
            return None

        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower():
            raise PublicAPIError(
                INVALID_API_KEY,
                "Authorization header must be 'Bearer <API key>'.",
                status_code=401,
            )

        raw = parts[1]
        parsed = key_utils.parse(raw)
        if parsed is None:
            # Covers a JWT presented here as well as a mistyped key. Both get
            # the same answer on purpose: a caller able to tell "that is not
            # even a key" from "that key does not exist" has an oracle for
            # which prefixes are real.
            raise PublicAPIError(
                INVALID_API_KEY,
                "That is not a valid API key. Public API keys begin with ss_live_ or ss_test_.",
                status_code=401,
            )

        environment, prefix, secret = parsed
        api_key = (
            ApiKey.objects.select_related("tenant", "owner")
            .filter(key_prefix=prefix, environment=environment)
            .first()
        )
        if api_key is None or not key_utils.verify(secret, api_key.hashed_secret):
            raise PublicAPIError(INVALID_API_KEY, status_code=401)

        self._check_usable(api_key)
        return api_key.owner, api_key

    def authenticate_header(self, request):
        # Makes DRF answer 401 rather than 403 when no credential was given.
        return self.keyword

    @staticmethod
    def _check_usable(api_key):
        """
        The four ways a syntactically valid, correctly signed key is still
        refused -- in the order that produces the most actionable message.

        Revoked before expired before disabled: those are three different
        things somebody must do three different things about, and collapsing
        them into "invalid key" is the difference between a developer fixing
        it in a minute and opening a support ticket.
        """
        if api_key.is_revoked:
            raise PublicAPIError(REVOKED_API_KEY, status_code=401)
        if api_key.is_expired:
            raise PublicAPIError(EXPIRED_API_KEY, status_code=401)
        if not api_key.is_active:
            raise PublicAPIError(DISABLED_API_KEY, status_code=401)

        # The same two checks SendStackJWTAuthentication makes, for the same
        # reason and with the same urgency: a suspended workspace must lose
        # access on its next request, not whenever a credential happens to
        # expire. An API key never expires on its own, so without this a
        # suspended tenant would keep full send access indefinitely.
        tenant = api_key.tenant
        if tenant is None or not tenant.is_active:
            raise PublicAPIError(
                WORKSPACE_SUSPENDED,
                "This workspace is suspended. Contact support.",
                status_code=403,
            )

        owner = api_key.owner
        if owner is None or not owner.is_active or getattr(owner, "is_deleted", False):
            raise PublicAPIError(
                INVALID_API_KEY,
                "The user this key belongs to is no longer active.",
                status_code=401,
            )
        # Belt and braces on the isolation boundary. `tenant` is the field
        # every query below filters on, and the owner is who those queries run
        # as; if the two ever disagreed, a key could read one workspace while
        # billing another. Cheap to check, and impossible to get wrong twice.
        if owner.tenant_id != tenant.pk:
            logger.error(
                "API key %s has owner %s in a different tenant (%s vs %s); refusing.",
                api_key.pk, owner.pk, owner.tenant_id, tenant.pk,
            )
            raise PublicAPIError(INVALID_API_KEY, status_code=401)


def record_usage(api_key, request, *, messages=0):
    """
    Stamp last-used metadata on the key.

    A queryset ``update`` rather than ``save``: this runs on every single API
    request, it must not race with a concurrent request on the same key, and
    it must never overwrite a field the dashboard changed a millisecond ago
    (revoking a key mid-flight, say). F-expressions keep the counters exact
    under concurrency for the same reason.
    """
    from django.db.models import F

    user_agent = (request.META.get("HTTP_USER_AGENT") or "")[:255]
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = forwarded_for.split(",")[0].strip() or request.META.get("REMOTE_ADDR") or None

    fields = {
        "last_used_at": timezone.now(),
        "last_user_agent": user_agent,
        "request_count": F("request_count") + 1,
    }
    if ip:
        fields["last_used_ip"] = ip
    if messages:
        fields["message_count"] = F("message_count") + messages

    ApiKey.objects.filter(pk=api_key.pk).update(**fields)
