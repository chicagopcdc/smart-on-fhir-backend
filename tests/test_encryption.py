"""Token encryption: round-trip, key rotation, and ciphertext-at-rest.

The first tests pin the crypto helper in isolation; the last drives a real
ProviderToken through SQLAlchemy to prove the database stores ciphertext while
application code reads back plaintext.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet
from sqlalchemy import text

import crypto
from models import ProviderToken
from settings import get_settings


def test_fernet_round_trip():
    ciphertext = crypto.encrypt("an-access-token")

    assert ciphertext != "an-access-token"
    assert ciphertext.startswith("gAAAAA")  # Fernet token version prefix
    assert crypto.decrypt(ciphertext) == "an-access-token"


def test_rotated_key_list_still_decrypts_old_ciphertext(monkeypatch):
    old_ciphertext = crypto.encrypt("rotate-me")
    old_key = os.environ["TOKEN_ENCRYPTION_KEY"]
    new_key = Fernet.generate_key().decode()

    # Rotate: prepend the new key, keep the old one for decryption.
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", f"{new_key},{old_key}")
    get_settings.cache_clear()
    try:
        # Tokens written under the old key remain readable.
        assert crypto.decrypt(old_ciphertext) == "rotate-me"

        # New ciphertext is produced by the new primary key alone.
        fresh = crypto.encrypt("rotate-me")
        assert Fernet(new_key.encode()).decrypt(fresh.encode()) == b"rotate-me"
    finally:
        get_settings.cache_clear()  # monkeypatch restores the env afterwards


async def test_encrypted_column_stores_ciphertext(db_session):
    token = ProviderToken(
        patient_fhir_id="patient-123",
        provider="EPIC_SANDBOX",
        iss="https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
        access_token="secret-access",
        refresh_token="secret-refresh",
        scope="patient/*.read",
    )
    db_session.add(token)
    await db_session.commit()

    # Read the raw column with textual SQL, bypassing the EncryptedString type:
    # what physically sits in the database must be ciphertext, not the token.
    raw = (
        await db_session.execute(
            text("SELECT access_token FROM provider_token WHERE id = :id"),
            {"id": token.id},
        )
    ).scalar_one()
    assert raw != "secret-access"
    assert raw.startswith("gAAAAA")
    assert crypto.decrypt(raw) == "secret-access"

    # Reading back through the ORM transparently decrypts.
    await db_session.refresh(token)
    assert token.access_token == "secret-access"
    assert token.refresh_token == "secret-refresh"
