"""
Which WhatsApp numbers an integration may send from.

Read-only, and narrow on purpose. An external integration needs exactly one
thing from this resource -- "which ids can I put in `account_id`, and are they
working?" -- and every additional field is a field that has to stay safe
forever. Connecting, pairing, QR display and disconnection all stay in the
authenticated dashboard, because they are acts of device ownership rather than
messaging operations.
"""

from apps.whatsapp.models import WhatsAppAccount

from .. import scopes
from ..serializers import serialize_account
from ..services import resolve_account
from .base import PublicAPIView


class AccountListView(PublicAPIView):
    """``GET /accounts``."""

    required_scopes = [scopes.ACCOUNTS_READ]

    def get(self, request):
        api_key = request.auth
        queryset = WhatsAppAccount.objects.filter(tenant=api_key.tenant)

        # The key's own allowlist, applied here as well as in the send path. A
        # key restricted to one device should not be able to *enumerate* the
        # others: the list is how an integration discovers ids, and showing ids
        # it cannot use is both useless and a small information leak.
        allowed_ids = list(api_key.allowed_accounts.values_list("pk", flat=True))
        if allowed_ids:
            queryset = queryset.filter(pk__in=allowed_ids)

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter.strip().upper())

        accounts = list(queryset.order_by("pk"))
        return self.ok({"accounts": [serialize_account(account) for account in accounts]})


class AccountDetailView(PublicAPIView):
    """``GET /accounts/{id}``."""

    required_scopes = [scopes.ACCOUNTS_READ]

    def get(self, request, account_id):
        # `require_connected=False`: asking about a disconnected account is a
        # perfectly reasonable thing to do, and is in fact the main reason an
        # integration reads this endpoint at all.
        account = resolve_account(request.auth, account_id, require_connected=False)
        return self.ok(serialize_account(account))
