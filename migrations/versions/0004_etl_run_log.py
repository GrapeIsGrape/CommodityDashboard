"""etl_run_log — scheduler run-log / heartbeat (#24)

Observability table the scheduler's ``dispatch_slot`` (#23) writes one row to per
source per dispatch: ``success`` on return, ``failure`` (with a short redacted
``detail``) on raise, ``skipped`` when the session-window guard skips a guarded
slot. A silently-failing or never-firing source is otherwise visible only in the
container log stream; this makes it queryable from the DB and surfaced on
``/health``.

Time-stamped and append-friendly like the ``0002`` data tables: a named natural
key UNIQUE constraint on ``(slot, source, run_date)`` lets the scheduler run
``INSERT ... ON CONFLICT DO UPDATE`` so a same-day re-dispatch (cron retry /
manual catch-up) overwrites the one logical run instead of appending duplicate
heartbeats. ``run_started_at`` / ``run_finished_at`` / ``status`` / ``detail``
are data columns, deliberately NOT in the key.

Revision ID: 0004_etl_run_log
Revises: 0003_prices_adj_close
Create Date: 2026-06-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_etl_run_log"
down_revision: Union[str, None] = "0003_prices_adj_close"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "etl_run_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("slot", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("run_date", sa.Date, nullable=False),
        sa.Column("run_started_at", sa.DateTime(timezone=True)),
        sa.Column("run_finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "slot", "source", "run_date", name="uq_etl_run_log_slot_source_date"
        ),
    )
    op.create_index(
        "ix_etl_run_log_source_run_date",
        "etl_run_log",
        ["source", sa.text("run_date DESC")],
    )


def downgrade() -> None:
    op.drop_table("etl_run_log")
