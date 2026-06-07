"""Symmetric encryption for tokens at rest.

Access and refresh tokens grant entry to a patient's clinical data, so they are
encrypted before they reach the database and decrypted only when read back. We
use Fernet (AES-128-CBC with an HMAC authenticator) from ``cryptography``, keyed
from the ``TOKEN_ENCRYPTION_KEY`` setting.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from settings import get_settings


class TokenEncryptionError(RuntimeError):
    """Encryption or decryption failed (missing/invalid key, or corrupt data)."""


@lru_cache
def _fernet() -> Fernet:
    """Build the Fernet cipher from the configured key, once."""
    try:
        return Fernet(get_settings().token_encryption_key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is missing or is not a valid Fernet key"
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a string, returning URL-safe base64 ciphertext."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext produced by :func:`encrypt`."""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise TokenEncryptionError(
            "Could not decrypt value with the configured key"
        ) from exc
