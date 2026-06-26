"""etl_manual_trigger — operator-initiated ETL run (#29)

Provides the shared Postgres IPC channel between the dashboard's
``POST /health/trigger`` button and the ETL scheduler poll loop.  The
dashboard inserts a row; the scheduler polls for unprocessed rows on each
tick, dispatches all slots, then marks the row processed.

Append-only audit log — no UNIQUE constraint (each button click is its own
row; multiple processed rows in history are the normal state).  The single
index on ``processed_at`` makes the scheduler's ``WHERE processed_at IS NULL``
query fast on a small table.

Revision ID: 0006_etl_manual_trigger
Revises: 0005_normalize_gvz_ovx
Create Date: 2026-06-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_etl_manual_trigger"
down_revision: Union[str, None] = "0005_normalize_gvz_ovx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "etl_manual_trigger",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("slot", sa.Text, nullable=False, server_default=sa.text("'all'")),
    )
    op.create_index(
        "ix_etl_manual_trigger_processed_at",
        "etl_manual_trigger",
        ["processed_at"],
    )


def downgrade() -> None:
    op.drop_table("etl_manual_trigger")
