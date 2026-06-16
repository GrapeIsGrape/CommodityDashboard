"""Tests for the EIA energy-inventory ETL source (etl/sources/eia.py).

Pure-function tests (config load, missing-value mapping, period parsing) run
anywhere. The idempotency test runs against a live Postgres when one is
reachable via the POSTGRES_* env vars and is skipped otherwise, matching
tests/test_fred.py. External EIA HTTP calls are always mocked — tests never hit
the live API.
"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url, load_eia_series
from etl.sources import eia

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

def test_load_eia_series_structure():
    config = load_eia_series()
    assert "defaults" in config
    assert "observation_start" in config["defaults"]
    assert "revision_lookback_days" in config["defaults"]
    ids = {s["id"] for s in config["series"]}
    # Panel B essentials: crude (incl. Cushing), products, nat-gas storage, proxy.
    assert {
        "PET.WCESTUS1.W",
        "PET.W_EPC0_SAX_YCUOK_MBBL.W",
        "NG.NW2_EPG0_SWO_R48_BCF.W",
    } <= ids
    for entry in config["series"]:
        assert {"id", "label", "unit", "panel"} <= entry.keys()


# --- Period parsing -------------------------------------------------------

def test_period_to_date_handles_eia_formats():
    assert eia._period_to_date("2024").isoformat() == "2024-01-01"
    assert eia._period_to_date("2024-03").isoformat() == "2024-03-01"
    assert eia._period_to_date("2024-03-15").isoformat() == "2024-03-15"


# --- Missing-value handling ----------------------------------------------

def test_missing_value_becomes_null():
    observations = [
        {"period": "2024-01-05", "value": "100", "units": "Mbbl"},
        {"period": "2024-01-12", "value": None, "units": "Mbbl"},
        {"period": "2024-01-19", "value": "", "units": "Mbbl"},
        {"period": "2024-01-26", "value": "200", "units": "Mbbl"},
    ]
    rows = eia._to_rows("PET.WCESTUS1.W", observations, "Thousand Barrels")
    by_date = {r["date"]: r["value"] for r in rows}
    assert by_date["2024-01-05"] == "100"
    assert by_date["2024-01-12"] is None  # JSON null must NOT become 0
    assert by_date["2024-01-19"] is None  # blank must NOT become 0
    assert by_date["2024-01-26"] == "200"
    assert all(r["source"] == "EIA" for r in rows)


def test_unit_prefers_config_then_api():
    observations = [{"period": "2024-01-05", "value": "1", "units": "Thousand Barrels"}]
    config_unit = eia._to_rows("X", observations, "Bcf")
    assert config_unit[0]["unit"] == "Bcf"
    api_unit = eia._to_rows("X", observations, None)
    assert api_unit[0]["unit"] == "Thousand Barrels"


# --- Secret redaction on request failure ---------------------------------

def test_fetch_failure_redacts_api_key(monkeypatch):
    secret = "supersecretkey"

    def boom(url, params, timeout):
        raise eia.requests.ConnectionError(
            f"Max retries exceeded with url: /v2/seriesid/X?api_key={secret}&start=2024"
        )

    monkeypatch.setattr(eia.requests, "get", boom)
    with pytest.raises(RuntimeError) as excinfo:
        eia._fetch_observations("X", secret, "2024")
    assert secret not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# --- API key guard --------------------------------------------------------

def test_run_requires_api_key(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "  ")
    with pytest.raises(RuntimeError, match="EIA_API_KEY"):
        eia.run()


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
        pytest.skip("No Postgres reachable for EIA idempotency test")

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
_ENTRY = {"id": "PET.WCESTUS1.W", "unit": "Thousand Barrels"}


def _count(engine, series_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM inventories "
                "WHERE source = 'EIA' AND series_id = :s"
            ),
            {"s": series_id},
        ).scalar()


def test_rerun_does_not_duplicate_rows(migrated_db, monkeypatch):
    engine = migrated_db
    observations = [
        {"period": "2024-01-05", "value": "100", "units": "Mbbl"},
        {"period": "2024-01-12", "value": None, "units": "Mbbl"},
        {"period": "2024-01-19", "value": "200", "units": "Mbbl"},
    ]
    monkeypatch.setattr(eia, "_fetch_observations", lambda *a, **k: observations)

    eia.ingest_series(engine, _ENTRY, "key", _DEFAULTS)
    eia.ingest_series(engine, _ENTRY, "key", _DEFAULTS)

    assert _count(engine, "PET.WCESTUS1.W") == 3


def test_revision_upserts_value_in_place(migrated_db, monkeypatch):
    engine = migrated_db
    monkeypatch.setattr(
        eia,
        "_fetch_observations",
        lambda *a, **k: [{"period": "2024-01-05", "value": "100", "units": "Mbbl"}],
    )
    eia.ingest_series(engine, _ENTRY, "key", _DEFAULTS)

    monkeypatch.setattr(
        eia,
        "_fetch_observations",
        lambda *a, **k: [{"period": "2024-01-05", "value": "999", "units": "Mbbl"}],
    )
    eia.ingest_series(engine, _ENTRY, "key", _DEFAULTS)

    assert _count(engine, "PET.WCESTUS1.W") == 1
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT value FROM inventories "
                "WHERE source = 'EIA' AND series_id = 'PET.WCESTUS1.W'"
            )
        ).scalar()
    assert float(value) == 999
