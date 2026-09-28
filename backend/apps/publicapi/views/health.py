"""
"Is this working?" -- asked two ways, because there are two different questions
behind it and answering them with one endpoint helps nobody.

``GET /health`` answers **"can I reach the API, and is my token good?"**. It is
the first call an integration makes and the first call a customer makes from
Postman, so it works with no credential at all (reporting `authenticated:
false`) and with a good one (reporting the workspace it belongs to). A *bad*
credential is still a 401 -- a health check that returns 200 for a revoked key
is a health check that hides the only thing it was asked about.

``GET /diagnostics`` answers **"will a send actually go out?"**, which is a
different question with a much longer answer. It walks the whole chain the
brief describes -- key, permission, workspace, WhatsApp connection, credits --
and reports each link separately. That separation is the point: "API
authentication PASS / WhatsApp connection FAIL" tells somebody to go and scan a
QR code, while a single "it didn't work" sends them to support.
"""

from django.utils import timezone
from rest_framework.permissions import BasePermission

from apps.whatsapp.models import WhatsAppAccount

from .. import scopes as scope_defs
from ..errors import INSUFFICIENT_PERMISSION, PublicAPIError
from ..models import ApiKey
from ..serializers import serialize_account
from .base import PublicAPIView


class AllowAnonymousOrScoped(BasePermission):
    """
    Like `HasApiScope`, but an *absent* credential is allowed through.

    Used by exactly one view. `HasApiScope` refuses a request with no key,
    which is correct everywhere else and wrong on the endpoint whose job is to
    tell an unauthenticated caller that the API is up. A bad key never reaches
    here -- `ApiKeyAuthentication` raises before permissions run -- so the only
    thing this widens is "no Authorization header at all".
    """

    def has_permission(self, request, view):
        api_key = getattr(request, "auth", None)
        if api_key is None:
            return True

        required = getattr(view, "required_scopes", None) or []
        missing = [scope for scope in required if not api_key.has_scope(scope)]
        if missing:
            raise PublicAPIError(
                INSUFFICIENT_PERMISSION,
                f"This API key is missing the required scope(s): {', '.join(missing)}.",
                status_code=403,
            )
        return True


class HealthView(PublicAPIView):
    """``GET /health`` -- reachability, and whether the presented token works."""

    permission_classes = [AllowAnonymousOrScoped]
    #: Scope-free by design: a token has to be able to prove it is valid before
    #: it knows what it is allowed to do.
    required_scopes = []

    def get(self, request):
        api_key = request.auth
        body = {
            "api": "online",
            "version": "v1",
            "authenticated": api_key is not None,
            "timestamp": timezone.now().isoformat(),
        }

        if api_key is None:
            # Said explicitly rather than left to be inferred from
            # `authenticated: false`. Somebody testing their setup in Postman
            # who forgot the header needs to be told which header.
            body["message"] = (
                "The API is reachable. Send 'Authorization: Bearer <your API key>' "
                "to confirm your token as well."
            )
            return self.ok(body)

        body.update({
            "user": {
                "id": str(api_key.owner_id),
                "email": api_key.owner.email,
            },
            "workspace": {
                "id": api_key.tenant_id,
                "name": api_key.tenant.name,
            },
            "token": {
                "name": api_key.name,
                # The masked prefix, never more. This body is the one a
                # customer is most likely to paste into a support ticket.
                "key": api_key.masked_key,
                "environment": api_key.environment,
                "scopes": api_key.scopes or [],
                "permissions": scope_defs.labels_for(api_key.scopes),
                "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
            },
        })
        return self.ok(body)


