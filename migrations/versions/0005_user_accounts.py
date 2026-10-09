"""add user accounts, so a patient record outlives the session that made it

Revision ID: 0005_user_accounts
Revises: 0004_connection_last_used
Create Date: 2026-09-14

Adds the authentication layer: an ``app_user`` row per person who signs in, a
``login_state`` row per sign-in in flight, and the columns that tie the two to
what was already here — ``app_session.user_id`` and ``oauth_state.user_id``.

The interesting half is the nullability change on ``app_session``. Until now a
session could only exist because an authorization had just completed, so it
always named a connection and the record that connection sat on. A session can
now also come from signing in, which happens before any of that is true, so
``patient_id``, ``patient_fhir_id``, ``provider`` and ``iss`` become nullable.
Widening a column is safe for rows already stored: every existing session was
minted the old way and keeps all four.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_user_accounts"
down_revision = "0004_connection_last_used"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.String(length=64), primary_key=True),
        # The identity provider that vouched for this person, and their subject
        # at it. Unique together and never apart: a subject only means anything
        # beside the issuer that minted it.
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        # 320 is the longest an email address can be (RFC 3696). Nullable
        # because a provider is not obliged to release one, and it is never what
        # identifies the account in any case.
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        # The record this account owns. Null until its first provider is
        # connected — a record exists only for as long as connections hang off
        # it, so there is nothing to point at before then.
        sa.Column("patient_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="uq_app_user_identity"),
    )
    op.create_index("ix_app_user_patient_id", "app_user", ["patient_id"])

    op.create_table(
        "login_state",
        sa.Column("state", sa.String(length=64), primary_key=True),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_login_state_expires_at", "login_state", ["expires_at"])

    op.add_column(
        "app_session", sa.Column("user_id", sa.String(length=64), nullable=True)
    )
    op.create_index("ix_app_session_user_id", "app_session", ["user_id"])
    op.add_column(
        "oauth_state", sa.Column("user_id", sa.String(length=64), nullable=True)
    )

    # A session minted by signing in names none of these. Widened rather than
    # defaulted: a placeholder patient_id would be a record id that matches
    # nothing, and the one thing a session's record must never be is a value
    # that looks real.
    for column, type_ in (
        ("patient_id", sa.String(length=64)),
        ("patient_fhir_id", sa.String(length=128)),
        ("provider", sa.String(length=64)),
        ("iss", sa.String(length=512)),
    ):
        op.alter_column("app_session", column, existing_type=type_, nullable=True)


def downgrade() -> None:
    # Sessions that came from signing in cannot exist in the pre-0005 schema:
    # the columns they leave null are required there, and there is nothing
    # truthful to fill them with. They are deleted rather than given stand-in
    # values, which would be a bearer pointing at a record that does not exist.
    # Whoever held one signs in again — or, after this downgrade, connects a
    # provider again, since there is no signing in to be done.
    op.execute(sa.text("DELETE FROM app_session WHERE patient_id IS NULL"))

    for column, type_ in (
        ("patient_id", sa.String(length=64)),
        ("patient_fhir_id", sa.String(length=128)),
        ("provider", sa.String(length=64)),
        ("iss", sa.String(length=512)),
    ):
        op.alter_column("app_session", column, existing_type=type_, nullable=False)

    op.drop_column("oauth_state", "user_id")
    op.drop_index("ix_app_session_user_id", table_name="app_session")
    op.drop_column("app_session", "user_id")

    op.drop_index("ix_login_state_expires_at", table_name="login_state")
    op.drop_table("login_state")
    op.drop_index("ix_app_user_patient_id", table_name="app_user")
    op.drop_table("app_user")
