"""normalize GVZ/OVX atm_iv to decimal fraction (#29)

``vol_indices.py`` originally stored the raw CBOE index level (e.g. 29.58 for
a 29.58% vol reading) directly in ``atm_iv``.  The Panel D display formats
``atm_iv`` with ``:.1%`` (×100), so the card showed 2 958.0 % instead of
29.58 %.  The correct convention, shared by every other ``atm_iv`` row in
``iv_metrics``, is a decimal fraction (0.2958 == 29.58 %).

This migration divides all existing GVZ/OVX ``atm_iv`` values by 100.  The
``> 1.0`` guard makes it safe to run twice: after the first run, every value
is ≤ 1.0 (commodity vol never exceeds ~100 % ≡ 1.0), so the WHERE clause
matches nothing on a re-run.

``iv_rank`` / ``iv_percentile`` are computed via ``(current − min)/(max − min)``
which is scale-invariant: dividing every value by 100 leaves all stored
rank / percentile figures unchanged and correct.

Revision ID: 0005_normalize_gvz_ovx
Revises: 0004_etl_run_log
Create Date: 2026-06-26
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005_normalize_gvz_ovx"
down_revision: Union[str, None] = "0004_etl_run_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE iv_metrics
           SET atm_iv = atm_iv / 100.0
         WHERE symbol IN ('GVZ', 'OVX')
           AND atm_iv IS NOT NULL
           AND atm_iv > 1.0
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE iv_metrics
           SET atm_iv = atm_iv * 100.0
         WHERE symbol IN ('GVZ', 'OVX')
           AND atm_iv IS NOT NULL
           AND atm_iv < 1.0
        """
    )
