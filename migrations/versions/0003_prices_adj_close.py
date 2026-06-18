"""prices.adj_close — total-return-adjusted close

Adds a nullable ``adj_close NUMERIC`` column to ``prices`` so the prices ETL
(#20, etl/sources/prices.py) can store BOTH the raw tape close (``close``) and
yfinance's dividend-/split-back-adjusted, dividend-reinvested TOTAL-RETURN close
(``adj_close``). The macro-context sub-panel renders ``adj_close`` (raw close
would show TLT "falling" purely from coupons). Nullable + additive: existing
rows keep ``adj_close = NULL`` and the ETL upserts it going forward; no other
table or column changes.

Revision ID: 0003_prices_adj_close
Revises: 0002_data_tables
Create Date: 2026-06-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_prices_adj_close"
down_revision: Union[str, None] = "0002_data_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("prices", sa.Column("adj_close", sa.Numeric, nullable=True))


def downgrade() -> None:
    op.drop_column("prices", "adj_close")