class DiagnosticsView(PublicAPIView):
    """
    ``GET /diagnostics`` -- every link in the send chain, checked and named.

    Deliberately read-only: it inspects the same objects a send would use and
    reports on them, but it queues nothing and reserves nothing. A diagnostic
    that sends a message is a diagnostic somebody runs once and then never
    dares run again.
    """

    required_scopes = []

    def get(self, request):
        api_key = request.auth
        checks = [
            self._authentication(api_key),
            self._permissions(api_key),
            self._workspace(api_key),
            self._whatsapp(api_key),
            self._credits(api_key),
        ]
        failed = [check for check in checks if check["status"] == "fail"]
        return self.ok({
            # `ready_to_send` is the one-line answer, and it is the AND of the
            # links -- so a UI can show a single green tick without deciding
            # for itself which of five checks were the important ones.
            "ready_to_send": not failed,
            "checks": checks,
            "summary": (
                "Everything needed to send a message is in place."
                if not failed
                else "; ".join(check["detail"] for check in failed)
            ),
            "timestamp": timezone.now().isoformat(),
        })

    # --- the individual links -------------------------------------------
    #
    # Each returns the same shape so the UI renders a uniform list, and each
    # says what to *do* on failure rather than only what is wrong.

    @staticmethod
    def _check(name, label, ok, detail, **extra):
        return {"name": name, "label": label, "status": "pass" if ok else "fail", "detail": detail, **extra}

    def _authentication(self, api_key):
        # Reaching this method at all means the key parsed, matched a stored
        # hash, and passed the revoked/expired/disabled gate in
        # ApiKeyAuthentication. There is nothing left to re-check.
        return self._check(
            "authentication",
            "API token",
            True,
            f"Authenticated as '{api_key.name}' ({api_key.masked_key}).",
            environment=api_key.environment,
        )

    def _permissions(self, api_key):
        can_send = api_key.has_scope(scope_defs.MESSAGES_SEND)
        return self._check(
            "permissions",
            "Send permission",
            can_send,
            (
                "This token may send messages."
                if can_send
                else "This token does not have the 'Send WhatsApp messages' permission. "
                     "Create a new token with it ticked -- permissions cannot be changed after creation."
            ),
            scopes=api_key.scopes or [],
            permissions=scope_defs.labels_for(api_key.scopes),
        )

    def _workspace(self, api_key):
        # A live key sends; a test key is refused by services.send_message
        # before it touches a provider. Reported as a failure of *this* check
        # rather than of the WhatsApp one, because the fix is a different key
        # rather than a different phone.
        if api_key.environment == ApiKey.Environment.TEST:
            return self._check(
                "workspace",
                "Workspace",
                False,
                "This is a test token. Test tokens are refused by the send endpoints. "
                "Create a live token (ss_live_...) to send real messages.",
                workspace=api_key.tenant.name,
            )
        return self._check(
            "workspace",
            "Workspace",
            True,
            f"Workspace '{api_key.tenant.name}' is active.",
            workspace=api_key.tenant.name,
        )

    def _whatsapp(self, api_key):
        """
        The link the brief singles out, and the one that fails most often.

        Counts what the key can actually reach -- the tenant's accounts
        narrowed by the key's own allowlist -- so a key restricted to one
        disconnected number reports a failure even when the workspace has
        other numbers working.
        """
        from apps.messaging.services import account_is_sendable

        accounts = WhatsAppAccount.objects.filter(tenant=api_key.tenant)
        allowed_ids = list(api_key.allowed_accounts.values_list("pk", flat=True))
        if allowed_ids:
            accounts = accounts.filter(pk__in=allowed_ids)

        rows = list(accounts.order_by("pk"))
        sendable = [account for account in rows if account_is_sendable(account)]
        # `serialize_account` rather than a dict built here: it is the
        # hand-written allowlist that decides what an API caller may see of a
        # number, and a second one in this file would be a second place for the
        # next credential column to leak from.
        detail_rows = [
            {**serialize_account(account), "can_send": account_is_sendable(account)}
            for account in rows
        ]

        if not rows:
            return self._check(
                "whatsapp_connection",
                "WhatsApp connection",
                False,
                "This workspace has no WhatsApp accounts. Connect one under WhatsApp -> Accounts."
                if not allowed_ids
                else "This token is restricted to WhatsApp accounts that no longer exist.",
                accounts=detail_rows,
            )
        if not sendable:
            return self._check(
                "whatsapp_connection",
                "WhatsApp connection",
                False,
                f"No active WhatsApp account is available for this token. "
                f"{len(rows)} account(s) exist but none are connected and unpaused.",
                accounts=detail_rows,
            )
        return self._check(
            "whatsapp_connection",
            "WhatsApp connection",
            True,
            f"{len(sendable)} of {len(rows)} WhatsApp account(s) can send.",
            accounts=detail_rows,
        )

    def _credits(self, api_key):
        """
        Available, not total. A balance of 500 with 500 reserved sends nothing,
        and reporting the total would say everything is fine right up to the
        402 -- which is exactly the confusion this endpoint exists to prevent.
        """
        owner = api_key.owner
        available = (owner.credits or 0) - (owner.reserved_credits or 0)
        return self._check(
            "credits",
            "Credits",
            available > 0,
            (
                f"{available} credit(s) available."
                if available > 0
                else "No credits available. Sends are refused with INSUFFICIENT_CREDITS until the balance is topped up."
            ),
            available=available,
            balance=owner.credits or 0,
            reserved=owner.reserved_credits or 0,
        )
