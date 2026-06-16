"""Tests for the CFTC COT ETL source (etl/sources/cftc.py).

Pure-function tests (config load, row mapping, blank→NULL, optional-token
header) run anywhere. The idempotency test runs against a live Postgres when one
is reachable via the POSTGRES_* env vars and is skipped otherwise, matching
tests/test_eia.py. External CFTC HTTP calls are always mocked — tests never hit
the live API.
"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url, load_cftc_markets
from etl.sources import cftc

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

def test_load_cftc_markets_structure():
    config = load_cftc_markets()
    assert "defaults" in config
    assert config["defaults"]["dataset"]
    assert "observation_start" in config["defaults"]
    assert "revision_lookback_days" in config["defaults"]
    by_symbol = {m["symbol"]: m for m in config["markets"]}
    # Verified codes for the majors (confirmed against the live API).
    assert by_symbol["GC"]["code"] == "088691"
    assert by_symbol["CL"]["code"] == "067651"
    assert by_symbol["ZC"]["code"] == "002602"
    for entry in config["markets"]:
        assert {"symbol", "code", "name"} <= entry.keys()
    # Base metals with no CFTC legacy report are omitted, not faked.
    assert "ALI" not in by_symbol
    assert "NICKEL" not in by_symbol


# --- Optional app-token header -------------------------------------------

def test_app_token_header_present_only_when_set(monkeypatch):
    monkeypatch.delenv("CFTC_APP_TOKEN", raising=False)
    assert cftc._app_token_headers() == {}
    monkeypatch.setenv("CFTC_APP_TOKEN", "tok123")
    assert cftc._app_token_headers() == {"X-App-Token": "tok123"}
    monkeypatch.setenv("CFTC_APP_TOKEN", "  ")
    assert cftc._app_token_headers() == {}


# --- Numeric parsing ------------------------------------------------------

def test_to_int_blank_becomes_null():
    assert cftc._to_int("207984") == 207984
    assert cftc._to_int("332709.0") == 332709
    assert cftc._to_int("") is None    # blank must NOT become 0
    assert cftc._to_int("  ") is None
    assert cftc._to_int(None) is None


# --- Row mapping ----------------------------------------------------------

def test_to_rows_maps_socrata_fields():
    records = [
        {
            "report_date_as_yyyy_mm_dd": "2026-06-09T00:00:00.000",
            "noncomm_positions_long_all": "207984",
            "noncomm_positions_short_all": "34147",
            "comm_positions_long_all": "58986",
            "comm_positions_short_all": "260022",
            "open_interest_all": "332709",
        },
        {
            "report_date_as_yyyy_mm_dd": "2026-06-02T00:00:00.000",
            "noncomm_positions_long_all": "200000",
            "noncomm_positions_short_all": "",
            "comm_positions_long_all": "55000",
            "comm_positions_short_all": "250000",
            "open_interest_all": "330000",
        },
    ]
    rows = cftc._to_rows("GC", records)
    first = {r["report_date"]: r for r in rows}["2026-06-09"]
    assert first["symbol"] == "GC"
    assert first["source"] == "CFTC"
    assert first["noncomm_long"] == 207984
    assert first["comm_short"] == 260022
    assert first["open_interest"] == 332709
    second = {r["report_date"]: r for r in rows}["2026-06-02"]
    assert second["noncomm_short"] is None  # blank → NULL


# --- Contract-code guard --------------------------------------------------

def test_fetch_rows_rejects_non_alphanumeric_code():
    with pytest.raises(ValueError, match="alphanumeric"):
        cftc._fetch_rows("6dca-aqww", "088'691", "2024-01-01", {})


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
        pytest.skip("No Postgres reachable for CFTC idempotency test")

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


_DEFAULTS = {"dataset": "6dca-aqww", "observation_start": "2024-01-01", "revision_lookback_days": 30}
_ENTRY = {"symbol": "GC", "code": "088691"}


def _count(engine, symbol):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM cot WHERE symbol = :s"),
            {"s": symbol},
        ).scalar()


def test_rerun_does_not_duplicate_rows(migrated_db, monkeypatch):
    engine = migrated_db
    records = [
        {
            "report_date_as_yyyy_mm_dd": "2024-01-02T00:00:00.000",
            "noncomm_positions_long_all": "100",
            "noncomm_positions_short_all": "50",
            "comm_positions_long_all": "30",
            "comm_positions_short_all": "70",
            "open_interest_all": "200",
        },
        {
            "report_date_as_yyyy_mm_dd": "2024-01-09T00:00:00.000",
            "noncomm_positions_long_all": "110",
            "noncomm_positions_short_all": "55",
            "comm_positions_long_all": "33",
            "comm_positions_short_all": "77",
            "open_interest_all": "210",
        },
    ]
    monkeypatch.setattr(cftc, "_fetch_rows", lambda *a, **k: records)

    cftc.ingest_market(engine, _ENTRY, _DEFAULTS, {})
    cftc.ingest_market(engine, _ENTRY, _DEFAULTS, {})

    assert _count(engine, "GC") == 2


def test_revision_upserts_value_in_place(migrated_db, monkeypatch):
    engine = migrated_db
    monkeypatch.setattr(
        cftc,
        "_fetch_rows",
        lambda *a, **k: [
            {
                "report_date_as_yyyy_mm_dd": "2024-01-02T00:00:00.000",
                "noncomm_positions_long_all": "100",
                "noncomm_positions_short_all": "50",
                "comm_positions_long_all": "30",
                "comm_positions_short_all": "70",
                "open_interest_all": "200",
            }
        ],
    )
    cftc.ingest_market(engine, _ENTRY, _DEFAULTS, {})

    monkeypatch.setattr(
        cftc,
        "_fetch_rows",
        lambda *a, **k: [
            {
                "report_date_as_yyyy_mm_dd": "2024-01-02T00:00:00.000",
                "noncomm_positions_long_all": "999",
                "noncomm_positions_short_all": "50",
                "comm_positions_long_all": "30",
                "comm_positions_short_all": "70",
                "open_interest_all": "200",
            }
        ],
    )
    cftc.ingest_market(engine, _ENTRY, _DEFAULTS, {})

    assert _count(engine, "GC") == 1
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT noncomm_long FROM cot WHERE symbol = 'GC'")
        ).scalar()
    assert value == 999
