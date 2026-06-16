"""Tests for the IV ETL source (etl/sources/iv.py).

The vol math is pure and tested without network. The provider is swapped for a
fake (no yfinance import needed). The idempotency test runs against a live
Postgres when reachable via POSTGRES_* and is skipped otherwise, matching
tests/test_eia.py.
"""
import datetime as dt
import math
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url
from etl.sources import iv

alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- ATM IV selection -----------------------------------------------------

def test_atm_iv_picks_strike_closest_to_spot():
    pairs = [(95.0, 0.30), (100.0, 0.25), (110.0, 0.40)]
    assert iv._atm_iv(101.0, pairs) == 0.25


def test_atm_iv_ignores_zero_nan_and_missing():
    pairs = [(100.0, 0.0), (101.0, float("nan")), (102.0, None), (105.0, 0.33)]
    # The nearest *usable* IV is the 105 strike.
    assert iv._atm_iv(100.0, pairs) == 0.33


def test_atm_iv_none_without_spot_or_usable_iv():
    assert iv._atm_iv(None, [(100.0, 0.25)]) is None
    assert iv._atm_iv(100.0, [(100.0, 0.0)]) is None
    assert iv._atm_iv(100.0, []) is None


def test_atm_iv_floors_degenerate_sentinels():
    # Yahoo's stale-contract sentinels (~1e-5, sub-1%) must NOT be stored as IV;
    # a chain that is all-degenerate yields None (e.g. when the market is closed).
    degenerate = [(100.0, 0.00001), (101.0, 0.0039), (99.0, 0.007822)]
    assert iv._atm_iv(100.0, degenerate) is None
    # A real ATM reading mixed in is still selected.
    mixed = degenerate + [(102.0, 0.18)]
    assert iv._atm_iv(100.0, mixed) == 0.18


# --- Realized vol ---------------------------------------------------------

def test_realized_vol_constant_prices_is_zero():
    assert iv._realized_vol([100.0] * 40) == 0.0


def test_realized_vol_matches_manual_calc():
    closes = [100.0, 101.0, 102.0, 101.0, 103.0]
    rv = iv._realized_vol(closes, window=4, trading_days=252)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    assert rv == pytest.approx(math.sqrt(var) * math.sqrt(252))


def test_realized_vol_insufficient_history_is_none():
    assert iv._realized_vol([100.0, 101.0], window=30) is None


# --- IV rank / percentile -------------------------------------------------

def test_iv_rank_and_percentile_basic():
    history = [float(x) for x in range(10, 29)]  # 19 prior obs: 10..28
    # 19 history + current = 20 == _MIN_HISTORY_OBS, so both resolve.
    assert iv._iv_rank(history, 28.0) == pytest.approx(1.0)
    assert iv._iv_rank(history, 10.0) == pytest.approx(0.0)
    assert iv._iv_percentile(history, 28.0) == pytest.approx(1.0)


def test_iv_rank_percentile_none_until_min_history():
    history = [20.0, 21.0, 22.0]  # + current = 4 < 20
    assert iv._iv_rank(history, 23.0) is None
    assert iv._iv_percentile(history, 23.0) is None


def test_iv_rank_none_when_window_flat():
    history = [25.0] * 19
    assert iv._iv_rank(history, 25.0) is None


# --- Spread + row building ------------------------------------------------

def test_iv_rv_spread_null_propagates():
    assert iv._iv_rv_spread(0.30, 0.20) == pytest.approx(0.10)
    assert iv._iv_rv_spread(None, 0.20) is None
    assert iv._iv_rv_spread(0.30, None) is None


def test_build_row_shapes_record():
    row = iv.build_row(
        "GC", dt.date(2026, 6, 16), atm_iv=0.30,
        closes=[100.0] * 40, history_ivs=[],
    )
    assert row["symbol"] == "GC"
    assert row["snapshot_date"] == "2026-06-16"
    assert row["source"] == "yfinance"
    assert row["rv_30"] == 0.0
    assert row["iv_rv_spread"] == pytest.approx(0.30)
    # No prior history -> rank/percentile stay NULL.
    assert row["iv_rank"] is None and row["iv_percentile"] is None


# --- Swappable provider / get_iv -----------------------------------------

class _FakeProvider:
    def __init__(self, iv_value, closes):
        self._iv = iv_value
        self._closes = closes

    def atm_iv(self, ticker):
        return self._iv

    def daily_closes(self, ticker, lookback_days):
        return self._closes


def test_get_iv_delegates_to_provider(monkeypatch):
    monkeypatch.setattr(iv, "_PROVIDER", _FakeProvider(0.42, []))
    assert iv.get_iv("GLD") == 0.42


def test_set_provider_swaps(monkeypatch):
    original = iv._PROVIDER
    try:
        iv.set_provider(_FakeProvider(0.11, []))
        assert iv.get_iv("anything") == 0.11
    finally:
        iv.set_provider(original)


def test_proxy_pairs_skips_null_proxies():
    symbols = {
        "commodities": {
            "metals": [
                {"future": "GC", "iv_proxy": "GLD"},
                {"future": "ALI", "iv_proxy": None},
            ]
        }
    }
    assert iv._proxy_pairs(symbols) == [("GC", "GLD")]


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
        pytest.skip("No Postgres reachable for IV idempotency test")

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


def _count(engine, symbol):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM iv_metrics WHERE symbol = :s"),
            {"s": symbol},
        ).scalar()


def test_rerun_same_day_does_not_duplicate(migrated_db, monkeypatch):
    engine = migrated_db
    monkeypatch.setattr(iv, "_PROVIDER", _FakeProvider(0.30, [100.0] * 40))
    today = dt.date(2026, 6, 16)

    iv.ingest_symbol(engine, "GC", "GLD", today)
    iv.ingest_symbol(engine, "GC", "GLD", today)

    assert _count(engine, "GC") == 1


def test_rerun_upserts_value_in_place(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 16)

    monkeypatch.setattr(iv, "_PROVIDER", _FakeProvider(0.30, [100.0] * 40))
    iv.ingest_symbol(engine, "GC", "GLD", today)
    monkeypatch.setattr(iv, "_PROVIDER", _FakeProvider(0.55, [100.0] * 40))
    iv.ingest_symbol(engine, "GC", "GLD", today)

    assert _count(engine, "GC") == 1
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT atm_iv FROM iv_metrics WHERE symbol = 'GC'")
        ).scalar()
    assert float(value) == pytest.approx(0.55)
