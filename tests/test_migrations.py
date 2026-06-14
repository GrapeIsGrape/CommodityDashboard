"""Migration tests for the Phase 1 data tables (0002_data_tables).

These run against a live Postgres when one is reachable via the POSTGRES_*
env vars (the Compose / local DB); when none is reachable the whole module is
skipped so the suite still passes in environments without a database.

What is verified: a clean upgrade from baseline creates every table with its
natural-key unique constraint and symbol/date index, re-running `upgrade head`
is a safe no-op, and `downgrade base` removes every table cleanly.
"""
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url

alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}

_DATA_TABLES = {
    "prices",
    "macro_metrics",
    "inventories",
    "cot",
    "iv_metrics",
    "curve_shape",
    "sentiment_articles",
    "sentiment_scores",
}

_NATURAL_KEYS = {
    "prices": {"symbol", "date"},
    "macro_metrics": {"series_id", "date"},
    "inventories": {"source", "series_id", "date"},
    "cot": {"symbol", "report_date"},
    "iv_metrics": {"symbol", "snapshot_date"},
    "curve_shape": {"symbol", "date"},
}


def _alembic_cfg():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    return cfg


@pytest.fixture
def migrated_db(monkeypatch):
    """Apply env overrides, skip if no DB, and leave the schema at baseline."""
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for migration tests")

    cfg = _alembic_cfg()
    alembic_command.downgrade(cfg, "base")
    try:
        yield cfg, engine
    finally:
        alembic_command.downgrade(cfg, "base")
        engine.dispose()


def test_upgrade_creates_all_tables(migrated_db):
    cfg, engine = migrated_db
    alembic_command.upgrade(cfg, "head")

    tables = set(inspect(engine).get_table_names())
    assert _DATA_TABLES <= tables


def test_natural_key_unique_constraints(migrated_db):
    cfg, engine = migrated_db
    alembic_command.upgrade(cfg, "head")

    inspector = inspect(engine)
    for table, key in _NATURAL_KEYS.items():
        constraints = inspector.get_unique_constraints(table)
        assert any(
            set(c["column_names"]) == key for c in constraints
        ), f"{table} missing unique constraint on {key}"


def test_symbol_date_indexes_present(migrated_db):
    cfg, engine = migrated_db
    alembic_command.upgrade(cfg, "head")

    inspector = inspect(engine)
    for table in _NATURAL_KEYS:
        index_names = {ix["name"] for ix in inspector.get_indexes(table)}
        assert any(name.startswith("ix_") for name in index_names), table


def test_iv_metrics_rank_columns_nullable(migrated_db):
    cfg, engine = migrated_db
    alembic_command.upgrade(cfg, "head")

    columns = {c["name"]: c for c in inspect(engine).get_columns("iv_metrics")}
    for col in ("iv_rank", "iv_percentile", "rv_30", "iv_rv_spread"):
        assert columns[col]["nullable"], f"{col} must be nullable"


def test_upgrade_head_is_idempotent_noop(migrated_db):
    cfg, engine = migrated_db
    alembic_command.upgrade(cfg, "head")
    # Re-running at head must not error or change the table set.
    before = set(inspect(engine).get_table_names())
    alembic_command.upgrade(cfg, "head")
    assert set(inspect(engine).get_table_names()) == before


def test_downgrade_removes_data_tables(migrated_db):
    cfg, engine = migrated_db
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "base")

    tables = set(inspect(engine).get_table_names())
    assert not (_DATA_TABLES & tables)
