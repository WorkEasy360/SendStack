"""Thin, app-facing wrapper around the connection manager for WhatsApp accounts."""

from .connections.manager import ConnectionManager
from .models import WhatsAppAccount


class WhatsAppConnectionService:

    @staticmethod
    def start_connection(account: WhatsAppAccount) -> dict:
        if account.status == WhatsAppAccount.Status.CONNECTED:
            return {
                "success": False,
                "status": account.status,
                "message": "WhatsApp account is already connected.",
                "session_id": None,
                "expires_at": None,
            }
        return ConnectionManager.start_connection(account)

    @staticmethod
    def refresh_qr(account: WhatsAppAccount) -> dict:
        return ConnectionManager.refresh_qr(account)

    @staticmethod
    def get_status(account: WhatsAppAccount) -> dict:
        return ConnectionManager.get_status(account)

    @staticmethod
    def get_qr(account: WhatsAppAccount) -> dict:
        return ConnectionManager.get_qr(account)

    @staticmethod
    def expire_stale(accounts) -> int:
        return ConnectionManager.expire_stale(accounts)

    @staticmethod
    def disconnect(account: WhatsAppAccount) -> dict:
        return ConnectionManager.disconnect(account)

    @staticmethod
    def test_credentials(account: WhatsAppAccount) -> dict:
        return ConnectionManager.test_credentials(account)
