"""Encryption helpers for provider credentials at rest (access tokens, app secrets)."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = settings.WHATSAPP_TOKEN_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured(
            "WHATSAPP_TOKEN_ENCRYPTION_KEY must be set to store WhatsApp provider credentials."
        )
    return Fernet(key)


def encrypt_secret(raw_value: str) -> str:
    return _fernet().encrypt(raw_value.encode("utf-8")).decode("utf-8")


def decrypt_secret(encrypted_value: str) -> str | None:
    if not encrypted_value:
        return None
    try:
        return _fernet().decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
