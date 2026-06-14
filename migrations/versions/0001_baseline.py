"""baseline — no schema yet

Establishes the Alembic version history. Real tables (prices, macro_metrics,
inventories, cot, iv_metrics, curve_shape, sentiment_*) arrive in ticket #2.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-14
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
