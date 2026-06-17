"""Tests for Panel A (Macro / Cross-Asset) — dashboard/panels/panel_a.py + route.

The pure presentation/logic helpers (frequency-aware change calcs — daily
~1m/~3m level change, monthly MoM/YoY incl. the inflation-index YoY headline
from the 12-months-prior row, quarterly prior-quarter change; frequency-aware
staleness; nearest-prior selection with the no-prior degradation; neutral
arrows; formatting) are network-free and unit-tested directly. The render path
is exercised with a fake engine so it needs no live DB. A separate live-Postgres-
or-skip integration test (mirroring tests/test_health.py) migrates to head,
seeds a few macro_metrics rows, and asserts build_view groups them — skipped
when no Postgres is reachable. FastAPI/httpx/jinja2 are optional in the bare
test env, so the route tests importorskip them.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url
from dashboard.panels import panel_a
from dashboard.panels.panel_a import (
    FREQ_DAILY,
    FREQ_MONTHLY,
    FREQ_QUARTERLY,
    GROUP_INFLATION,
    GROUP_REAL_RATES,
    GROUP_RISK_REGIME,
    GROUP_USD,
    direction_arrow,
    format_date,
    format_level,
    format_pct_change,
    format_points,
    is_stale,
    level_change,
    nearest_prior,
    pct_change,
)

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- Frequency-aware staleness --------------------------------------------

def test_daily_stale_when_before_prior_session():
    # Tue 2026-06-16; prior session is Mon 06-15. A row from 06-10 is stale.
    today = dt.date(2026, 6, 16)
    assert is_stale(dt.date(2026, 6, 10), FREQ_DAILY, today) is True
    assert is_stale(dt.date(2026, 6, 15), FREQ_DAILY, today) is False


def test_daily_friday_row_read_monday_not_stale():
    # Mon 2026-06-15; prior trading session is Fri 06-12 (weekend skipped).
    today = dt.date(2026, 6, 15)
    assert is_stale(dt.date(2026, 6, 12), FREQ_DAILY, today) is False


def test_monthly_freshest_reference_print_not_stale():
    # FRED dates May CPI as 2026-05-01 but only publishes it ~June 11. On
    # 2026-06-17 that May print is the freshest expected monthly print — it must
    # NOT flag STALE despite being ~47 days old by raw day-age (the old bug).
    today = dt.date(2026, 6, 17)
    assert is_stale(dt.date(2026, 5, 1), FREQ_MONTHLY, today) is False


def test_monthly_missing_period_is_stale():
    # A genuinely missing month (March stored when April should already be out)
    # IS stale.
    today = dt.date(2026, 6, 17)
    assert is_stale(dt.date(2026, 3, 1), FREQ_MONTHLY, today) is True


def test_quarterly_freshest_reference_print_not_stale():
    # Q1-2026 (dated 2026-01-01, quarter ends 2026-03-31) is published with a lag
    # in spring; on 2026-06-17 it is the freshest expected quarterly print and
    # must NOT flag STALE.
    today = dt.date(2026, 6, 17)
    assert is_stale(dt.date(2026, 1, 1), FREQ_QUARTERLY, today) is False


def test_quarterly_missing_period_is_stale():
    # A full quarter overdue: Q3-2025 (2025-07-01) stored on 2026-06-17, when
    # Q4-2025 and Q1-2026 should both already have been published — IS stale.
    today = dt.date(2026, 6, 17)
    assert is_stale(dt.date(2025, 7, 1), FREQ_QUARTERLY, today) is True


def test_null_date_never_stale():
    today = dt.date(2026, 6, 17)
    assert is_stale(None, FREQ_DAILY, today) is False
    assert is_stale(None, FREQ_MONTHLY, today) is False
    assert is_stale(None, FREQ_QUARTERLY, today) is False


# --- Nearest-prior selection (no-prior degradation) -----------------------

def test_nearest_prior_picks_on_or_before_target():
    hist = [
        (dt.date(2026, 6, 16), 105.0),
        (dt.date(2026, 5, 15), 102.0),
        (dt.date(2026, 4, 15), 100.0),
    ]
    got = nearest_prior(hist, dt.date(2026, 5, 20))
    assert got == (dt.date(2026, 5, 15), 102.0)


def test_nearest_prior_skips_null_values():
    hist = [
        (dt.date(2026, 6, 16), 105.0),
        (dt.date(2026, 5, 15), None),  # FRED "." sentinel.
        (dt.date(2026, 4, 15), 100.0),
    ]
    got = nearest_prior(hist, dt.date(2026, 5, 20))
    assert got == (dt.date(2026, 4, 15), 100.0)


def test_nearest_prior_none_when_out_of_floor_range():
    hist = [(dt.date(2025, 1, 1), 90.0)]
    # Target far before any stored row, floored -> no comparable prior.
    got = nearest_prior(
        hist, dt.date(2026, 5, 20), floor=dt.date(2026, 4, 1)
    )
    assert got is None


def test_nearest_prior_empty_history_is_none():
    assert nearest_prior([], dt.date(2026, 5, 20)) is None


# --- Change calcs + NULL handling -----------------------------------------

def test_level_change_points():
    assert level_change(4.50, (dt.date(2026, 5, 1), 4.20)) == pytest.approx(0.30)


def test_level_change_null_latest_or_no_prior():
    assert level_change(None, (dt.date(2026, 5, 1), 4.2)) is None
    assert level_change(4.5, None) is None


def test_pct_change_fraction():
    assert pct_change(309.0, (dt.date(2025, 5, 1), 300.0)) == pytest.approx(0.03)


def test_pct_change_guards_nonpositive_base():
    assert pct_change(10.0, (dt.date(2025, 5, 1), 0.0)) is None
    assert pct_change(10.0, (dt.date(2025, 5, 1), -1.0)) is None


def test_pct_change_no_prior():
    assert pct_change(10.0, None) is None


# --- Neutral arrows (no colored good/bad) ---------------------------------

def test_direction_arrow_neutral_and_flat_and_unknown():
    assert direction_arrow(1.0) == "↑"
    assert direction_arrow(-1.0) == "↓"
    assert direction_arrow(0.0) == "→"
    assert direction_arrow(None) == ""


# --- Formatting + honest no-prior -----------------------------------------

def test_format_level_rate_vs_index():
    assert format_level(4.531, is_rate=True) == "4.53%"
    assert format_level(1234.5, is_rate=False) == "1,234.50"
    assert format_level(None, is_rate=True) == "—"


def test_format_points_signed_and_no_prior():
    assert format_points(0.3, is_rate=True) == "+0.30 pp"
    assert format_points(-0.3, is_rate=True) == "-0.30 pp"
    assert format_points(1234.0, is_rate=False) == "+1,234.00"
    assert format_points(None, is_rate=True) == "— (no prior)"


def test_format_pct_change_signed_and_no_prior():
    assert format_pct_change(0.031) == "+3.1%"
    assert format_pct_change(-0.02) == "-2.0%"
    assert format_pct_change(None) == "— (no prior)"


def test_format_date():
    assert format_date(dt.date(2026, 6, 16)) == "2026-06-16"
    assert format_date(None) == "—"


# --- Render path: fake engine (no live DB) --------------------------------

class _FakeRow:
    def __init__(self, **kw):
        self._m = kw

    @property
    def series_id(self):
        return self._m["series_id"]

    @property
    def date(self):
        return self._m["date"]

    @property
    def value(self):
        return self._m["value"]

    @property
    def _mapping(self):
        return self._m


class _FakeConn:
    def __init__(self, latest_rows, history_rows):
        self._latest = latest_rows
        self._history = history_rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "date >=" in sql:
            return list(self._history)
        return list(self._latest)


class _FakeEngine:
    def __init__(self, latest_rows, history_rows):
        self._latest = latest_rows
        self._history = history_rows

    def connect(self):
        return _FakeConn(self._latest, self._history)


def _row(series_id, date, value):
    return _FakeRow(series_id=series_id, date=date, value=value)


def _monthly_history(series_id, latest_date, latest_val, mom_val, yoy_val):
    return [
        _row(series_id, latest_date, latest_val),
        _row(series_id, latest_date - dt.timedelta(days=30), mom_val),
        _row(series_id, latest_date - dt.timedelta(days=365), yoy_val),
    ]


def test_inflation_index_headlines_yoy_from_12mo_prior():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 5, 1)
    latest = [_row("CPIAUCSL", latest_date, 309.0)]
    history = _monthly_history("CPIAUCSL", latest_date, 309.0, 308.0, 300.0)
    view = panel_a.build_view(_FakeEngine(latest, history), today=today)
    cpi = _find_row(view, "CPIAUCSL")
    assert cpi.group == GROUP_INFLATION
    assert cpi.headline_caption == "YoY"
    assert cpi.headline_label == "+3.0%"  # (309-300)/300.
    # Raw index appears only secondarily, not as the headline.
    secondary_caps = [c for c, _ in cpi.secondary]
    assert "index" in secondary_caps
    assert cpi.level_label == "309.00"


def test_monthly_rate_headlines_mom_level_change():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 5, 1)
    latest = [_row("UNRATE", latest_date, 4.1)]
    history = _monthly_history("UNRATE", latest_date, 4.1, 4.0, 3.8)
    view = panel_a.build_view(_FakeEngine(latest, history), today=today)
    unrate = _find_row(view, "UNRATE")
    assert unrate.headline_caption == "MoM"
    assert unrate.headline_label == "+0.10 pp"  # 4.1 - 4.0, rate -> points.
    assert unrate.is_rate is True


def test_daily_headlines_one_month_level_change_with_arrow():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [_row("DGS10", latest_date, 4.50)]
    history = [
        _row("DGS10", latest_date, 4.50),
        _row("DGS10", latest_date - dt.timedelta(days=30), 4.30),
        _row("DGS10", latest_date - dt.timedelta(days=90), 4.10),
    ]
    view = panel_a.build_view(_FakeEngine(latest, history), today=today)
    dgs = _find_row(view, "DGS10")
    assert dgs.group == GROUP_REAL_RATES
    assert dgs.headline_caption == "~1m change"
    assert dgs.headline_arrow == "↑"
    assert dgs.headline_label == "+0.20 pp"  # 4.50 - 4.30.
    assert any("~3m change" in c for c, _ in dgs.secondary)


def test_quarterly_marked_quarterly():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 3, 31)
    latest = [_row("GDPC1", latest_date, 23000.0)]
    history = [
        _row("GDPC1", latest_date, 23000.0),
        _row("GDPC1", dt.date(2025, 12, 31), 22850.0),
    ]
    view = panel_a.build_view(_FakeEngine(latest, history), today=today)
    gdp = _find_row(view, "GDPC1")
    assert "quarterly" in gdp.headline_caption


def test_null_latest_does_not_break_calc():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [_row("DTWEXBGS", latest_date, None)]  # "." sentinel today.
    history = [_row("DTWEXBGS", latest_date, None)]
    view = panel_a.build_view(_FakeEngine(latest, history), today=today)
    usd = _find_row(view, "DTWEXBGS")
    assert usd.group == GROUP_USD
    assert usd.level_label == "—"
    assert usd.headline_label == "— (no prior)"


def test_missing_prior_degrades_to_no_prior():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [_row("VIXCLS", latest_date, 18.0)]
    history = [_row("VIXCLS", latest_date, 18.0)]  # no older row.
    view = panel_a.build_view(_FakeEngine(latest, history), today=today)
    vix = _find_row(view, "VIXCLS")
    assert vix.group == GROUP_RISK_REGIME
    assert "no prior" in vix.headline_label


def test_no_option_action_language_in_any_row():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [_row("VIXCLS", latest_date, 18.0)]
    history = [
        _row("VIXCLS", latest_date, 18.0),
        _row("VIXCLS", latest_date - dt.timedelta(days=30), 22.0),
    ]
    view = panel_a.build_view(_FakeEngine(latest, history), today=today)
    banned = ["sell", "buy", "rich", "candidate", "premium", "short put", "short call"]
    for group in view.groups:
        for row in group.rows:
            blob = " ".join(
                [row.label, row.headline_label, row.headline_caption]
                + [v for _, v in row.secondary]
            ).lower()
            for word in banned:
                assert word not in blob


def test_vix_carries_no_rank_or_percentile():
    today = dt.date(2026, 6, 17)
    latest = [_row("VIXCLS", dt.date(2026, 6, 16), 18.0)]
    view = panel_a.build_view(_FakeEngine(latest, latest), today=today)
    vix = _find_row(view, "VIXCLS")
    # The dataclass has no rank/percentile fields at all (regime, not a signal).
    assert not hasattr(vix, "iv_rank")
    assert not hasattr(vix, "iv_percentile")


def test_five_buckets_present_in_order():
    view = panel_a.build_view(_FakeEngine([], []), today=dt.date(2026, 6, 17))
    keys = [g.key for g in view.groups]
    assert keys == ["usd", "real_rates", "inflation", "growth_labor", "risk_regime"]


def _find_row(view, series_id):
    for group in view.groups:
        for row in group.rows:
            if row.series_id == series_id:
                return row
    raise AssertionError(f"series {series_id} not in view")


# --- DB-failure isolation: never 500, honest error state ------------------

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
    def __init__(self, exc):
        self._exc = exc

    def connect(self):
        return _RaisingConn(self._exc)


def _operational_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


def _programming_error() -> ProgrammingError:
    return ProgrammingError(
        "SELECT ...", {}, Exception('relation "macro_metrics" does not exist')
    )


def test_build_view_db_unreachable_returns_error_state_not_raise():
    view = panel_a.build_view(
        _RaisingEngine(_operational_error()), today=dt.date(2026, 6, 17)
    )
    assert view.error is True
    assert view.groups == []


def test_build_view_missing_table_returns_error_state_not_raise():
    view = panel_a.build_view(
        _RaisingEngine(_programming_error()), today=dt.date(2026, 6, 17)
    )
    assert view.error is True


# --- Route render (no live DB; fake engine via monkeypatch) ---------------

def _reload_main(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("jinja2")
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    import importlib

    import dashboard.main as dashboard_main

    importlib.reload(dashboard_main)
    return dashboard_main


def test_panel_a_route_renders_buckets_and_caveats(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    usd = panel_a.MacroRow(
        series_id="DTWEXBGS", label="Trade-Weighted USD Index (DXY proxy)",
        freq=FREQ_DAILY, group=GROUP_USD, date=dt.date(2026, 6, 16),
        level=121.5, is_rate=False, stale=False, level_label="121.50",
        headline_label="+1.40%", headline_caption="~1m change", headline_arrow="↑",
        secondary=[("~3m change", "+2.10%")],
    )
    cpi = panel_a.MacroRow(
        series_id="CPIAUCSL", label="CPI (All Urban Consumers)", freq=FREQ_MONTHLY,
        group=GROUP_INFLATION, date=dt.date(2026, 5, 1), level=309.0, is_rate=False,
        stale=False, level_label="309.00", headline_label="+3.0%",
        headline_caption="YoY", headline_arrow="↑",
        secondary=[("MoM", "+0.2%"), ("index", "309.00")],
    )
    vix = panel_a.MacroRow(
        series_id="VIXCLS", label="CBOE Volatility Index (VIX)", freq=FREQ_DAILY,
        group=GROUP_RISK_REGIME, date=dt.date(2026, 6, 16), level=18.0, is_rate=False,
        stale=False, level_label="18.00", headline_label="-4.00",
        headline_caption="~1m change", headline_arrow="↓",
        secondary=[("~3m change", "+1.00")],
    )
    groups = [
        panel_a.MacroGroup(GROUP_USD, "US Dollar", "x", [usd]),
        panel_a.MacroGroup(GROUP_INFLATION, "Realized Inflation", "x", [cpi]),
        panel_a.MacroGroup(GROUP_RISK_REGIME, "Risk Regime", "x", [vix]),
    ]
    view = panel_a.PanelAView(groups=groups, last_session=dt.date(2026, 6, 16))
    monkeypatch.setattr(dashboard_main.panel_a, "build_view", lambda *a, **k: view)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/a")

    assert resp.status_code == 200
    body = resp.text
    assert "Panel A" in body
    assert "DXY proxy" in body  # DXY-proxy caveat present.
    assert "NOT the 6-currency ICE DXY" in body or "NOT" in body
    assert "Panel D" in body  # VIX footnote points to Panel D.
    assert "GVZ/OVX" in body
    assert "121.50" in body  # USD level rendered.
    # No option-action language leaked into the page body.
    lowered = body.lower()
    for word in ["sell candidate", "premium rich", "buy ", "short puts", "short calls"]:
        assert word not in lowered


def test_panel_a_route_empty_state(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    empty = panel_a.PanelAView(groups=[], last_session=dt.date(2026, 6, 16))
    monkeypatch.setattr(dashboard_main.panel_a, "build_view", lambda *a, **k: empty)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/a")

    assert resp.status_code == 200
    assert "No macro data yet" in resp.text


def test_panel_a_route_error_state_not_500(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    errored = panel_a.PanelAView(
        groups=[], last_session=dt.date(2026, 6, 16), error=True
    )
    monkeypatch.setattr(dashboard_main.panel_a, "build_view", lambda *a, **k: errored)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/a")

    assert resp.status_code == 200
    assert "currently unavailable" in resp.text
    assert "macro_metrics" not in resp.text  # no leaked internal table name.


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
        pytest.skip("No Postgres reachable for Panel A integration test")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    alembic_command.upgrade(cfg, "head")

    seeded = ["DGS10", "CPIAUCSL", "VIXCLS"]
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM macro_metrics WHERE series_id = ANY(:s)"),
            {"s": seeded},
        )
        # DGS10 daily: latest + a ~1m-prior row.
        conn.execute(
            text(
                "INSERT INTO macro_metrics (series_id, date, value, source) "
                "VALUES ('DGS10', :d1, 4.50, 'FRED'), ('DGS10', :d2, 4.30, 'FRED')"
            ),
            {"d1": dt.date(2026, 6, 16), "d2": dt.date(2026, 5, 15)},
        )
        # CPIAUCSL monthly: latest + 12-months-prior for the YoY headline.
        conn.execute(
            text(
                "INSERT INTO macro_metrics (series_id, date, value, source) "
                "VALUES ('CPIAUCSL', :d1, 309.0, 'FRED'), ('CPIAUCSL', :d2, 300.0, 'FRED')"
            ),
            {"d1": dt.date(2026, 5, 1), "d2": dt.date(2025, 5, 1)},
        )
        conn.execute(
            text(
                "INSERT INTO macro_metrics (series_id, date, value, source) "
                "VALUES ('VIXCLS', :d, 18.0, 'FRED')"
            ),
            {"d": dt.date(2026, 6, 16)},
        )
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM macro_metrics WHERE series_id = ANY(:s)"),
                {"s": seeded},
            )
        engine.dispose()


def test_build_view_reads_seeded_rows(seeded_engine):
    view = panel_a.build_view(seeded_engine, today=dt.date(2026, 6, 17))
    dgs = _find_row(view, "DGS10")
    assert dgs.level == pytest.approx(4.50)
    assert dgs.headline_label == "+0.20 pp"  # 4.50 - 4.30.
    cpi = _find_row(view, "CPIAUCSL")
    assert cpi.headline_caption == "YoY"
    assert cpi.headline_label == "+3.0%"  # (309-300)/300.
