"""create app_session table

Revision ID: 0002_app_session
Revises: 0001_initial_persistence
Create Date: 2026-07-01

Records the patient/provider/issuer a caller authenticated as, keyed by an
opaque session id, so resource access is scoped to that patient rather than to a
patient id supplied on the request.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_app_session"
down_revision = "0001_initial_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_session",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("patient_fhir_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("iss", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_app_session_expires_at", "app_session", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_app_session_expires_at", table_name="app_session")
    op.drop_table("app_session")
