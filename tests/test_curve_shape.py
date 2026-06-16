"""Tests for the curve-shape ETL source (etl/sources/curve_shape.py).

Pure transforms (annualized-slope math, sign/structure classification with the
deadband, negative/zero-front guard, missing-leg → NULL, the month-code/ticker
resolver) are network-free. The provider is swapped for a fake (no yfinance
import). The DB upsert + idempotency tests run against a live Postgres when
reachable and are skipped otherwise, matching tests/test_iv.py /
tests/test_vol_indices.py.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url, load_curve_config
from etl.sources import curve_shape as cs

alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- Price cleaning -------------------------------------------------------

def test_clean_price_passes_through():
    assert cs._clean_price(70.5) == 70.5
    assert cs._clean_price(-37.0) == -37.0  # negative front legitimately stored.


def test_clean_price_rejects_nan_none_and_garbage():
    assert cs._clean_price(None) is None
    assert cs._clean_price(float("nan")) is None
    assert cs._clean_price("notanumber") is None


# --- Annualized slope math ------------------------------------------------

def test_slope_annualizes_carry():
    # back 76, front 70 over 6 months: (6/70) / (6/12) = 0.085714... * 2.
    expected = ((76.0 - 70.0) / 70.0) / (6 / 12.0)
    assert cs.slope(70.0, 76.0, 6) == pytest.approx(expected)
    assert cs.slope(70.0, 76.0, 6) > 0  # contango sign.


def test_slope_backwardation_is_negative():
    assert cs.slope(80.0, 74.0, 6) < 0


def test_slope_months_normalize():
    # Same raw % spread, different months_out → larger annualized slope for the
    # nearer deferred leg.
    near = cs.slope(100.0, 105.0, 3)
    far = cs.slope(100.0, 105.0, 12)
    assert near > far > 0


def test_slope_null_when_a_leg_missing():
    assert cs.slope(None, 76.0, 6) is None
    assert cs.slope(70.0, None, 6) is None


def test_slope_guards_zero_and_negative_front():
    # April-2020 WTI: front <= 0 must give NULL, never ±inf / NaN.
    assert cs.slope(0.0, 5.0, 6) is None
    assert cs.slope(-37.0, 5.0, 6) is None


def test_slope_guards_bad_months_out():
    assert cs.slope(70.0, 76.0, 0) is None
    assert cs.slope(70.0, 76.0, -6) is None


# --- Structure classification with deadband -------------------------------

def test_classify_contango_backwardation_flat():
    eps = 0.005
    assert cs.classify(0.10, eps) == "contango"
    assert cs.classify(-0.10, eps) == "backwardation"
    assert cs.classify(0.0, eps) == "flat"
    assert cs.classify(0.002, eps) == "flat"   # inside deadband
    assert cs.classify(-0.002, eps) == "flat"


def test_classify_deadband_boundaries_are_flat():
    eps = 0.005
    assert cs.classify(eps, eps) == "flat"     # exactly +eps is within [-eps,+eps]
    assert cs.classify(-eps, eps) == "flat"
    assert cs.classify(eps + 1e-9, eps) == "contango"
    assert cs.classify(-eps - 1e-9, eps) == "backwardation"


def test_classify_null_when_slope_null():
    assert cs.classify(None) is None


# --- Month-code / deferred-ticker resolver --------------------------------

def test_deferred_month_code_within_year():
    # June (6) + 6 → December (Z), no year wrap.
    code, year_offset = cs.deferred_month_code(6, 6)
    assert code == "Z"
    assert year_offset == 0


def test_deferred_month_code_wraps_year():
    # October (10) + 6 → April (J) of next year.
    code, year_offset = cs.deferred_month_code(10, 6)
    assert code == "J"
    assert year_offset == 1


def test_deferred_month_code_each_month_letter():
    # Jan + 0..11 walks the full F..Z alphabet in order.
    codes = [cs.deferred_month_code(1, m)[0] for m in range(12)]
    assert codes == list(cs._MONTH_CODES)


def test_build_deferred_ticker_shape():
    today = dt.date(2026, 6, 16)  # June + 6 → December 2026 → CLZ26.NYM
    assert cs.build_deferred_ticker("CL", ".NYM", today, 6) == "CLZ26.NYM"


def test_build_deferred_ticker_year_rollover():
    today = dt.date(2026, 10, 1)  # Oct + 6 → April 2027 → CLJ27.NYM
    assert cs.build_deferred_ticker("CL", ".NYM", today, 6) == "CLJ27.NYM"


# --- Row building ---------------------------------------------------------

def test_build_row_full_contango():
    row = cs.build_row("CL", dt.date(2026, 6, 16), 70.0, 76.0, 6, eps=0.005)
    assert row["symbol"] == "CL"
    assert row["date"] == "2026-06-16"
    assert row["front_price"] == 70.0
    assert row["back_price"] == 76.0
    assert row["spread"] == pytest.approx(6.0)
    assert row["slope_pct"] > 0
    assert row["structure"] == "contango"
    assert row["source"] == "yfinance"


def test_build_row_missing_deferred_leg_writes_front_only():
    # A missing deferred leg → front_price only; back/spread/slope/structure NULL.
    row = cs.build_row("NG", dt.date(2026, 6, 16), 3.20, None, 6)
    assert row["front_price"] == 3.20
    assert row["back_price"] is None
    assert row["spread"] is None
    assert row["slope_pct"] is None
    assert row["structure"] is None


def test_build_row_nan_deferred_leg_is_null():
    row = cs.build_row("NG", dt.date(2026, 6, 16), 3.20, float("nan"), 6)
    assert row["back_price"] is None
    assert row["spread"] is None
    assert row["slope_pct"] is None
    assert row["structure"] is None


def test_build_row_negative_front_guards_slope_but_keeps_prices():
    # April-2020 WTI: front <= 0 stores both prices + spread but NULLs slope/structure.
    row = cs.build_row("CL", dt.date(2020, 4, 20), -37.0, 20.0, 6)
    assert row["front_price"] == -37.0
    assert row["back_price"] == 20.0
    assert row["spread"] == pytest.approx(57.0)
    assert row["slope_pct"] is None
    assert row["structure"] is None


def test_build_row_flat_within_deadband():
    # Tiny carry inside the deadband → 'flat', not a contango/backwardation flip.
    row = cs.build_row("CL", dt.date(2026, 6, 16), 100.0, 100.1, 6, eps=0.05)
    assert row["structure"] == "flat"


# --- Config consumption ---------------------------------------------------

def test_curve_config_ships_energy_underlyings():
    cfg = load_curve_config()
    symbols = {u["symbol"] for u in cs._underlyings(cfg)}
    assert {"CL", "BZ", "NG", "RB", "HO"} <= symbols


def test_curve_config_specs_are_complete():
    cfg = load_curve_config()
    for u in cs._underlyings(cfg):
        assert {"symbol", "front_ticker", "deferred_root", "suffix", "months_out"} <= set(u)


def test_flat_eps_from_config():
    assert cs._flat_eps({"defaults": {"flat_eps": 0.01}}) == 0.01


def test_flat_eps_default_when_absent():
    assert cs._flat_eps({}) == cs._DEFAULT_FLAT_EPS


# --- Swappable provider ---------------------------------------------------

class _FakeProvider:
    def __init__(self, closes_by_ticker):
        self._closes = closes_by_ticker
        self.calls = []

    def latest_close(self, ticker):
        self.calls.append(ticker)
        return self._closes.get(ticker)


def test_set_provider_swaps():
    original = cs._PROVIDER
    try:
        fake = _FakeProvider({})
        cs.set_provider(fake)
        assert cs._PROVIDER is fake
    finally:
        cs.set_provider(original)


def test_get_curve_returns_front_and_back():
    original = cs._PROVIDER
    try:
        cs.set_provider(_FakeProvider({"CL=F": 70.0, "CLZ26.NYM": 76.0}))
        assert cs.get_curve("CL=F", "CLZ26.NYM") == (70.0, 76.0)
    finally:
        cs.set_provider(original)


def test_get_curve_missing_deferred_is_none():
    original = cs._PROVIDER
    try:
        cs.set_provider(_FakeProvider({"CL=F": 70.0}))
        assert cs.get_curve("CL=F", "CLZ26.NYM") == (70.0, None)
    finally:
        cs.set_provider(original)


# --- Idempotency / upsert (live Postgres or skip) ------------------------

_SPEC = {
    "symbol": "CL",
    "front_ticker": "CL=F",
    "deferred_root": "CL",
    "suffix": ".NYM",
    "months_out": 6,
}


@pytest.fixture
def migrated_db(monkeypatch):
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for curve-shape idempotency test")

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
            text("SELECT count(*) FROM curve_shape WHERE symbol = :s"),
            {"s": symbol},
        ).scalar()


def test_upsert_then_rerun_does_not_duplicate(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 16)
    deferred = cs.build_deferred_ticker("CL", ".NYM", today, 6)
    monkeypatch.setattr(cs, "_PROVIDER", _FakeProvider({"CL=F": 70.0, deferred: 76.0}))

    cs.ingest_underlying(engine, _SPEC, today, eps=0.005)
    first = _count(engine, "CL")
    cs.ingest_underlying(engine, _SPEC, today, eps=0.005)
    second = _count(engine, "CL")

    assert first == 1
    assert second == 1  # re-run upserts in place, no duplicate row.


def test_rerun_upserts_value_in_place(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 16)
    deferred = cs.build_deferred_ticker("CL", ".NYM", today, 6)

    monkeypatch.setattr(cs, "_PROVIDER", _FakeProvider({"CL=F": 70.0, deferred: 76.0}))
    cs.ingest_underlying(engine, _SPEC, today, eps=0.005)
    monkeypatch.setattr(cs, "_PROVIDER", _FakeProvider({"CL=F": 70.0, deferred: 65.0}))
    cs.ingest_underlying(engine, _SPEC, today, eps=0.005)

    assert _count(engine, "CL") == 1
    with engine.connect() as conn:
        back, structure = conn.execute(
            text("SELECT back_price, structure FROM curve_shape WHERE symbol = 'CL'")
        ).one()
    assert float(back) == pytest.approx(65.0)
    assert structure == "backwardation"  # 65 < 70 → backwardation after re-run.


def test_missing_deferred_leg_persists_null(migrated_db, monkeypatch):
    engine = migrated_db
    today = dt.date(2026, 6, 16)
    monkeypatch.setattr(cs, "_PROVIDER", _FakeProvider({"CL=F": 70.0}))
    cs.ingest_underlying(engine, _SPEC, today, eps=0.005)

    with engine.connect() as conn:
        front, back, spread, slope_pct, structure = conn.execute(
            text(
                "SELECT front_price, back_price, spread, slope_pct, structure "
                "FROM curve_shape WHERE symbol = 'CL'"
            )
        ).one()
    assert float(front) == pytest.approx(70.0)
    assert back is None
    assert spread is None
    assert slope_pct is None
    assert structure is None
