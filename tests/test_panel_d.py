"""Tests for Panel D (Volatility) — dashboard/panels/panel_d.py + the route.

The pure presentation/logic helpers (highlight classification, last-expected-
session staleness, cold-start N/20 labelling, off-hours vs accruing NULL, sort
ordering, formatting) are network-free and unit-tested directly. The render path
is exercised with a fake engine so it needs no live DB. A separate live-Postgres-
or-skip integration test (mirroring tests/test_health.py) migrates to head,
seeds a few iv_metrics rows, and asserts the route renders them — skipped when no
Postgres is reachable. FastAPI/httpx/jinja2 are optional in the bare test env, so
the route tests importorskip them.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url
from dashboard.panels import panel_d
from dashboard.panels.panel_d import (
    HIGHLIGHT_NONE,
    HIGHLIGHT_RICH_RV,
    HIGHLIGHT_SELL,
    classify_highlight,
    format_date,
    format_pct,
    is_stale,
    last_expected_session,
    rank_display,
)
from etl.sources.iv import _MIN_HISTORY_OBS

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- Conjunctive highlight ------------------------------------------------

def test_sell_candidate_needs_rank_and_positive_spread():
    assert classify_highlight(0.70, 0.05) == HIGHLIGHT_SELL
    assert classify_highlight(0.95, 0.01) == HIGHLIGHT_SELL


def test_rich_rank_with_nonpositive_spread_is_amber_not_sell():
    assert classify_highlight(0.85, 0.0) == HIGHLIGHT_RICH_RV
    assert classify_highlight(0.85, -0.04) == HIGHLIGHT_RICH_RV


def test_high_rank_alone_never_fires_sell_highlight():
    # rank >= 70 but spread NULL -> not a sell candidate.
    assert classify_highlight(0.90, None) == HIGHLIGHT_NONE


def test_below_threshold_is_default_regardless_of_spread():
    assert classify_highlight(0.69, 0.20) == HIGHLIGHT_NONE
    assert classify_highlight(0.10, -0.20) == HIGHLIGHT_NONE


def test_null_rank_is_default():
    assert classify_highlight(None, 0.10) == HIGHLIGHT_NONE


# --- Last expected session + staleness (weekend/holiday safe) -------------

def test_last_session_skips_weekend():
    # last_expected_session is the prior trading session (strictly before today).
    # Read on Mon 2026-06-15 -> prior session is Fri 2026-06-12 (weekend skipped).
    assert last_expected_session(dt.date(2026, 6, 15)) == dt.date(2026, 6, 12)  # Mon
    assert last_expected_session(dt.date(2026, 6, 13)) == dt.date(2026, 6, 12)  # Sat
    assert last_expected_session(dt.date(2026, 6, 16)) == dt.date(2026, 6, 15)  # Tue


def test_last_session_skips_us_holiday():
    # 2026-07-03 is the observed Independence Day holiday (Fri); 07-04 is Sat.
    # Prior session before Mon 2026-07-06 is Thu 2026-07-02 (holiday+weekend skipped).
    assert last_expected_session(dt.date(2026, 7, 6)) == dt.date(2026, 7, 2)


def test_friday_snapshot_not_stale_on_monday():
    # A Friday snapshot read on the following Monday is NOT stale (no session
    # in between) — calendar-day arithmetic would falsely flag it.
    assert is_stale(dt.date(2026, 6, 12), dt.date(2026, 6, 15)) is False


def test_old_snapshot_is_stale():
    assert is_stale(dt.date(2026, 6, 10), dt.date(2026, 6, 15)) is True


def test_null_snapshot_is_not_stale():
    assert is_stale(None, dt.date(2026, 6, 15)) is False


# --- Cold-start N/20 vs off-hours NULL (same NULL, opposite meaning) -------

def test_accruing_shows_n_over_min_obs():
    # NULL rank with too-few snapshots -> "— (N/20)" mirroring _MIN_HISTORY_OBS.
    label = rank_display(iv_rank=None, atm_iv=0.31, snapshot_count=7)
    assert label == f"— (7/{_MIN_HISTORY_OBS})"


def test_accruing_threshold_mirrors_etl_constant():
    # The denominator is _MIN_HISTORY_OBS, not a hardcoded 20.
    label = rank_display(iv_rank=None, atm_iv=0.31, snapshot_count=0)
    assert label.endswith(f"/{_MIN_HISTORY_OBS})")


def test_off_hours_null_labelled_differently_from_accruing():
    # Enough history, but today's chain is NULL -> distinct "no chain" label.
    label = rank_display(iv_rank=None, atm_iv=None, snapshot_count=_MIN_HISTORY_OBS + 5)
    assert label == "— (no chain)"
    accruing = rank_display(iv_rank=None, atm_iv=None, snapshot_count=3)
    assert accruing != label


def test_real_rank_renders_as_percentage():
    assert rank_display(iv_rank=0.72, atm_iv=0.30, snapshot_count=50) == "72.0%"


# --- Formatting -----------------------------------------------------------

def test_format_pct_and_thousands():
    assert format_pct(0.305) == "30.5%"
    assert format_pct(None) == "—"
    # large magnitude carries a thousands separator.
    assert format_pct(15.0) == "1,500.0%"


def test_format_date():
    assert format_date(dt.date(2026, 6, 16)) == "2026-06-16"
    assert format_date(None) == "—"


# --- Sort ordering: iv_rank DESC NULLS LAST -------------------------------

class _FakeRow:
    def __init__(self, **kw):
        self._m = kw

    @property
    def symbol(self):
        return self._m["symbol"]

    @property
    def n(self):
        return self._m["n"]

    @property
    def _mapping(self):
        return self._m


class _FakeConn:
    def __init__(self, latest_rows, count_rows):
        self._latest = latest_rows
        self._counts = count_rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "COUNT(*)" in sql:
            return list(self._counts)
        return list(self._latest)


class _FakeEngine:
    def __init__(self, latest_rows, count_rows):
        self._latest = latest_rows
        self._counts = count_rows

    def connect(self):
        return _FakeConn(self._latest, self._counts)


def _latest(symbol, **kw):
    base = dict(
        symbol=symbol,
        snapshot_date=dt.date(2026, 6, 16),
        atm_iv=0.30,
        iv_rank=None,
        iv_percentile=None,
        rv_30=0.25,
        iv_rv_spread=0.05,
    )
    base.update(kw)
    return _FakeRow(**base)


def test_build_view_orders_rank_desc_nulls_last():
    # GC rank 0.9, SI rank 0.2, CL rank NULL -> order GC, SI, then CL (null last).
    latest = [
        _latest("GC", iv_rank=0.90),
        _latest("SI", iv_rank=0.20),
        _latest("CL", iv_rank=None),
    ]
    counts = [_FakeRow(symbol="GC", n=50), _FakeRow(symbol="SI", n=50), _FakeRow(symbol="CL", n=3)]
    view = panel_d.build_view(_FakeEngine(latest, counts), today=dt.date(2026, 6, 16))
    ordered = [r.symbol for r in view.underlyings]
    assert ordered[:3] == ["GC", "SI", "CL"]
    assert view.underlyings[-1].symbol == "CL"  # null rank sinks to the bottom.


def test_build_view_accruing_row_keeps_iv_but_nulls_rank():
    latest = [_latest("CL", iv_rank=None, atm_iv=0.40)]
    counts = [_FakeRow(symbol="CL", n=4)]
    view = panel_d.build_view(_FakeEngine(latest, counts), today=dt.date(2026, 6, 16))
    cl = next(r for r in view.underlyings if r.symbol == "CL")
    assert cl.atm_iv == 0.40
    assert cl.rank_label == f"— (4/{_MIN_HISTORY_OBS})"
    assert cl.highlight == HIGHLIGHT_NONE


def test_build_view_separates_index_strip_from_underlyings():
    latest = [
        _latest("GC", iv_rank=0.90),
        _latest("GVZ", iv_rank=0.80, rv_30=None, iv_rv_spread=None),
        _latest("OVX", iv_rank=0.85, rv_30=None, iv_rv_spread=None),
    ]
    counts = [_FakeRow(symbol="GC", n=50)]
    view = panel_d.build_view(_FakeEngine(latest, counts), today=dt.date(2026, 6, 16))
    underlying_syms = {r.symbol for r in view.underlyings}
    index_syms = {r.symbol for r in view.indices}
    assert "GVZ" in index_syms and "OVX" in index_syms
    assert "GVZ" not in underlying_syms and "OVX" not in underlying_syms


def test_build_view_empty_when_no_rows():
    view = panel_d.build_view(_FakeEngine([], []), today=dt.date(2026, 6, 16))
    assert view.is_empty is True


# --- DB-failure isolation: never 500, render the honest error state -------

class _RaisingConn:
    def __init__(self, exc):
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        raise self._exc


class _RaisingEngine:
    """Engine whose connection raises on the first SELECT — models a DB-down
    OperationalError or a pre-migration ProgrammingError without a live DB."""

    def __init__(self, exc):
        self._exc = exc

    def connect(self):
        return _RaisingConn(self._exc)


def _operational_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


def _programming_error() -> ProgrammingError:
    return ProgrammingError("SELECT ...", {}, Exception('relation "iv_metrics" does not exist'))


def test_build_view_db_unreachable_returns_error_state_not_raise():
    view = panel_d.build_view(_RaisingEngine(_operational_error()), today=dt.date(2026, 6, 16))
    assert view.error is True
    assert view.underlyings == [] and view.indices == []
    assert view.last_session == dt.date(2026, 6, 15)  # still computed, no wall clock.


def test_build_view_missing_table_returns_error_state_not_raise():
    view = panel_d.build_view(_RaisingEngine(_programming_error()), today=dt.date(2026, 6, 16))
    assert view.error is True
    assert view.underlyings == [] and view.indices == []


# --- Route render (no live DB; fake engine via monkeypatch) ---------------

def test_panel_d_route_renders_table(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("jinja2")
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    import importlib

    import dashboard.main as dashboard_main

    importlib.reload(dashboard_main)
    from fastapi.testclient import TestClient

    sell = panel_d.UnderlyingRow(
        symbol="GC", iv_proxy="GLD", atm_iv=0.32, iv_rank=0.80, iv_percentile=0.78,
        rv_30=0.25, iv_rv_spread=0.07, snapshot_date=dt.date(2026, 6, 16),
        snapshot_count=50, highlight=HIGHLIGHT_SELL, stale=False,
        rank_label="80.0%", percentile_label="78.0%",
    )
    accruing = panel_d.UnderlyingRow(
        symbol="CL", iv_proxy="USO", atm_iv=0.40, iv_rank=None, iv_percentile=None,
        rv_30=0.30, iv_rv_spread=0.10, snapshot_date=dt.date(2026, 6, 16),
        snapshot_count=4, highlight=HIGHLIGHT_NONE, stale=False,
        rank_label="— (4/20)", percentile_label="— (4/20)",
    )
    gvz = panel_d.IndexRow(
        symbol="GVZ", name="CBOE Gold Volatility Index", atm_iv=0.18,
        iv_rank=0.60, iv_percentile=0.55, snapshot_date=dt.date(2026, 6, 16), stale=False,
    )
    view = panel_d.PanelDView(
        underlyings=[sell, accruing], indices=[gvz], last_session=dt.date(2026, 6, 16)
    )
    monkeypatch.setattr(dashboard_main.panel_d, "build_view", lambda *a, **k: view)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/d")

    assert resp.status_code == 200
    body = resp.text
    assert "Panel D" in body
    assert "GLD" in body and "USO" in body  # iv_proxy surfaced.
    assert "sell-candidate" in body  # conjunctive highlight class rendered.
    assert "— (4/20)" in body  # accruing label rendered.
    assert "GVZ" in body and "NOT tradeable" in body  # index strip marked context.
    assert "<td>OVX</td>" not in body  # only ingested indices.


def test_panel_d_route_empty_state(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("jinja2")
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    import importlib

    import dashboard.main as dashboard_main

    importlib.reload(dashboard_main)
    from fastapi.testclient import TestClient

    empty = panel_d.PanelDView(underlyings=[], indices=[], last_session=dt.date(2026, 6, 16))
    monkeypatch.setattr(dashboard_main.panel_d, "build_view", lambda *a, **k: empty)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/d")

    assert resp.status_code == 200
    assert "No volatility snapshots yet" in resp.text


def test_panel_d_route_renders_error_state_not_500_on_db_failure(monkeypatch):
    # The route must serve a 200 with the honest "data unavailable" message when
    # the DB read fails — not propagate a 500. build_view already degrades the
    # OperationalError/ProgrammingError to error=True; the route renders it.
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("jinja2")
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    import importlib

    import dashboard.main as dashboard_main

    importlib.reload(dashboard_main)
    from fastapi.testclient import TestClient

    errored = panel_d.PanelDView(
        underlyings=[], indices=[], last_session=dt.date(2026, 6, 16), error=True
    )
    monkeypatch.setattr(dashboard_main.panel_d, "build_view", lambda *a, **k: errored)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/d")

    assert resp.status_code == 200
    assert "currently unavailable" in resp.text
    # No fabricated table / leaked internals.
    assert "No volatility snapshots yet" not in resp.text
    assert "iv_metrics" not in resp.text


# --- Live-Postgres-or-skip integration (mirrors tests/test_health.py) -----

@pytest.fixture
def seeded_engine(monkeypatch):
    alembic_config = pytest.importorskip("alembic.config")
    alembic_command = pytest.importorskip("alembic.command")
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for Panel D integration test")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    alembic_command.upgrade(cfg, "head")

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM iv_metrics WHERE symbol IN ('GC','GVZ')"))
        conn.execute(
            text(
                "INSERT INTO iv_metrics "
                "(symbol, snapshot_date, atm_iv, iv_rank, iv_percentile, rv_30, iv_rv_spread, source) "
                "VALUES (:s,:d,:a,:r,:p,:rv,:sp,'yfinance')"
            ),
            {"s": "GC", "d": dt.date.today(), "a": 0.30, "r": 0.80, "p": 0.75, "rv": 0.25, "sp": 0.05},
        )
        conn.execute(
            text(
                "INSERT INTO iv_metrics "
                "(symbol, snapshot_date, atm_iv, iv_rank, iv_percentile, rv_30, iv_rv_spread, source) "
                "VALUES (:s,:d,:a,:r,:p,NULL,NULL,'yfinance')"
            ),
            {"s": "GVZ", "d": dt.date.today(), "a": 0.18, "r": 0.60, "p": 0.55},
        )
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM iv_metrics WHERE symbol IN ('GC','GVZ')"))
        engine.dispose()


def test_build_view_reads_seeded_rows(seeded_engine):
    view = panel_d.build_view(seeded_engine, today=dt.date.today())
    gc = next((r for r in view.underlyings if r.symbol == "GC"), None)
    assert gc is not None
    assert gc.iv_proxy == "GLD"
    assert gc.highlight == HIGHLIGHT_SELL  # rank 0.80 AND spread 0.05 > 0.
    gvz = next((r for r in view.indices if r.symbol == "GVZ"), None)
    assert gvz is not None and gvz.atm_iv == 0.18
