"""Tests for the USDA NASS QuickStats ETL source (etl/sources/usda.py).

Pure-function tests (config load, value/sentinel parsing, date mapping, secret
redaction) run anywhere. The idempotency test runs against a live Postgres when
one is reachable via the POSTGRES_* env vars and is skipped otherwise, matching
tests/test_eia.py. External NASS HTTP calls are always mocked — tests never hit
the live API.
"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url, load_usda_series
from etl.sources import usda

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

def test_load_usda_series_structure():
    config = load_usda_series()
    assert "defaults" in config
    assert "observation_start" in config["defaults"]
    assert "revision_lookback_days" in config["defaults"]
    ids = {s["id"] for s in config["series"]}
    # Panel B grains essentials: corn/soybean/wheat production + stocks.
    assert {
        "CORN_GRAIN_PRODUCTION_US",
        "SOYBEANS_PRODUCTION_US",
        "WHEAT_PRODUCTION_US",
        "CORN_GRAIN_STOCKS_US",
    } <= ids
    for entry in config["series"]:
        assert {"id", "label", "unit", "panel", "query"} <= entry.keys()
        # The api key must never be baked into the config query.
        assert "key" not in entry["query"]
        assert entry["query"].get("agg_level_desc") == "NATIONAL"


# --- Date mapping ---------------------------------------------------------

def test_record_date_prefers_week_ending():
    assert usda._record_date(
        {"year": "2024", "reference_period_desc": "WEEK #19", "week_ending": "2024-05-12"}
    ).isoformat() == "2024-05-12"


def test_record_date_annual_anchors_to_jan_1():
    assert usda._record_date(
        {"year": "2023", "reference_period_desc": "YEAR"}
    ).isoformat() == "2023-01-01"
    assert usda._record_date(
        {"year": "2023", "reference_period_desc": "MARKETING YEAR"}
    ).isoformat() == "2023-01-01"


def test_record_date_quarterly_stocks_anchor_to_month():
    assert usda._record_date(
        {"year": "2024", "reference_period_desc": "FIRST OF DEC"}
    ).isoformat() == "2024-12-01"
    assert usda._record_date(
        {"year": "2024", "reference_period_desc": "FIRST OF SEP"}
    ).isoformat() == "2024-09-01"


def test_record_date_unmapped_period_falls_back_to_jan_1():
    assert usda._record_date(
        {"year": "2024", "reference_period_desc": "SOMETHING ODD"}
    ).isoformat() == "2024-01-01"


# --- Value / sentinel parsing --------------------------------------------

def test_parse_value_strips_separators_and_nulls_sentinels():
    assert usda._parse_value("15,148,038,000") == "15148038000"
    assert usda._parse_value("(D)") is None   # withheld must NOT become 0
    assert usda._parse_value("(NA)") is None
    assert usda._parse_value("") is None
    assert usda._parse_value(None) is None
    assert usda._parse_value(" 1,234 ") == "1234"


def test_to_rows_maps_value_unit_and_source():
    records = [
        {"year": "2023", "reference_period_desc": "YEAR", "Value": "1,000", "unit_desc": "BU"},
        {"year": "2022", "reference_period_desc": "YEAR", "Value": "(D)", "unit_desc": "BU"},
    ]
    config_unit = usda._to_rows("CORN_GRAIN_PRODUCTION_US", records, "BU")
    by_date = {r["date"]: r["value"] for r in config_unit}
    assert by_date["2023-01-01"] == "1000"
    assert by_date["2022-01-01"] is None
    assert all(r["source"] == "USDA" for r in config_unit)
    assert all(r["unit"] == "BU" for r in config_unit)
    # Falls back to the API's reported unit when config omits it.
    api_unit = usda._to_rows("X", records, None)
    assert api_unit[0]["unit"] == "BU"


# --- Secret redaction on request failure ---------------------------------

def test_fetch_failure_redacts_api_key(monkeypatch):
    secret = "supersecretkey"

    def boom(url, params, timeout):
        raise usda.requests.ConnectionError(
            f"Max retries exceeded with url: /api/api_GET/?key={secret}&format=JSON"
        )

    monkeypatch.setattr(usda.requests, "get", boom)
    with pytest.raises(RuntimeError) as excinfo:
        usda._fetch_records({"short_desc": "X"}, secret, "2024")
    assert secret not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# --- API key guard --------------------------------------------------------

def test_run_requires_api_key(monkeypatch):
    monkeypatch.setenv("USDA_NASS_API_KEY", "  ")
    with pytest.raises(RuntimeError, match="USDA_NASS_API_KEY"):
        usda.run()


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
        pytest.skip("No Postgres reachable for USDA idempotency test")

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


_DEFAULTS = {"observation_start": "2020-01-01", "revision_lookback_days": 30}
_ENTRY = {
    "id": "CORN_GRAIN_PRODUCTION_US",
    "unit": "BU",
    "query": {"short_desc": "CORN, GRAIN - PRODUCTION, MEASURED IN BU"},
}


def _count(engine, series_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM inventories "
                "WHERE source = 'USDA' AND series_id = :s"
            ),
            {"s": series_id},
        ).scalar()


def test_rerun_does_not_duplicate_rows(migrated_db, monkeypatch):
    engine = migrated_db
    records = [
        {"year": "2021", "reference_period_desc": "YEAR", "Value": "100", "unit_desc": "BU"},
        {"year": "2022", "reference_period_desc": "YEAR", "Value": "(D)", "unit_desc": "BU"},
        {"year": "2023", "reference_period_desc": "YEAR", "Value": "200", "unit_desc": "BU"},
    ]
    monkeypatch.setattr(usda, "_fetch_records", lambda *a, **k: records)

    usda.ingest_series(engine, _ENTRY, "key", _DEFAULTS)
    usda.ingest_series(engine, _ENTRY, "key", _DEFAULTS)

    assert _count(engine, "CORN_GRAIN_PRODUCTION_US") == 3


def test_revision_upserts_value_in_place(migrated_db, monkeypatch):
    engine = migrated_db
    monkeypatch.setattr(
        usda,
        "_fetch_records",
        lambda *a, **k: [
            {"year": "2023", "reference_period_desc": "YEAR", "Value": "100", "unit_desc": "BU"}
        ],
    )
    usda.ingest_series(engine, _ENTRY, "key", _DEFAULTS)

    monkeypatch.setattr(
        usda,
        "_fetch_records",
        lambda *a, **k: [
            {"year": "2023", "reference_period_desc": "YEAR", "Value": "999", "unit_desc": "BU"}
        ],
    )
    usda.ingest_series(engine, _ENTRY, "key", _DEFAULTS)

    assert _count(engine, "CORN_GRAIN_PRODUCTION_US") == 1
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT value FROM inventories "
                "WHERE source = 'USDA' AND series_id = 'CORN_GRAIN_PRODUCTION_US'"
            )
        ).scalar()
    assert float(value) == 999
