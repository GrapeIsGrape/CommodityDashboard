"""Tests for the FRED macro ETL source (etl/sources/fred.py).

Pure-function tests (config load, missing-value mapping) run anywhere. The
idempotency test runs against a live Postgres when one is reachable via the
POSTGRES_* env vars and is skipped otherwise, matching tests/test_migrations.py.
External FRED HTTP calls are always mocked — tests never hit the live API.
"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url, load_fred_series
from etl.sources import fred

alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- Config loading -------------------------------------------------------

def test_load_fred_series_structure():
    config = load_fred_series()
    assert "defaults" in config
    assert "observation_start" in config["defaults"]
    assert "revision_lookback_days" in config["defaults"]
    ids = {s["id"] for s in config["series"]}
    # Panel A coverage incl. the DXY proxy and FRED's VIX.
    assert {"DGS10", "DFII10", "T10YIE", "CPIAUCSL", "VIXCLS", "DTWEXBGS"} <= ids
    for entry in config["series"]:
        assert {"id", "label", "panel"} <= entry.keys()


# --- Missing-value handling ----------------------------------------------

def test_missing_value_sentinel_becomes_null():
    observations = [
        {"date": "2024-01-01", "value": "1.5"},
        {"date": "2024-01-02", "value": "."},
        {"date": "2024-01-03", "value": "2.0"},
    ]
    rows = fred._to_rows("DGS10", observations)
    by_date = {r["date"]: r["value"] for r in rows}
    assert by_date["2024-01-01"] == "1.5"
    assert by_date["2024-01-02"] is None  # "." must NOT become 0
    assert by_date["2024-01-03"] == "2.0"
    assert all(r["source"] == "FRED" for r in rows)


# --- API key guard --------------------------------------------------------

def test_run_requires_api_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "  ")
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        fred.run()


# --- Idempotency (live Postgres or skip) ---------------------------------

@pytest.fixture
def migrated_db(monkeypatch):
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for FRED idempotency test")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    alembic_command.downgrade(cfg, "base")
    alembic_command.upgrade(cfg, "head")
    try:
        yield engine
    finally:
        alembic_command.downgrade(cfg, "base")
        engine.dispose()


_DEFAULTS = {"observation_start": "2024-01-01", "revision_lookback_days": 14}


def _count(engine, series_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM macro_metrics WHERE series_id = :s"),
            {"s": series_id},
        ).scalar()


def test_rerun_does_not_duplicate_rows(migrated_db, monkeypatch):
    engine = migrated_db
    observations = [
        {"date": "2024-01-01", "value": "1.0"},
        {"date": "2024-01-02", "value": "."},
        {"date": "2024-01-03", "value": "3.0"},
    ]
    monkeypatch.setattr(fred, "_fetch_observations", lambda *a, **k: observations)

    fred.ingest_series(engine, "DGS10", "key", _DEFAULTS)
    fred.ingest_series(engine, "DGS10", "key", _DEFAULTS)

    assert _count(engine, "DGS10") == 3


def test_revision_upserts_value_in_place(migrated_db, monkeypatch):
    engine = migrated_db
    monkeypatch.setattr(
        fred, "_fetch_observations", lambda *a, **k: [{"date": "2024-01-01", "value": "1.0"}]
    )
    fred.ingest_series(engine, "DGS10", "key", _DEFAULTS)

    monkeypatch.setattr(
        fred, "_fetch_observations", lambda *a, **k: [{"date": "2024-01-01", "value": "9.9"}]
    )
    fred.ingest_series(engine, "DGS10", "key", _DEFAULTS)

    assert _count(engine, "DGS10") == 1
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT value FROM macro_metrics WHERE series_id = 'DGS10'")
        ).scalar()
    assert float(value) == 9.9
