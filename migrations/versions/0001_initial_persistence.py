"""create oauth_state and provider_token tables

Revision ID: 0001_initial_persistence
Revises:
Create Date: 2026-06-07

The encrypted token columns are plain TEXT at the database level; encryption is
applied in the application, so the schema stays decoupled from the crypto
implementation.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_initial_persistence"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_state",
        sa.Column("state", sa.String(length=64), primary_key=True),
        sa.Column("iss", sa.String(length=512), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_oauth_state_expires_at", "oauth_state", ["expires_at"])
    op.create_table(
        "provider_token",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("patient_fhir_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("iss", sa.String(length=512), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "patient_fhir_id",
            "provider",
            "iss",
            name="uq_provider_token_identity",
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_token")
    op.drop_index("ix_oauth_state_expires_at", table_name="oauth_state")
    op.drop_table("oauth_state")
