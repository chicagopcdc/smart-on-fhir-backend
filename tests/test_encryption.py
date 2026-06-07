"""Token encryption: round-trip, and ciphertext-at-rest through the column type.

The first test pins the crypto helper in isolation; the second drives a real
ProviderToken through SQLAlchemy to prove the database stores ciphertext while
application code reads back plaintext.
"""

from __future__ import annotations

from sqlalchemy import text

import crypto
from models import ProviderToken


def test_fernet_round_trip():
    ciphertext = crypto.encrypt("an-access-token")

    assert ciphertext != "an-access-token"
    assert ciphertext.startswith("gAAAAA")  # Fernet token version prefix
    assert crypto.decrypt(ciphertext) == "an-access-token"


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
