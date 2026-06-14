"""data tables — prices, macro_metrics, inventories, cot, iv_metrics,
curve_shape, and placeholder sentiment_articles / sentiment_scores.

Phase 1 persistence layer. Every table is time-stamped and append-only: a
named unique constraint on each table's natural key lets Phase 2+ ETL jobs
run `INSERT ... ON CONFLICT (<natural key>) DO UPDATE` so a same-date re-run
upserts in place instead of duplicating. No business data is written here.

Revision ID: 0002_data_tables
Revises: 0001_baseline
Create Date: 2026-06-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_data_tables"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    # Panel C/D macro sub-panel — daily futures/spot prices.
    op.create_table(
        "prices",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("open", sa.Numeric),
        sa.Column("high", sa.Numeric),
        sa.Column("low", sa.Numeric),
        sa.Column("close", sa.Numeric),
        sa.Column("volume", sa.BigInteger),
        sa.Column("source", sa.Text, nullable=False),
        _created_at(),
        sa.UniqueConstraint("symbol", "date", name="uq_prices_symbol_date"),
    )
    op.create_index(
        "ix_prices_symbol_date", "prices", ["symbol", sa.text("date DESC")]
    )

    # Panel A — FRED macro series (DXY, real yields, CPI/PCE/PPI, VIX, ...).
    op.create_table(
        "macro_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("series_id", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("value", sa.Numeric),
        sa.Column("source", sa.Text, nullable=False, server_default="FRED"),
        _created_at(),
        sa.UniqueConstraint("series_id", "date", name="uq_macro_metrics_series_date"),
    )
    op.create_index(
        "ix_macro_metrics_series_date",
        "macro_metrics",
        ["series_id", sa.text("date DESC")],
    )

    # Panel B — EIA / USDA inventory & fundamentals series. `source` is part of
    # the natural key because EIA and USDA can both publish a given series_id.
    op.create_table(
        "inventories",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("series_id", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("value", sa.Numeric),
        sa.Column("unit", sa.Text),
        _created_at(),
        sa.UniqueConstraint(
            "source", "series_id", "date", name="uq_inventories_source_series_date"
        ),
    )
    op.create_index(
        "ix_inventories_series_date",
        "inventories",
        ["series_id", sa.text("date DESC")],
    )

    # Panel C — CFTC Commitments of Traders positioning.
    op.create_table(
        "cot",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("report_date", sa.Date, nullable=False),
        sa.Column("noncomm_long", sa.BigInteger),
        sa.Column("noncomm_short", sa.BigInteger),
        sa.Column("comm_long", sa.BigInteger),
        sa.Column("comm_short", sa.BigInteger),
        sa.Column("open_interest", sa.BigInteger),
        sa.Column("source", sa.Text, nullable=False),
        _created_at(),
        sa.UniqueConstraint("symbol", "report_date", name="uq_cot_symbol_report_date"),
    )
    op.create_index(
        "ix_cot_symbol_report_date",
        "cot",
        ["symbol", sa.text("report_date DESC")],
    )

    # Panel D — daily ATM-IV summary. rank/percentile/rv are nullable: they are
    # accrued from our own daily series, so null until enough history exists.
    op.create_table(
        "iv_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column("atm_iv", sa.Numeric),
        sa.Column("iv_rank", sa.Numeric),
        sa.Column("iv_percentile", sa.Numeric),
        sa.Column("rv_30", sa.Numeric),
        sa.Column("iv_rv_spread", sa.Numeric),
        sa.Column("source", sa.Text, nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "symbol", "snapshot_date", name="uq_iv_metrics_symbol_snapshot_date"
        ),
    )
    op.create_index(
        "ix_iv_metrics_symbol_snapshot_date",
        "iv_metrics",
        ["symbol", sa.text("snapshot_date DESC")],
    )

    # Panel C — futures curve shape (contango/backwardation). May stay empty
    # until a multi-expiry futures source is resolved (README §4, Open Q #3).
    op.create_table(
        "curve_shape",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("front_price", sa.Numeric),
        sa.Column("back_price", sa.Numeric),
        sa.Column("spread", sa.Numeric),
        sa.Column("slope_pct", sa.Numeric),
        sa.Column("structure", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
        _created_at(),
        sa.UniqueConstraint("symbol", "date", name="uq_curve_shape_symbol_date"),
    )
    op.create_index(
        "ix_curve_shape_symbol_date",
        "curve_shape",
        ["symbol", sa.text("date DESC")],
    )

    # Placeholder for Writer 2 (separate project). Created but unused — reserves
    # the shape so sentiment tables can be added without touching data tables.
    # Raw inputs live here; the model's score + reasoning live in sentiment_scores.
    op.create_table(
        "sentiment_articles",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("headline", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("source", sa.Text),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.UniqueConstraint("url", name="uq_sentiment_articles_url"),
    )
    op.create_index(
        "ix_sentiment_articles_published_at",
        "sentiment_articles",
        [sa.text("published_at DESC")],
    )

    # Model output kept separate from raw inputs so the reasoning is auditable
    # and a re-score (new model) appends rather than overwrites.
    op.create_table(
        "sentiment_scores",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "article_id",
            sa.BigInteger,
            sa.ForeignKey("sentiment_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("commodity", sa.Text, nullable=False),
        sa.Column("score", sa.Numeric),
        sa.Column("reasoning", sa.Text),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.UniqueConstraint(
            "article_id",
            "commodity",
            "model",
            name="uq_sentiment_scores_article_commodity_model",
        ),
    )
    op.create_index(
        "ix_sentiment_scores_article_id", "sentiment_scores", ["article_id"]
    )


def downgrade() -> None:
    op.drop_table("sentiment_scores")
    op.drop_table("sentiment_articles")
    op.drop_table("curve_shape")
    op.drop_table("iv_metrics")
    op.drop_table("cot")
    op.drop_table("inventories")
    op.drop_table("macro_metrics")
    op.drop_table("prices")
