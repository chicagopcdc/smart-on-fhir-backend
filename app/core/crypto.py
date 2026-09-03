"""Symmetric encryption for tokens at rest.

Access and refresh tokens grant entry to a patient's clinical data, so they are
encrypted before they reach the database and decrypted only when read back. We
use Fernet (AES-128-CBC with an HMAC authenticator) from ``cryptography``.

``TOKEN_ENCRYPTION_KEY`` may hold several comma-separated keys: MultiFernet
encrypts with the first and tries every key on decryption, so the key can be
rotated (prepend the new one) without invalidating already-stored tokens.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


class TokenEncryptionError(RuntimeError):
    """Encryption or decryption failed (missing/invalid key, or corrupt data)."""


# Public because a caller with a key string but no configured process — a check on
# the environment a clean checkout gets — needs to know whether that string builds
# a cipher at all. Cached by the key material itself, so a changed key can never
# serve a stale cipher (which matters when tests reconfigure keys at runtime).
@lru_cache
def build_cipher(keys_csv: str) -> MultiFernet:
    keys = [key.strip() for key in keys_csv.split(",") if key.strip()]
    try:
        return MultiFernet([Fernet(key.encode("utf-8")) for key in keys])
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is missing or contains an invalid Fernet key"
        ) from exc


def _cipher() -> MultiFernet:
    return build_cipher(get_settings().token_encryption_key)


def encrypt(plaintext: str) -> str:
    """Encrypt a string with the primary key, returning base64 ciphertext."""
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext produced under any of the configured keys."""
    try:
        return _cipher().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise TokenEncryptionError(
            "Could not decrypt value with any configured key"
        ) from exc
