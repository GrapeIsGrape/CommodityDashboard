"""Tests for the prices ETL source (etl/sources/prices.py).

Pure transforms (bar -> row, bad-bar skip/NULL, adj_close=close non-dividend
convention, the incremental start floor = max(date) - refetch_days) are
network-free. The provider is swapped for a fake (no yfinance import). The DB
upsert + idempotency + incremental + per-symbol-isolation tests run against a
live Postgres when reachable and are skipped otherwise, matching
tests/test_vol_indices.py.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url, load_macro_context
from etl.sources import prices as pr

alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


def _bar(d, o=10.0, h=11.0, low=9.0, close=10.5, adj=None, volume=1000):
    return (d, o, h, low, close, adj, volume)


# --- Pure cleaning --------------------------------------------------------

def test_clean_price_passes_positive():
    assert pr._clean_price(18.5) == 18.5


def test_clean_price_rejects_nan_none_nonpositive_nonnumeric():
    assert pr._clean_price(float("nan")) is None
    assert pr._clean_price(None) is None
    assert pr._clean_price(0.0) is None
    assert pr._clean_price(-1.0) is None
    assert pr._clean_price("notanumber") is None


def test_clean_volume_keeps_zero_rejects_negative_and_nan():
    assert pr._clean_volume(0) == 0
    assert pr._clean_volume(1234.0) == 1234
    assert pr._clean_volume(-5) is None
    assert pr._clean_volume(float("nan")) is None
    assert pr._clean_volume(None) is None


# --- Row building ---------------------------------------------------------

def test_build_row_stores_raw_and_adjusted_in_right_columns():
    # auto_adjust=False -> raw Close and separate Adj Close both present.
    row = pr.build_row(_bar(dt.date(2026, 6, 1), close=100.0, adj=98.0))
    assert row["close"] == 100.0      # raw tape close
    assert row["adj_close"] == 98.0   # back-adjusted total-return close
    assert row["source"] == "yfinance"
    assert row["date"] == "2026-06-01"
    assert row["open"] == 10.0 and row["high"] == 11.0 and row["low"] == 9.0
    assert row["volume"] == 1000


def test_build_row_non_dividend_sets_adj_close_equals_close():
    # No separate adjusted value -> adj_close falls back to raw close so a
    # consumer can always read adj_close.
    row = pr.build_row(_bar(dt.date(2026, 6, 1), close=100.0, adj=None))
    assert row["adj_close"] == row["close"] == 100.0


def test_build_row_unusable_adj_falls_back_to_close():
    # An adjusted field that is NaN/<=0 is unusable -> fall back to raw close,
    # never store a fabricated adjusted value.
    row = pr.build_row(_bar(dt.date(2026, 6, 1), close=100.0, adj=float("nan")))
    assert row["adj_close"] == 100.0
    row2 = pr.build_row(_bar(dt.date(2026, 6, 1), close=100.0, adj=0.0))
    assert row2["adj_close"] == 100.0


def test_build_row_bad_close_skips_whole_bar():
    # No usable raw close -> no row at all (never a NULL-close placeholder).
    assert pr.build_row(_bar(dt.date(2026, 6, 1), close=float("nan"))) is None
    assert pr.build_row(_bar(dt.date(2026, 6, 1), close=0.0)) is None
    assert pr.build_row(_bar(dt.date(2026, 6, 1), close=None)) is None


def test_build_rows_drops_unusable_bars_and_tags_symbol():
    bars = [
        _bar(dt.date(2026, 6, 1), close=100.0, adj=99.0),
        _bar(dt.date(2026, 6, 2), close=float("nan")),  # holiday/stale -> dropped
        _bar(dt.date(2026, 6, 3), close=101.0, adj=100.0),
    ]
    rows = pr.build_rows("TLT", bars)
    assert [r["date"] for r in rows] == ["2026-06-01", "2026-06-03"]
    assert all(r["symbol"] == "TLT" for r in rows)


# --- Incremental start floor ----------------------------------------------

def test_start_date_first_run_backfills():
    today = dt.date(2026, 6, 18)
    assert pr.start_date(None, backfill_days=1825, refetch_days=400, today=today) == (
        today - dt.timedelta(days=1825)
    )


def test_start_date_incremental_floor_is_max_minus_refetch():
    today = dt.date(2026, 6, 18)
    latest = dt.date(2026, 6, 17)
    # NOT just max(date): the floor steps back refetch_days so the lookback
    # re-touches recent rows for adjustment consistency.
    assert pr.start_date(latest, backfill_days=1825, refetch_days=400, today=today) == (
        latest - dt.timedelta(days=400)
    )


# --- Config consumption ---------------------------------------------------

def test_symbols_from_config_are_macro_context():
    # Driven off the real macro_context block, not a hardcoded list.
    assert set(pr._symbols()) == {"TLT", "VTI", "QQQ"}
    assert {e["symbol"] for e in load_macro_context()} == set(pr._symbols())


def test_backfill_and_refetch_from_config():
    cfg = {"defaults": {"backfill_days": 365, "refetch_days": 90}}
    assert pr._backfill_days(cfg) == 365
    assert pr._refetch_days(cfg) == 90


def test_backfill_and_refetch_defaults_when_absent():
    assert pr._backfill_days({}) == pr._DEFAULT_BACKFILL_DAYS
    assert pr._refetch_days({}) == pr._DEFAULT_REFETCH_DAYS


# --- Swappable provider ---------------------------------------------------

class _FakeProvider:
    """Returns the bars at/after `start`; records each call for assertions.
    Optionally raises for a given ticker (per-symbol isolation test)."""

    def __init__(self, bars_by_ticker, fail_ticker=None):
        self._bars = bars_by_ticker
        self._fail = fail_ticker
        self.calls = []

    def daily_bars(self, ticker, start):
        self.calls.append((ticker, start))
        if ticker == self._fail:
            raise RuntimeError(f"boom {ticker}")
        return [b for b in self._bars.get(ticker, []) if b[0] >= start]


def test_set_provider_swaps():
    original = pr._PROVIDER
    try:
        fake = _FakeProvider({})
        pr.set_provider(fake)
        assert pr._PROVIDER is fake
    finally:
        pr.set_provider(original)


# --- Idempotency / incremental / isolation (live Postgres or skip) -------

@pytest.fixture
def migrated_db(monkeypatch):
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for prices idempotency test")

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
            text("SELECT count(*) FROM prices WHERE symbol = :s"),
            {"s": symbol},
        ).scalar()


def _row(engine, symbol, date):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT close, adj_close FROM prices "
                "WHERE symbol = :s AND date = :d"
            ),
            {"s": symbol, "d": date},
        ).first()


def test_rerun_does_not_duplicate(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 18)
    bars = [_bar(today - dt.timedelta(days=i), close=100.0 + i, adj=99.0 + i) for i in range(5)]
    monkeypatch.setattr(pr, "_PROVIDER", _FakeProvider({"TLT": bars}))

    pr.ingest_symbol(engine, "TLT", backfill_days=1825, refetch_days=400, today=today)
    first = _count(engine, "TLT")
    pr.ingest_symbol(engine, "TLT", backfill_days=1825, refetch_days=400, today=today)
    second = _count(engine, "TLT")

    assert first == 5
    assert second == 5  # re-run upserts in place, no duplicates.


def test_rerun_updates_adj_close_in_place_keeps_raw_close(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 18)
    day = today - dt.timedelta(days=1)

    # First fetch: raw 100, adjusted 98.
    monkeypatch.setattr(pr, "_PROVIDER", _FakeProvider({"TLT": [_bar(day, close=100.0, adj=98.0)]}))
    pr.ingest_symbol(engine, "TLT", backfill_days=1825, refetch_days=400, today=today)
    # Ex-dividend re-fetch: same raw close (stable tape), new adjusted value.
    monkeypatch.setattr(pr, "_PROVIDER", _FakeProvider({"TLT": [_bar(day, close=100.0, adj=99.5)]}))
    pr.ingest_symbol(engine, "TLT", backfill_days=1825, refetch_days=400, today=today)

    assert _count(engine, "TLT") == 1
    close, adj = _row(engine, "TLT", day.isoformat())
    assert float(close) == pytest.approx(100.0)  # raw close stable
    assert float(adj) == pytest.approx(99.5)      # adj_close updated in place


def test_second_run_is_incremental_with_refetch_floor(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 18)
    bars = [_bar(today - dt.timedelta(days=i), close=100.0 + i, adj=99.0 + i) for i in range(5)]
    latest_bar_date = max(b[0] for b in bars)

    fake = _FakeProvider({"TLT": bars})
    monkeypatch.setattr(pr, "_PROVIDER", fake)
    pr.ingest_symbol(engine, "TLT", backfill_days=1825, refetch_days=400, today=today)
    pr.ingest_symbol(engine, "TLT", backfill_days=1825, refetch_days=400, today=today)

    first_start, second_start = fake.calls[0][1], fake.calls[1][1]
    assert first_start == today - dt.timedelta(days=1825)            # first run backfills
    assert second_start == latest_bar_date - dt.timedelta(days=400)  # floor = max - refetch


def test_holiday_bar_stores_no_row(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 18)
    holiday = today - dt.timedelta(days=2)
    bars = [
        _bar(today - dt.timedelta(days=3), close=100.0, adj=99.0),
        _bar(holiday, close=float("nan")),  # weekend/holiday/stale
        _bar(today - dt.timedelta(days=1), close=101.0, adj=100.0),
    ]
    monkeypatch.setattr(pr, "_PROVIDER", _FakeProvider({"VTI": bars}))
    pr.ingest_symbol(engine, "VTI", backfill_days=1825, refetch_days=400, today=today)

    assert _count(engine, "VTI") == 2
    assert _row(engine, "VTI", holiday.isoformat()) is None  # no placeholder row


def test_per_symbol_isolation_one_bad_ticker(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 18)
    good = [_bar(today - dt.timedelta(days=1), close=100.0, adj=99.0)]
    fake = _FakeProvider({"VTI": good, "QQQ": good}, fail_ticker="TLT")
    monkeypatch.setattr(pr, "_PROVIDER", fake)
    monkeypatch.setattr(pr, "_symbols", lambda: ["TLT", "VTI", "QQQ"])

    pr.run()  # must not abort on TLT's failure

    assert _count(engine, "TLT") == 0
    assert _count(engine, "VTI") == 1
    assert _count(engine, "QQQ") == 1
