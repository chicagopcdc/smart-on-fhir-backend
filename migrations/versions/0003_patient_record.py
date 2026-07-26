"""attach connections to a patient record

Revision ID: 0003_patient_record
Revises: 0002_app_session
Create Date: 2026-07-26

Adds the patient record a connection belongs to: ``patient_id`` on the stored
token and the session that reads it, and ``link_patient_id`` on an in-flight
authorization to carry the record a new connection should join. See
``ProviderToken`` for why a record needs an identifier of our own.

There is no patient table. A record has no state beyond its id, so letting its
existence follow from the connections that carry it means it disappears with the
last of them rather than needing a lifecycle of its own.
"""

from __future__ import annotations

import secrets

import sqlalchemy as sa

from alembic import op

revision = "0003_patient_record"
down_revision = "0002_app_session"
branch_labels = None
depends_on = None


def _new_patient_id() -> str:
    """Mint a record id.

    Spelled out here rather than imported from ``app.auth.models``: a migration
    has to keep producing the values it produced on the day it ran, and importing
    application code would let a later refactor rewrite history.
    """
    return "pat_" + secrets.token_urlsafe(24)


def upgrade() -> None:
    # Added nullable, backfilled, then tightened. Adding the column NOT NULL
    # outright would fail against any database that already holds tokens, which
    # is every deployment this migration is for.
    op.add_column(
        "provider_token", sa.Column("patient_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "app_session", sa.Column("patient_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "oauth_state", sa.Column("link_patient_id", sa.String(length=64), nullable=True)
    )

    connection = op.get_bind()

    # Every existing connection becomes a record of its own. Grouping any of them
    # together would be a guess about which of them are the same person, and the
    # data cannot answer that: only the person authorizing both can.
    tokens = connection.execute(
        sa.text("SELECT id FROM provider_token ORDER BY id")
    ).fetchall()
    for (token_id,) in tokens:
        connection.execute(
            sa.text("UPDATE provider_token SET patient_id = :pid WHERE id = :id"),
            {"pid": _new_patient_id(), "id": token_id},
        )

    # A live session inherits the record of the connection it was issued for, so
    # a bearer already in a browser keeps working across the upgrade.
    connection.execute(
        sa.text(
            """
            UPDATE app_session
               SET patient_id = (
                     SELECT t.patient_id
                       FROM provider_token t
                      WHERE t.patient_fhir_id = app_session.patient_fhir_id
                        AND t.provider = app_session.provider
                        AND t.iss = app_session.iss
                   )
            """
        )
    )
    # Any session whose connection has since been deleted has nothing to inherit.
    # It is about to expire on its own, but it cannot be left null, and it must
    # not be handed a record that exists — that would grant it someone's data.
    orphans = connection.execute(
        sa.text("SELECT session_id FROM app_session WHERE patient_id IS NULL")
    ).fetchall()
    for (session_id,) in orphans:
        connection.execute(
            sa.text(
                "UPDATE app_session SET patient_id = :pid WHERE session_id = :sid"
            ),
            {"pid": _new_patient_id(), "sid": session_id},
        )

    op.alter_column("provider_token", "patient_id", nullable=False)
    op.alter_column("app_session", "patient_id", nullable=False)

    # A connection is unique within a record, not globally. The same EHR account
    # may therefore be held under two records, each with its own token, which is
    # what stops a caller who authenticates to one connection from landing on a
    # record someone else assembled around it. The backfill above gave every
    # existing row its own record, so widening the constraint cannot collide.
    op.drop_constraint(
        "uq_provider_token_identity", "provider_token", type_="unique"
    )
    op.create_unique_constraint(
        "uq_provider_token_record_identity",
        "provider_token",
        ["patient_id", "patient_fhir_id", "provider", "iss"],
    )

    op.create_index("ix_provider_token_patient_id", "provider_token", ["patient_id"])
    op.create_index("ix_app_session_patient_id", "app_session", ["patient_id"])


def downgrade() -> None:
    connection = op.get_bind()

    # Going back means the same EHR account can once again exist only once across
    # the whole table. If two records hold it, one of them would have to lose a
    # connection to fit, so refuse rather than pick a winner: the operator can
    # decide which record keeps it and delete the other, and then downgrade.
    duplicated = connection.execute(
        sa.text(
            """
            SELECT patient_fhir_id, provider, iss, count(*) AS held_by
              FROM provider_token
             GROUP BY patient_fhir_id, provider, iss
            HAVING count(*) > 1
            """
        )
    ).fetchall()
    if duplicated:
        listed = ", ".join(
            f"{row.provider}/{row.patient_fhir_id} held by {row.held_by} records"
            for row in duplicated
        )
        raise RuntimeError(
            "cannot downgrade: the pre-0003 schema allows an EHR account to be "
            f"held by only one patient record, but {listed}. Delete the "
            "connections that should not survive, then downgrade again."
        )

    op.drop_index("ix_app_session_patient_id", table_name="app_session")
    op.drop_index("ix_provider_token_patient_id", table_name="provider_token")
    op.drop_constraint(
        "uq_provider_token_record_identity", "provider_token", type_="unique"
    )
    op.create_unique_constraint(
        "uq_provider_token_identity",
        "provider_token",
        ["patient_fhir_id", "provider", "iss"],
    )
    op.drop_column("oauth_state", "link_patient_id")
    op.drop_column("app_session", "patient_id")
    op.drop_column("provider_token", "patient_id")
