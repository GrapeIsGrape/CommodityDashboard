"""Tests for the vol-indices ETL source (etl/sources/vol_indices.py).

Pure transforms (close -> row, rank/percentile over a backfilled series,
NaN/holiday -> NULL, rv_30/iv_rv_spread forced NULL) are network-free. The
provider is swapped for a fake (no yfinance import). The DB upsert + idempotency
+ incremental-vs-backfill tests run against a live Postgres when reachable and
are skipped otherwise, matching tests/test_iv.py.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url, load_symbols
from etl.sources import vol_indices as vi

alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- Pure level cleaning --------------------------------------------------

def test_clean_level_passes_positive():
    assert vi._clean_level(18.5) == 18.5


def test_clean_level_rejects_nan_none_and_nonpositive():
    assert vi._clean_level(float("nan")) is None
    assert vi._clean_level(None) is None
    assert vi._clean_level(0.0) is None
    assert vi._clean_level(-1.0) is None
    assert vi._clean_level("notanumber") is None


# --- Row building ---------------------------------------------------------

def _bars(start, levels):
    return [(start + dt.timedelta(days=i), lvl) for i, lvl in enumerate(levels)]


def test_build_index_rows_shapes_records_and_forces_nulls():
    bars = _bars(dt.date(2026, 6, 1), [20.0, 21.0])
    rows = vi.build_index_rows("GVZ", bars)
    assert [r["symbol"] for r in rows] == ["GVZ", "GVZ"]
    assert rows[0]["snapshot_date"] == "2026-06-01"
    assert rows[0]["atm_iv"] == 20.0
    assert rows[0]["source"] == "yfinance"
    # Vol indices: RV and the spread are always NULL.
    for r in rows:
        assert r["rv_30"] is None
        assert r["iv_rv_spread"] is None


def test_build_index_rows_holiday_nan_is_null_and_excluded_from_history():
    # A NaN bar in the middle stores NULL and must not become part of the rank
    # history (so it neither fakes a level nor pollutes later ranks).
    levels = [float(x) for x in range(10, 29)] + [float("nan"), 40.0]
    bars = _bars(dt.date(2026, 1, 1), levels)
    rows = vi.build_index_rows("OVX", bars)

    nan_row = rows[-2]
    assert nan_row["atm_iv"] is None
    assert nan_row["iv_rank"] is None and nan_row["iv_percentile"] is None

    # The final 40.0 row: 20 valid prior obs (10..28, 19 of them) + current.
    last = rows[-1]
    assert last["atm_iv"] == 40.0
    # 40 is the max of the series -> rank 1.0; not diluted by the NaN bar.
    assert last["iv_rank"] == pytest.approx(1.0)
    assert last["iv_percentile"] == pytest.approx(1.0)


def test_build_index_rows_rank_null_until_min_history():
    # Fewer than _MIN_HISTORY_OBS valid observations -> rank/pct NULL but level
    # is still stored.
    bars = _bars(dt.date(2026, 1, 1), [20.0, 21.0, 22.0])
    rows = vi.build_index_rows("GVZ", bars)
    assert rows[-1]["atm_iv"] == 22.0
    assert rows[-1]["iv_rank"] is None
    assert rows[-1]["iv_percentile"] is None


def test_build_index_rows_backfill_makes_latest_rank_meaningful():
    # A full backfilled series: the latest row's rank resolves immediately
    # (this is the whole point of backfilling vs. #9's forward accrual).
    levels = [float(x) for x in range(20, 45)]  # 25 obs
    bars = _bars(dt.date(2026, 1, 1), levels)
    rows = vi.build_index_rows("GVZ", bars)
    assert rows[-1]["iv_rank"] == pytest.approx(1.0)


def test_build_index_rows_rank_lookback_is_bounded_to_trailing_window():
    # An ancient spike far outside the trailing window must NOT depress the
    # latest bar's rank: rank/percentile use only the trailing _RANK_WINDOW_DAYS.
    #   - one very high old bar (100.0), >_RANK_WINDOW_DAYS before the recent run
    #   - then >= _MIN_HISTORY_OBS recent bars rising 30.0 -> 49.0
    # Under full history the latest (49.0) ranks below 100.0 (rank ~0.27);
    # under the trailing window the old spike drops out and 49.0 is the max.
    old_bar = [(dt.date(2024, 1, 1), 100.0)]
    recent_start = dt.date(2026, 1, 1)  # ~2 years after the old spike
    recent = _bars(recent_start, [float(x) for x in range(30, 50)])  # 20 obs
    rows = vi.build_index_rows("GVZ", old_bar + recent)

    last = rows[-1]
    assert last["atm_iv"] == 49.0
    # Trailing window: 30..49 only -> 49 is the max -> rank 1.0, pct 1.0.
    # (Full-history would give (49-30)/(100-30) ~= 0.27 and pct 20/21.)
    assert last["iv_rank"] == pytest.approx(1.0)
    assert last["iv_percentile"] == pytest.approx(1.0)


def test_build_index_rows_in_window_history_still_counts():
    # Control for the bound: a high prior bar INSIDE the trailing window must
    # still cap the rank below 1.0 (the bound is a window, not a drop-everything).
    high_recent = [(dt.date(2026, 1, 1), 100.0)]  # within 365d of the last bar
    recent = _bars(dt.date(2026, 2, 1), [float(x) for x in range(30, 50)])  # 20 obs
    rows = vi.build_index_rows("GVZ", high_recent + recent)

    last = rows[-1]
    assert last["atm_iv"] == 49.0
    # 100.0 is still in-window -> it is the max, so 49.0 ranks below 1.0.
    assert last["iv_rank"] == pytest.approx((49.0 - 30.0) / (100.0 - 30.0))


# --- Config consumption ---------------------------------------------------

def test_ingest_entries_excludes_vix():
    symbols = load_symbols()
    entries = vi._ingest_entries(symbols)
    by_symbol = {sym for _, sym in entries}
    assert "VIX" not in by_symbol
    assert by_symbol == {"GVZ", "OVX"}


def test_ingest_entries_carries_ticker_and_stored_symbol():
    symbols = {
        "volatility_indices": {
            "indices": [
                {"ticker": "^VIX", "symbol": "VIX", "ingest": False},
                {"ticker": "^GVZ", "symbol": "GVZ", "ingest": True},
            ]
        }
    }
    assert vi._ingest_entries(symbols) == [("^GVZ", "GVZ")]


def test_backfill_days_from_config():
    symbols = {"volatility_indices": {"defaults": {"backfill_days": 365}}}
    assert vi._backfill_days(symbols) == 365


def test_backfill_days_default_when_absent():
    assert vi._backfill_days({}) == vi._DEFAULT_BACKFILL_DAYS


# --- Swappable provider ---------------------------------------------------

class _FakeProvider:
    def __init__(self, bars):
        self._bars = bars
        self.calls = []

    def daily_closes(self, ticker, start):
        self.calls.append((ticker, start))
        return [(d, c) for d, c in self._bars if d >= start]


def test_set_provider_swaps():
    original = vi._PROVIDER
    try:
        fake = _FakeProvider([])
        vi.set_provider(fake)
        assert vi._PROVIDER is fake
    finally:
        vi.set_provider(original)


# --- Idempotency / incremental (live Postgres or skip) -------------------

@pytest.fixture
def migrated_db(monkeypatch):
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for vol-indices idempotency test")

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


def test_backfill_then_rerun_does_not_duplicate(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 16)
    bars = _bars(today - dt.timedelta(days=4), [20.0, 21.0, 22.0, 23.0, 24.0])
    monkeypatch.setattr(vi, "_PROVIDER", _FakeProvider(bars))

    vi.ingest_index(engine, "^GVZ", "GVZ", backfill_days=1095, today=today)
    first = _count(engine, "GVZ")
    vi.ingest_index(engine, "^GVZ", "GVZ", backfill_days=1095, today=today)
    second = _count(engine, "GVZ")

    assert first == 5
    assert second == 5  # re-run upserts in place, no duplicates.


def test_rerun_upserts_value_in_place(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 16)
    day = today - dt.timedelta(days=1)

    monkeypatch.setattr(vi, "_PROVIDER", _FakeProvider([(day, 20.0)]))
    vi.ingest_index(engine, "^GVZ", "GVZ", backfill_days=1095, today=today)
    monkeypatch.setattr(vi, "_PROVIDER", _FakeProvider([(day, 25.0)]))
    vi.ingest_index(engine, "^GVZ", "GVZ", backfill_days=1095, today=today)

    assert _count(engine, "GVZ") == 1
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT atm_iv FROM iv_metrics WHERE symbol = 'GVZ'")
        ).scalar()
    assert float(value) == pytest.approx(25.0)


def test_second_run_is_incremental_not_full_backfill(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 16)
    bars = _bars(today - dt.timedelta(days=4), [20.0, 21.0, 22.0, 23.0, 24.0])

    fake = _FakeProvider(bars)
    monkeypatch.setattr(vi, "_PROVIDER", fake)
    vi.ingest_index(engine, "^GVZ", "GVZ", backfill_days=1095, today=today)
    vi.ingest_index(engine, "^GVZ", "GVZ", backfill_days=1095, today=today)

    # First call backfills from today-1095d; second starts at the latest stored
    # snapshot_date (the last bar), not the full window again.
    first_start, second_start = fake.calls[0][1], fake.calls[1][1]
    assert first_start == today - dt.timedelta(days=1095)
    assert second_start == bars[-1][0]


def test_holiday_null_persists_as_null(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 16)
    holiday = today - dt.timedelta(days=2)
    bars = [
        (today - dt.timedelta(days=3), 20.0),
        (holiday, float("nan")),
        (today - dt.timedelta(days=1), 21.0),
    ]
    monkeypatch.setattr(vi, "_PROVIDER", _FakeProvider(bars))
    vi.ingest_index(engine, "^OVX", "OVX", backfill_days=1095, today=today)

    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT atm_iv FROM iv_metrics "
                "WHERE symbol = 'OVX' AND snapshot_date = :d"
            ),
            {"d": holiday.isoformat()},
        ).scalar()
    assert value is None
