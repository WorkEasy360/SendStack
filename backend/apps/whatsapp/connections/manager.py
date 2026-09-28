"""
Orchestrates a WhatsAppAccount's connection lifecycle on top of a provider.

This is the only place account.status (and WhatsAppConnectionSession.status)
transitions happen. Campaign/messaging code should call ``send_*`` here
rather than reaching into a provider or the model directly, so lifecycle
rules (e.g. "don't send while disconnected") stay in one place.

QR payloads are never written to Postgres (see models.py docstring on
WhatsAppConnectionSession) -- they live in the cache (Redis in production,
see CACHES in settings) keyed by session id, with a TTL matching the
provider's stated expiry.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..models import WhatsAppConnectionSession
from ..providers import ConnectionWebhookEvent, SendResult, get_provider
from ..providers.base import ProviderError

logger = logging.getLogger("sendstack")

_QR_CACHE_PREFIX = "whatsapp:qr:"


def _qr_cache_key(session_id: int) -> str:
    return f"{_QR_CACHE_PREFIX}{session_id}"


def _human_error(exc: ProviderError) -> str:
    """Map known provider error codes to copy a non-technical user can act on."""
    friendly = {
        "not_configured": "QR provider is not configured.",
        "configuration_error": "WhatsApp provider is not fully configured. Contact your administrator.",
        "not_supported": exc.message,
        "no_qr": "No QR code is available yet. Try again in a moment.",
        "network_error": "Could not reach the WhatsApp provider. Please try again.",
        "timeout": "The WhatsApp provider took too long to respond. Please try again.",
        "authentication_error": "Could not authenticate with the WhatsApp provider. Contact your administrator.",
        "invalid_response": "The WhatsApp provider returned an unexpected response. Please try again.",
        "missing_credentials": exc.message,
    }
    return friendly.get(exc.code, exc.message)


class ConnectionManager:

    @staticmethod
    def _latest_session(account) -> WhatsAppConnectionSession | None:
        return account.connection_sessions.first()

    @classmethod
    def start_connection(cls, account) -> dict:
        """Begin a new connection attempt (QR pairing or Cloud API credential check)."""
        provider = get_provider(account)

        account.status = account.Status.CONNECTING
        account.error_message = ""
        account.save(update_fields=["status", "error_message", "updated_at"])

        session = WhatsAppConnectionSession.objects.create(
            whatsapp_account=account,
            provider=account.provider,
            status=WhatsAppConnectionSession.Status.CONNECTING,
        )

        try:
            result = provider.start_connection(account)
        except ProviderError as exc:
            message = _human_error(exc)
            account.status = account.Status.ERROR
            account.error_message = message
            account.save(update_fields=["status", "error_message", "updated_at"])
            session.status = WhatsAppConnectionSession.Status.ERROR
            session.error_message = message
            session.save(update_fields=["status", "error_message", "updated_at"])
            return {"success": False, "status": account.status, "message": message, "session_id": None, "expires_at": None}

        return cls._apply_session_result(account, session, result)

    @classmethod
    def refresh_qr(cls, account) -> dict:
        old_session = cls._latest_session(account)
        if old_session and old_session.status not in (
            WhatsAppConnectionSession.Status.EXPIRED,
            WhatsAppConnectionSession.Status.ERROR,
            WhatsAppConnectionSession.Status.DISCONNECTED,
        ):
            # A refresh is a replacement, not an additional socket. This is
            # especially important when the database says CONNECTED but the
            # provider socket is stale/corrupted: starting a fresh QR session
            # while the old one is still alive gives Baileys two owners of the
            # same WhatsApp device and can corrupt Signal ratchets (Bad MAC).
            if old_session.session_id:
                provider = get_provider(account)
                try:
                    provider.disconnect(account, old_session.session_id)
                except ProviderError as exc:
                    logger.warning(
                        "Failed to disconnect provider session_id=%s for account_id=%s before refresh: %s",
                        old_session.session_id, account.pk, exc.message,
                    )
            old_session.status = (
                WhatsAppConnectionSession.Status.EXPIRED
                if old_session.status in (
                    WhatsAppConnectionSession.Status.CONNECTING,
                    WhatsAppConnectionSession.Status.QR_READY,
                )
                else WhatsAppConnectionSession.Status.DISCONNECTED
            )
            old_session.save(update_fields=["status", "updated_at"])
            cache.delete(_qr_cache_key(old_session.pk))
        return cls.start_connection(account)

    @classmethod
    def _apply_session_result(cls, account, session, result) -> dict:
        # `result` is a ConnectionSessionResult (from start_connection/
        # refresh_qr) when called from those paths, but a
        # ConnectionStatusResult (from get_status's polling) when called
        # from get_status -- the latter has no session_id field at all
        # (polling never introduces a new session id), so guard the
        # attribute lookup instead of assuming ConnectionSessionResult's
        # shape unconditionally.
        session_id = getattr(result, "session_id", None)
        if session_id:
            session.session_id = session_id
        session.qr_expires_at = result.qr_expires_at

        if result.status == "QR_READY":
            account.status = account.Status.QR_READY
            account.provider_session_id = session.session_id
            account.save(update_fields=["status", "provider_session_id", "updated_at"])
            session.status = WhatsAppConnectionSession.Status.QR_READY
            session.save(update_fields=["session_id", "qr_expires_at", "status", "updated_at"])
            if result.qr_data:
                ttl = max(int((result.qr_expires_at - timezone.now()).total_seconds()), 1) if result.qr_expires_at else settings.WHATSAPP_QR_SESSION_TTL_SECONDS
                cache.set(_qr_cache_key(session.pk), result.qr_data, timeout=ttl)
            return {
                "success": True,
                "status": account.status,
                "message": "Scan the QR code with WhatsApp.",
                "session_id": session.session_id,
                "expires_at": result.qr_expires_at,
            }

        if result.status == "CONNECTED":
            cls._mark_connected(account, session, result)
            return {
                "success": True,
                "status": account.status,
                "message": "WhatsApp account connected.",
                "session_id": session.session_id,
                "expires_at": None,
            }

        message = result.error or "Unable to connect this WhatsApp account."
        account.status = account.Status.ERROR
        account.error_message = message
        account.save(update_fields=["status", "error_message", "updated_at"])
        session.status = WhatsAppConnectionSession.Status.ERROR
        session.error_message = message
        session.save(update_fields=["session_id", "qr_expires_at", "status", "error_message", "updated_at"])
        return {"success": False, "status": account.status, "message": message, "session_id": session.session_id, "expires_at": None}

    @staticmethod
    def _mark_connected(account, session, result) -> None:
        account.status = account.Status.CONNECTED
        account.error_message = ""
        if result.phone_number:
            account.phone_number = result.phone_number
        if result.provider_account_id:
            account.provider_account_id = result.provider_account_id
        account.provider_session_id = session.session_id or account.provider_session_id
        account.last_connected_at = timezone.now()
        account.last_seen_at = timezone.now()
        account.save(
            update_fields=[
                "status",
                "error_message",
                "phone_number",
                "provider_account_id",
                "provider_session_id",
                "last_connected_at",
                "last_seen_at",
                "updated_at",
            ]
        )
        session.status = WhatsAppConnectionSession.Status.CONNECTED
        session.connected_at = timezone.now()
        session.save(update_fields=["session_id", "qr_expires_at", "status", "connected_at", "updated_at"])
        cache.delete(_qr_cache_key(session.pk))

    @classmethod
    def get_status(cls, account) -> dict:
        """Poll for the current connection state, reconciling with the provider if a session is still pending."""
        session = cls._latest_session(account)

        if account.status == account.Status.CONNECTED or session is None:
            return {"status": account.status, "message": account.error_message or "", "expires_at": None}

        if session.status == WhatsAppConnectionSession.Status.QR_READY and cls._is_expired(session):
            cls._expire(account, session)
            return {"status": account.status, "message": "QR code expired. Generate a new QR code.", "expires_at": None}

        if session.status in (WhatsAppConnectionSession.Status.CONNECTING, WhatsAppConnectionSession.Status.QR_READY):
            provider = get_provider(account)
            try:
                result = provider.get_connection_status(account, session.session_id)
            except ProviderError as exc:
                message = _human_error(exc)
                account.status = account.Status.ERROR
                account.error_message = message
                account.save(update_fields=["status", "error_message", "updated_at"])
                return {"status": account.status, "message": message, "expires_at": None}

            outcome = cls._apply_session_result(account, session, result)
            return {"status": outcome["status"], "message": outcome["message"], "expires_at": outcome["expires_at"]}

        return {"status": account.status, "message": account.error_message or "", "expires_at": session.qr_expires_at}

    @classmethod
    def get_qr(cls, account) -> dict:
        session = cls._latest_session(account)
        if session is None or session.status not in (
            WhatsAppConnectionSession.Status.CONNECTING,
            WhatsAppConnectionSession.Status.QR_READY,
        ):
            return {"success": False, "message": "No QR code is available. Start a new connection first."}

        if cls._is_expired(session):
            cls._expire(account, session)
            return {"success": False, "message": "QR code expired. Generate a new QR code."}

        qr_data = cache.get(_qr_cache_key(session.pk))
        if not qr_data:
            provider = get_provider(account)
            try:
                result = provider.get_qr(account, session.session_id)
            except ProviderError as exc:
                return {"success": False, "message": _human_error(exc)}
            qr_data = result.qr_data
            ttl = max(int((result.expires_at - timezone.now()).total_seconds()), 1) if result.expires_at else settings.WHATSAPP_QR_SESSION_TTL_SECONDS
            cache.set(_qr_cache_key(session.pk), qr_data, timeout=ttl)

        return {"success": True, "qr_data": qr_data, "expires_at": session.qr_expires_at, "status": account.status}

    @staticmethod
    def _is_expired(session) -> bool:
        return bool(session.qr_expires_at and session.qr_expires_at <= timezone.now())

    @staticmethod
    def _expire(account, session) -> None:
        account.status = account.Status.EXPIRED
        account.save(update_fields=["status", "updated_at"])
        session.status = WhatsAppConnectionSession.Status.EXPIRED
        session.save(update_fields=["status", "updated_at"])
        cache.delete(_qr_cache_key(session.pk))

    @classmethod
    def _is_stale(cls, session) -> bool:
        """Whether a pending session is past any hope of still being scanned."""
        if cls._is_expired(session):
            return True
        # Fallback for sessions the provider never handed an expiry for (and
        # for CONNECTING ones, which have none until a result lands): age them
        # out against the configured QR TTL. That setting defaults to an
        # effectively infinite value, so an unconfigured deployment keeps
        # today's behaviour -- never stale by age -- instead of blowing up on
        # the out-of-range timedelta.
        try:
            cutoff = timezone.now() - timedelta(seconds=settings.WHATSAPP_QR_SESSION_TTL_SECONDS)
        except OverflowError:
            return False
        return session.created_at <= cutoff

    @classmethod
    def expire_stale(cls, accounts) -> int:
        """
        Move abandoned pairing attempts to EXPIRED. Returns how many were expired.

        ``get_status`` expires only the single account it is polling, and the
        connection wizard is its only caller -- so closing the wizard without
        scanning pins the account at QR_READY indefinitely. The devices page
        renders that as a permanent "Waiting for scan" spinner offering
        neither Reconnect nor Delete, leaving no way to clear the row. This is
        the sweep that reconciles those, and it is why the devices list calls
        it before serializing.

        Deliberately database-only: it runs on every devices-page load, and a
        provider round-trip per stale account would put N blocking HTTP calls
        in the request path. That leaves the provider-side session to its own
        timeout -- the same trade-off ``_expire`` already makes when polling.
        """
        model = accounts.model
        pending = list(accounts.filter(status__in=(model.Status.CONNECTING, model.Status.QR_READY)))
        if not pending:
            return 0

        # One query for every candidate's latest session, rather than
        # _latest_session (and so one query) per account.
        latest: dict[int, WhatsAppConnectionSession] = {}
        for session in WhatsAppConnectionSession.objects.filter(
            whatsapp_account__in=pending
        ).order_by("whatsapp_account_id", "-created_at"):
            latest.setdefault(session.whatsapp_account_id, session)

        stale = [
            session
            for account in pending
            # No session row yet means start_connection is still mid-flight --
            # it sets the account status before creating the session -- so an
            # account without one is racing, not stale.
            if (session := latest.get(account.pk)) is not None and cls._is_stale(session)
        ]
        if not stale:
            return 0

        now = timezone.now()
        account_ids = [session.whatsapp_account_id for session in stale]
        # updated_at is auto_now, which queryset.update() does not trigger --
        # hence setting it by hand rather than saving each row individually.
        WhatsAppConnectionSession.objects.filter(pk__in=[s.pk for s in stale]).update(
            status=WhatsAppConnectionSession.Status.EXPIRED, updated_at=now,
        )
        model.objects.filter(pk__in=account_ids).update(status=model.Status.EXPIRED, updated_at=now)
        cache.delete_many([_qr_cache_key(s.pk) for s in stale])
        logger.info("Expired %s stale WhatsApp pairing attempt(s): account_ids=%s", len(account_ids), account_ids)
        return len(account_ids)

    @classmethod
    def disconnect(cls, account) -> dict:
        account.status = account.Status.DISCONNECTING
        account.save(update_fields=["status", "updated_at"])

        session = cls._latest_session(account)
        provider = get_provider(account)
        try:
            provider.disconnect(account, account.provider_session_id)
        except ProviderError as exc:
            logger.warning("Provider disconnect failed for account_id=%s: %s", account.pk, exc.message)

        account.status = account.Status.DISCONNECTED
        account.error_message = ""
        account.save(update_fields=["status", "error_message", "updated_at"])

        if session and session.status not in (
            WhatsAppConnectionSession.Status.EXPIRED,
            WhatsAppConnectionSession.Status.ERROR,
        ):
            session.status = WhatsAppConnectionSession.Status.DISCONNECTED
            session.save(update_fields=["status", "updated_at"])
            cache.delete(_qr_cache_key(session.pk))

        return {"success": True, "status": account.status, "message": "WhatsApp account disconnected."}

    @staticmethod
    def test_credentials(account) -> dict:
        """Validate credentials without mutating account/session status (the wizard's "Test Connection" button)."""
        provider = get_provider(account)
        try:
            result = provider.get_account_info(account)
        except ProviderError as exc:
            return {"success": False, "message": _human_error(exc)}

        if not result.connected:
            return {"success": False, "message": result.error or "Unable to verify these credentials."}
        return {
            "success": True,
            "message": "Credentials are valid.",
            "phone_number": result.phone_number,
            "display_name": result.display_name,
        }

    @classmethod
    def handle_connection_webhook(cls, provider_name: str, event: ConnectionWebhookEvent) -> WhatsAppConnectionSession | None:
        session = (
            WhatsAppConnectionSession.objects.filter(provider=provider_name, session_id=event.session_id)
            .order_by("-created_at")
            .first()
        )
        if session is None:
            logger.warning("Connection webhook for unknown session_id=%s provider=%s", event.session_id, provider_name)
            return None

        account = session.whatsapp_account

        if event.status == "CONNECTED":
            from ..providers.base import ConnectionSessionResult

            cls._mark_connected(
                account,
                session,
                ConnectionSessionResult(
                    status="CONNECTED",
                    phone_number=event.phone_number,
                    display_name=event.display_name,
                    provider_account_id=event.provider_account_id,
                ),
            )
        elif event.status in ("ERROR", "EXPIRED", "DISCONNECTED"):
            account.status = event.status
            account.error_message = event.error or ""
            account.save(update_fields=["status", "error_message", "updated_at"])
            session.status = event.status
            session.error_message = event.error or ""
            session.save(update_fields=["status", "error_message", "updated_at"])
            cache.delete(_qr_cache_key(session.pk))

        return session

    @staticmethod
    def _require_connected(account) -> SendResult | None:
        if account.status != account.Status.CONNECTED:
            return SendResult(success=False, error="WhatsApp account is not connected.")
        return None

    @classmethod
    def send_text_message(cls, account, to: str, body: str, operation_id: str = "") -> SendResult:
        blocked = cls._require_connected(account)
        if blocked:
            return blocked
        return get_provider(account).send_text_message(account, to, body, operation_id=operation_id)

    @classmethod
    def send_media_message(cls, account, to, media_type, media_link, caption=None, filename=None,
                           operation_id: str = "") -> SendResult:
        blocked = cls._require_connected(account)
        if blocked:
            return blocked
        return get_provider(account).send_media_message(
            account, to, media_type, media_link, caption, filename, operation_id=operation_id,
        )

    @classmethod
    def get_send_operation_status(cls, account, operation_id: str) -> dict | None:
        """
        What became of one send, by its operation id -- for the recovery sweep.

        Returns None when the provider offers no such lookup (see
        WhatsAppProvider.get_send_operation_status). Never raises: a provider
        that blows up here is reported as unreachable so the sweep marks the
        message UNKNOWN rather than failing the whole run.
        """
        try:
            return get_provider(account).get_send_operation_status(account, operation_id)
        except ProviderError as exc:
            logger.warning(
                "Operation status lookup failed for account_id=%s: %s", account.pk, exc.message,
            )
            return {"unreachable": True}

    @classmethod
    def supports_interactive_buttons(cls, account) -> bool:
        """Whether this account's connection can deliver buttons at all."""
        return bool(get_provider(account).supports_interactive_buttons)

    @classmethod
    def capabilities(cls, account) -> dict:
        """
        What this account's connection can actually deliver.

        The one way anything outside ``providers/`` asks the question -- the
        composer, the campaign pre-flight and the send path all read this, so
        there is a single answer rather than three that can drift apart.
        """
        return get_provider(account).capabilities()

    @classmethod
    def supports(cls, account, capability: str) -> bool:
        """
        One capability by name, e.g. ``supports(account, "polls")``.

        Unknown names are False rather than an error: a caller asking about a
        capability this deployment has never heard of is asking about
        something the connection certainly cannot do.
        """
        return bool(cls.capabilities(account).get(capability, False))

    @classmethod
    def send_interactive_message(cls, account, to: str, body: str, buttons: list,
                                 operation_id: str = "") -> SendResult:
        blocked = cls._require_connected(account)
        if blocked:
            return blocked
        return get_provider(account).send_interactive_message(
            account, to, body, buttons, operation_id=operation_id,
        )

    @classmethod
    def send_list_message(cls, account, to: str, body: str, list_content: dict,
                          operation_id: str = "") -> SendResult:
        blocked = cls._require_connected(account)
        if blocked:
            return blocked
        return get_provider(account).send_list_message(
            account, to, body, list_content, operation_id=operation_id,
        )

    @classmethod
    def send_poll_message(cls, account, to: str, poll_content: dict, operation_id: str = "") -> SendResult:
        blocked = cls._require_connected(account)
        if blocked:
            return blocked
        return get_provider(account).send_poll_message(account, to, poll_content, operation_id=operation_id)

    @classmethod
    def send_template_message(cls, account, to, template_name, language_code, components=None,
                              operation_id: str = "") -> SendResult:
        blocked = cls._require_connected(account)
        if blocked:
            return blocked
        return get_provider(account).send_template_message(
            account, to, template_name, language_code, components, operation_id=operation_id,
        )

    # -- Lookups --------------------------------------------------------------
    #
    # A send answers "not connected" with a failed SendResult because a send
    # has an outcome to record. A lookup has nothing to record, so these
    # raise, and the caller (a view or a task) decides what to tell the user.

    @staticmethod
    def _require_connected_or_raise(account) -> None:
        if account.status != account.Status.CONNECTED:
            raise ProviderError("WhatsApp account is not connected.", code="not_connected")

    @classmethod
    def check_numbers(cls, account, phones: list[str]):
        """Which of ``phones`` are on WhatsApp -- see WhatsAppProvider.check_numbers."""
        cls._require_connected_or_raise(account)
        return get_provider(account).check_numbers(account, phones)

    @classmethod
    def list_groups(cls, account):
        cls._require_connected_or_raise(account)
        return get_provider(account).list_groups(account)

    @classmethod
    def get_group_members(cls, account, group_ids: list[str]):
        cls._require_connected_or_raise(account)
        return get_provider(account).get_group_members(account, group_ids)
