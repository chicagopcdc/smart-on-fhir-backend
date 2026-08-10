"""record when a connection was last read

Revision ID: 0004_connection_last_used
Revises: 0003_patient_record
Create Date: 2026-08-05

``provider_token`` is the one table with no way to shed rows: ``oauth_state`` and
``app_session`` both expire, but a connection is meant to outlive a session, so
there is nothing on the row that says when it has stopped being worth keeping.
This adds it — the last time anything read the record the connection hangs off,
which is what separates one somebody is using from one authorized and abandoned.

Left nullable rather than backfilled to ``created_at``. NULL is its own fact,
"never read since it was authorized", and the sweep coalesces the two rather than
losing the difference.

Deliberately unindexed, unlike the expiry on the two TTL tables. Those are
filtered on directly, so an index turns their sweep into a range scan; this one
is only ever read through ``coalesce(last_used_at, created_at)``, which a plain
b-tree on the column cannot serve, and it sits among an ``OR`` and a correlated
``NOT EXISTS`` that keep the statement a scan anyway. An index would be paid for
on every read that stamps the column and would buy nothing back.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_connection_last_used"
down_revision = "0003_patient_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_token", sa.Column("last_used_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("provider_token", "last_used_at")
