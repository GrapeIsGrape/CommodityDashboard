"""Tests for the macro-context sub-panel — dashboard/panels/panel_macro.py + route.

Pure presentation/logic helpers (total-return ~1m/~3m change off adj_close via
nearest_prior + pct_change; trailing-high drawdown incl. the thin-window "actual
window used" path; the neutral risk-regime classification incl. deadband gating;
NYSE daily staleness; formatting; honest NULL / no-prior) are network-free and
unit-tested directly. The render path uses a fake engine (no live DB). A separate
live-Postgres-or-skip integration test migrates to head, seeds a few prices rows
and asserts build_view. FastAPI/httpx/jinja2 are optional in the bare test env,
so the route tests importorskip them.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url
from dashboard.panels import panel_macro
from dashboard.panels.panel_macro import (
    REGIME_DERISK,
    REGIME_FLAT,
    REGIME_RISK_OFF,
    REGIME_RISK_ON,
    REGIME_UNKNOWN,
    classify_regime,
    format_drawdown,
    format_usd,
    last_expected_session,
    trailing_drawdown,
)

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- Regime classification (deadband-gated, four cases) -------------------

def test_regime_risk_on_equity_up_bonds_down():
    assert classify_regime(0.02, -0.01) == REGIME_RISK_ON


def test_regime_risk_off_equity_down_bonds_up():
    assert classify_regime(-0.02, 0.01) == REGIME_RISK_OFF


def test_regime_correlated_derisking_both_down():
    assert classify_regime(-0.02, -0.01) == REGIME_DERISK


def test_regime_flat_when_both_flat():
    assert classify_regime(0.0001, -0.0001) == REGIME_FLAT


def test_regime_subrounding_drift_is_flat_not_risk_on():
    # +0.0004 equity / -0.0003 bonds: both inside the deadband → "~flat / mixed",
    # never a confident risk-on (Panel A deadband UAT lesson).
    assert classify_regime(0.0004, -0.0003) == REGIME_FLAT


def test_regime_equity_down_bonds_flat_with_drawdown_is_derisking():
    # Equity soft, bonds flat, but equity in a material drawdown → de-risking.
    assert classify_regime(-0.03, 0.0001, equity_in_drawdown=True) == REGIME_DERISK


def test_regime_equity_down_bonds_flat_no_drawdown_is_flat():
    assert classify_regime(-0.03, 0.0001, equity_in_drawdown=False) == REGIME_FLAT


def test_regime_unknown_when_both_legs_missing():
    assert classify_regime(None, None) == REGIME_UNKNOWN


def test_regime_single_leg_missing_is_not_unknown():
    # One leg present is enough to avoid the "no data" label; with the bond leg
    # missing (sign 0) an equity-up tape reads neutral "~flat / mixed" rather
    # than a confident risk-on (we don't infer the missing leg).
    assert classify_regime(0.02, None) == REGIME_FLAT
    assert classify_regime(0.02, None) != REGIME_UNKNOWN


# --- Trailing-high drawdown (pure) ----------------------------------------

def test_trailing_drawdown_off_high():
    hist = [
        (dt.date(2026, 6, 16), 90.0),
        (dt.date(2026, 3, 16), 100.0),  # the high.
        (dt.date(2025, 6, 20), 80.0),
    ]
    dd = trailing_drawdown(hist, 90.0)
    assert dd.pct == pytest.approx(-0.10)
    assert dd.obs == 3


def test_trailing_drawdown_at_new_high_is_zero_not_positive():
    hist = [(dt.date(2026, 6, 16), 110.0), (dt.date(2025, 7, 1), 100.0)]
    dd = trailing_drawdown(hist, 110.0)
    assert dd.pct == 0.0


def test_trailing_drawdown_skips_null_bars():
    hist = [
        (dt.date(2026, 6, 16), 95.0),
        (dt.date(2026, 5, 16), None),
        (dt.date(2026, 1, 16), 100.0),
    ]
    dd = trailing_drawdown(hist, 95.0)
    assert dd.pct == pytest.approx(-0.05)
    assert dd.obs == 2


def test_trailing_drawdown_unknown_on_null_latest_or_empty():
    assert trailing_drawdown([], 100.0).pct is None
    assert trailing_drawdown([(dt.date(2026, 6, 16), 100.0)], None).pct is None


def test_trailing_drawdown_reports_actual_window():
    hist = [(dt.date(2026, 6, 16), 95.0), (dt.date(2026, 1, 26), 100.0)]
    dd = trailing_drawdown(hist, 95.0)
    # ~141 days, not a full year — the actual span must be surfaced.
    assert dd.window_days == (dt.date(2026, 6, 16) - dt.date(2026, 1, 26)).days


def test_format_drawdown_thin_window_shows_actual_days():
    hist = [(dt.date(2026, 6, 16), 95.0), (dt.date(2026, 1, 26), 100.0)]
    dd = trailing_drawdown(hist, 95.0)
    label = format_drawdown(dd)
    assert "-5.0%" in label
    assert "day high" in label  # actual window, not "~1y".
    assert "~1y" not in label


def test_format_drawdown_full_window_labels_one_year():
    hist = [(dt.date(2026, 6, 16), 92.0), (dt.date(2025, 6, 16), 100.0)]
    dd = trailing_drawdown(hist, 92.0)
    label = format_drawdown(dd)
    assert "off ~1y high" in label


def test_format_drawdown_unknown_is_dash():
    assert format_drawdown(trailing_drawdown([], 100.0)) == "—"


# --- Formatting + honest NULL ---------------------------------------------

def test_format_usd_thousands_and_null():
    assert format_usd(1234.5) == "$1,234.50"
    assert format_usd(0.0) == "$0.00"  # real zero distinct from NULL.
    assert format_usd(None) == "—"


# --- NYSE daily staleness (reuse Panel A) ---------------------------------

def test_staleness_fresh_bar_not_stale():
    # Tue 2026-06-16; prior session Mon 06-15. A bar from 06-15 is fresh.
    assert panel_macro._is_stale(dt.date(2026, 6, 15), dt.date(2026, 6, 16)) is False


def test_staleness_old_bar_is_stale():
    assert panel_macro._is_stale(dt.date(2026, 6, 10), dt.date(2026, 6, 16)) is True


def test_staleness_friday_bar_read_monday_not_stale():
    # Mon 2026-06-15; prior trading session is Fri 06-12 (weekend skipped).
    assert panel_macro._is_stale(dt.date(2026, 6, 12), dt.date(2026, 6, 15)) is False


def test_staleness_null_date_never_stale():
    assert panel_macro._is_stale(None, dt.date(2026, 6, 16)) is False


def test_last_session_skips_weekend_and_holiday():
    assert last_expected_session(dt.date(2026, 6, 15)) == dt.date(2026, 6, 12)
    # 2026-07-03 observed Independence Day; prior session before Mon 07-06 is 07-02.
    assert last_expected_session(dt.date(2026, 7, 6)) == dt.date(2026, 7, 2)


# --- Render path: fake engine (no live DB) --------------------------------

class _FakeRow:
    def __init__(self, **kw):
        self._m = kw

    def __getattr__(self, name):
        try:
            return self._m[name]
        except KeyError:
            raise AttributeError(name)

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


def _latest(symbol, date, close, adj_close):
    return _FakeRow(symbol=symbol, date=date, close=close, adj_close=adj_close)


def _hist(symbol, date, adj_close):
    return _FakeRow(symbol=symbol, date=date, adj_close=adj_close)


def _find_row(view, symbol):
    for row in view.rows:
        if row.symbol == symbol:
            return row
    raise AssertionError(f"symbol {symbol} not in view")


def test_build_view_three_config_rows_in_order():
    view = panel_macro.build_view(_FakeEngine([], []), today=dt.date(2026, 6, 17))
    symbols = [r.symbol for r in view.rows]
    assert symbols == ["TLT", "VTI", "QQQ"]  # config order.


def test_build_view_total_return_headline_off_adj_close():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [_latest("VTI", latest_date, 250.0, 280.0)]  # close != adj_close.
    history = [
        _hist("VTI", latest_date, 280.0),
        _hist("VTI", latest_date - dt.timedelta(days=30), 270.0),  # ~1m prior.
        _hist("VTI", latest_date - dt.timedelta(days=90), 260.0),  # ~3m prior.
    ]
    view = panel_macro.build_view(_FakeEngine(latest, history), today=today)
    vti = _find_row(view, "VTI")
    # (280-270)/270 = +3.7%; computed off adj_close, NOT the 250 raw close.
    assert vti.one_month_label == "+3.7%"
    assert vti.one_month_arrow == "↑"
    assert vti.three_month_label == "+7.7%"  # (280-260)/260.
    # Raw close shown only as the secondary tape level (USD), never mixed in.
    assert vti.close_label == "$250.00"


def test_build_view_null_adj_close_renders_dash_and_no_prior():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [_latest("TLT", latest_date, 92.0, None)]
    history = [_hist("TLT", latest_date, None)]
    view = panel_macro.build_view(_FakeEngine(latest, history), today=today)
    tlt = _find_row(view, "TLT")
    assert tlt.one_month_label == "— (no prior)"
    assert tlt.close_label == "$92.00"  # raw close still shown.
    assert tlt.drawdown_label == "—"  # TLT has no drawdown gauge anyway.


def test_build_view_missing_prior_degrades_to_no_prior():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [_latest("QQQ", latest_date, 500.0, 510.0)]
    history = [_hist("QQQ", latest_date, 510.0)]  # no older row.
    view = panel_macro.build_view(_FakeEngine(latest, history), today=today)
    qqq = _find_row(view, "QQQ")
    assert "no prior" in qqq.one_month_label


def test_build_view_equity_has_drawdown_bond_does_not():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [
        _latest("VTI", latest_date, 250.0, 250.0),
        _latest("TLT", latest_date, 90.0, 95.0),
    ]
    history = [
        _hist("VTI", latest_date, 250.0),
        _hist("VTI", latest_date - dt.timedelta(days=120), 300.0),  # prior high.
        _hist("TLT", latest_date, 95.0),
        _hist("TLT", latest_date - dt.timedelta(days=120), 99.0),
    ]
    view = panel_macro.build_view(_FakeEngine(latest, history), today=today)
    vti = _find_row(view, "VTI")
    tlt = _find_row(view, "TLT")
    assert "-16.7%" in vti.drawdown_label  # (250-300)/300.
    assert vti.in_drawdown is True
    assert tlt.drawdown_label == "—"  # long bond gets no equity drawdown gauge.


def test_build_view_composes_regime_from_vti_vs_tlt():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [
        _latest("VTI", latest_date, 280.0, 280.0),
        _latest("TLT", latest_date, 90.0, 90.0),
    ]
    history = [
        _hist("VTI", latest_date, 280.0),
        _hist("VTI", latest_date - dt.timedelta(days=30), 270.0),  # equity up.
        _hist("TLT", latest_date, 90.0),
        _hist("TLT", latest_date - dt.timedelta(days=30), 92.0),  # bonds down.
    ]
    view = panel_macro.build_view(_FakeEngine(latest, history), today=today)
    assert view.regime == REGIME_RISK_ON


def test_build_view_carries_no_rank_or_percentile_fields():
    view = panel_macro.build_view(_FakeEngine([], []), today=dt.date(2026, 6, 17))
    for row in view.rows:
        assert not hasattr(row, "iv_rank")
        assert not hasattr(row, "iv_percentile")


def test_no_option_action_language_in_view_model():
    today = dt.date(2026, 6, 17)
    latest_date = dt.date(2026, 6, 16)
    latest = [_latest("VTI", latest_date, 250.0, 250.0)]
    history = [
        _hist("VTI", latest_date, 250.0),
        _hist("VTI", latest_date - dt.timedelta(days=30), 260.0),
    ]
    view = panel_macro.build_view(_FakeEngine(latest, history), today=today)
    banned = ["sell", "buy", "rich", "candidate", "premium", "short", "write", "iv rank", "percentile"]
    blob = view.regime.lower()
    for row in view.rows:
        blob += " ".join(
            [row.name, row.note, row.one_month_label, row.three_month_label, row.drawdown_label, row.close_label]
        ).lower()
    for word in banned:
        assert word not in blob


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


def _operational_error():
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


def _programming_error():
    return ProgrammingError("SELECT ...", {}, Exception('relation "prices" does not exist'))


def test_build_view_db_unreachable_returns_error_state():
    view = panel_macro.build_view(_RaisingEngine(_operational_error()), today=dt.date(2026, 6, 17))
    assert view.error is True
    assert view.rows == []


def test_build_view_missing_table_returns_error_state():
    view = panel_macro.build_view(_RaisingEngine(_programming_error()), today=dt.date(2026, 6, 17))
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


def test_panel_macro_route_renders_strip_and_subordination(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    rows = [
        panel_macro.MacroContextRow(
            symbol="TLT", name="20+ Year Treasury Bond ETF",
            note="duration proxy; see Panel A for the rate itself",
            date=dt.date(2026, 6, 16), close=92.0, adj_close=95.0, stale=False,
            close_label="$92.00", one_month_label="+1.2%", one_month_arrow="↑",
            three_month_label="-0.5%", three_month_arrow="↓", drawdown_label="—",
        ),
        panel_macro.MacroContextRow(
            symbol="VTI", name="Total US Stock Market ETF", note="broad US equity",
            date=dt.date(2026, 6, 16), close=250.0, adj_close=280.0, stale=False,
            close_label="$250.00", one_month_label="+3.7%", one_month_arrow="↑",
            three_month_label="+7.7%", three_month_arrow="↑",
            drawdown_label="-2.1% (off ~1y high)",
        ),
        panel_macro.MacroContextRow(
            symbol="QQQ", name="Nasdaq-100 ETF",
            note="higher-beta / tech (QQQ ⊂ VTI) — the QQQ-vs-VTI gap is not a signal",
            date=dt.date(2026, 6, 16), close=500.0, adj_close=510.0, stale=False,
            close_label="$500.00", one_month_label="+4.0%", one_month_arrow="↑",
            three_month_label="+9.0%", three_month_arrow="↑",
            drawdown_label="-1.0% (off ~1y high)",
        ),
    ]
    view = panel_macro.PanelMacroView(
        rows=rows, last_session=dt.date(2026, 6, 16), regime=REGIME_RISK_ON
    )
    monkeypatch.setattr(dashboard_main.panel_macro, "build_view", lambda *a, **k: view)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/macro")

    assert resp.status_code == 200
    body = resp.text
    assert "TLT" in body and "VTI" in body and "QQQ" in body
    assert "context, not commodities" in body
    assert "Panel A" in body and "Panel D" in body
    assert "total-return adjusted" in body
    assert "$250.00" in body  # raw tape level.
    assert "+3.7%" in body  # total-return headline.
    assert REGIME_RISK_ON in body
    assert "duration proxy" in body
    assert "QQQ-vs-VTI gap is not a signal" in body
    # Banned-phrase assertion over the FULL rendered output (#1 invariant).
    lowered = body.lower()
    # Note: bare "sell"/"short" appear in base.html's shared CSS palette
    # (--sell / --short-* color vars), so the banned-phrase guard targets
    # human-readable option-action phrases, not CSS identifiers.
    for word in ["iv rank", "percentile", "premium rich", "sell candidate",
                 "short put", "short call", " write ", "sell the", "go short",
                 "write the"]:
        assert word not in lowered


def test_panel_macro_route_empty_state(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    empty = panel_macro.PanelMacroView(rows=[], last_session=dt.date(2026, 6, 16))
    monkeypatch.setattr(dashboard_main.panel_macro, "build_view", lambda *a, **k: empty)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/macro")

    assert resp.status_code == 200
    assert "No macro-context data yet" in resp.text


def test_panel_macro_route_error_state_not_500(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    errored = panel_macro.PanelMacroView(rows=[], last_session=dt.date(2026, 6, 16), error=True)
    monkeypatch.setattr(dashboard_main.panel_macro, "build_view", lambda *a, **k: errored)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/macro")

    assert resp.status_code == 200
    assert "currently unavailable" in resp.text
    assert "prices" not in resp.text.lower() or "prices ETL" in resp.text  # no leaked raw table name in error.


# --- Static guard: no dashboard -> etl import (the #17 pattern) ------------

def test_panel_macro_does_not_import_etl():
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "dashboard" / "panels" / "panel_macro.py").read_text(encoding="utf-8")
    assert re.search(r"^\s*(from\s+etl[\s.]|import\s+etl[\s.]?)", src, re.MULTILINE) is None


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
        pytest.skip("No Postgres reachable for macro-context integration test")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    alembic_command.upgrade(cfg, "head")

    seeded = ["TLT", "VTI", "QQQ"]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM prices WHERE symbol = ANY(:s)"), {"s": seeded})
        conn.execute(
            text(
                "INSERT INTO prices (symbol, date, close, adj_close, source) VALUES "
                "('VTI', :d1, 250.0, 280.0, 'yfinance'), "
                "('VTI', :d2, 245.0, 270.0, 'yfinance'), "
                "('TLT', :d1, 92.0, 95.0, 'yfinance'), "
                "('TLT', :d2, 93.0, 96.0, 'yfinance')"
            ),
            {"d1": dt.date(2026, 6, 16), "d2": dt.date(2026, 5, 15)},
        )
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM prices WHERE symbol = ANY(:s)"), {"s": seeded})
        engine.dispose()


def test_build_view_reads_seeded_rows(seeded_engine):
    view = panel_macro.build_view(seeded_engine, today=dt.date(2026, 6, 17))
    vti = _find_row(view, "VTI")
    assert vti.close == pytest.approx(250.0)
    assert vti.adj_close == pytest.approx(280.0)
    # ~1m total-return off adj_close: (280-270)/270 = +3.7%.
    assert vti.one_month_label == "+3.7%"
