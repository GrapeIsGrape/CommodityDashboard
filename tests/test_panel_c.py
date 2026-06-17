"""Tests for Panel C (Positioning & Flow) — dashboard/panels/panel_c.py + route.

The pure presentation/logic helpers (COT index + degenerate guard, 80/20
crowding classification + directional inference, ABS-from-50 NULLS-LAST sort,
the weekly Tue→Fri expected-report-date staleness with holiday grace, the
cold-start ``accruing M/156`` labelling, the curve NULL-vs-flat labelling, and
formatting) are network-free and unit-tested directly. The render path is
exercised with a fake engine so it needs no live DB. A separate live-Postgres-
or-skip integration test (mirroring tests/test_health.py) migrates to head,
seeds a few cot/curve_shape rows, and asserts the route renders them — skipped
when no Postgres is reachable. FastAPI/httpx/jinja2 are optional in the bare
test env, so the route tests importorskip them.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url
from dashboard.panels import panel_c
from dashboard.panels.panel_c import (
    COT_CROWDED_LONG_THRESHOLD,
    COT_CROWDED_SHORT_THRESHOLD,
    COT_INDEX_LOOKBACK_WEEKS,
    COT_MIN_HISTORY_WEEKS,
    CROWD_LONG,
    CROWD_NONE,
    CROWD_SHORT,
    CURVE_BACKWARDATION,
    CURVE_FLAT,
    CURVE_NONE,
    classify_crowding,
    cot_index,
    cot_index_display,
    cot_sort_key,
    crowding_inference,
    curve_structure_class,
    curve_structure_label,
    expected_cot_report_date,
    format_date,
    format_int,
    format_pct,
    format_price,
    is_cot_stale,
)

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- COT index math (incl. degenerate guard) ------------------------------

def test_cot_index_linear_scaling():
    # today=50 over [0..100] -> 50; today=100 -> 100; today=0 -> 0.
    hist = [0.0, 25.0, 50.0, 75.0, 100.0]
    assert cot_index(50.0, hist) == 50.0
    assert cot_index(100.0, hist) == 100.0
    assert cot_index(0.0, hist) == 0.0


def test_cot_index_degenerate_window_returns_none_not_nan():
    # max == min (flat history) must not divide by zero.
    assert cot_index(500.0, [500.0, 500.0, 500.0]) is None


def test_cot_index_empty_history_is_none():
    assert cot_index(10.0, []) is None


# --- Crowding classification (>=80 long / <=20 short / neutral / NULL) -----

def test_crowded_long_at_and_above_threshold():
    assert classify_crowding(float(COT_CROWDED_LONG_THRESHOLD)) == CROWD_LONG
    assert classify_crowding(95.0) == CROWD_LONG


def test_crowded_short_at_and_below_threshold():
    assert classify_crowding(float(COT_CROWDED_SHORT_THRESHOLD)) == CROWD_SHORT
    assert classify_crowding(5.0) == CROWD_SHORT


def test_neutral_middle_is_not_crowded():
    assert classify_crowding(50.0) == CROWD_NONE
    assert classify_crowding(79.9) == CROWD_NONE
    assert classify_crowding(20.1) == CROWD_NONE


def test_null_index_is_never_crowded():
    assert classify_crowding(None) == CROWD_NONE


def test_inference_carries_the_which_option_not_to_short_read():
    long_text = crowding_inference(CROWD_LONG)
    short_text = crowding_inference(CROWD_SHORT)
    assert "puts" in long_text and "calls" in long_text
    assert "DOWN" in long_text
    assert "calls" in short_text and "puts" in short_text
    assert "UP" in short_text
    assert crowding_inference(CROWD_NONE) == ""


# --- Cold-start accruing M/156 + degenerate label -------------------------

def test_accruing_label_below_history_threshold():
    label = cot_index_display(index_value=None, history_weeks=40)
    assert label == f"— (accruing 40/{COT_INDEX_LOOKBACK_WEEKS})"


def test_accruing_label_uses_lookback_constant_not_hardcoded():
    label = cot_index_display(index_value=None, history_weeks=0)
    assert label.endswith(f"/{COT_INDEX_LOOKBACK_WEEKS})")


def test_enough_history_but_degenerate_window_is_plain_dash():
    # >= min history but a None index (max==min) -> "—", not an accruing label.
    label = cot_index_display(index_value=None, history_weeks=COT_MIN_HISTORY_WEEKS + 10)
    assert label == "—"


def test_real_index_renders_with_thousands_no_decimals():
    assert cot_index_display(index_value=87.4, history_weeks=156) == "87"


# --- ABS-from-50 NULLS-LAST sort key --------------------------------------

def test_sort_key_extremes_outrank_middle():
    # 90 (dist 40) and 10 (dist 40) both beat 55 (dist 5).
    assert cot_sort_key(90.0) > cot_sort_key(55.0)
    assert cot_sort_key(10.0) > cot_sort_key(55.0)


def test_sort_key_nulls_last():
    # Any real index outranks NULL, regardless of how close to 50 it is.
    assert cot_sort_key(50.0) > cot_sort_key(None)
    assert cot_sort_key(51.0) > cot_sort_key(None)


# --- Weekly expected-report-date (Tue->Fri, clock-injectable) -------------

def test_expected_report_is_tuesday_before_release_friday():
    # Read on Fri 2026-06-12 (a release day): newest report is Tue 2026-06-09.
    assert expected_cot_report_date(dt.date(2026, 6, 12)) == dt.date(2026, 6, 9)


def test_expected_report_pre_friday_uses_prior_week():
    # Read on Wed 2026-06-17: most recent past Friday is 2026-06-12 -> Tue 06-09.
    assert expected_cot_report_date(dt.date(2026, 6, 17)) == dt.date(2026, 6, 9)


def test_expected_report_thursday_before_release_still_prior_week():
    # Thu 2026-06-18: this week's Friday (06-19) release has NOT happened yet, so
    # the newest expected report is still Tue 2026-06-09 (prior Friday 06-12).
    assert expected_cot_report_date(dt.date(2026, 6, 18)) == dt.date(2026, 6, 9)


def test_fresh_report_not_stale():
    # Stored report == expected -> not stale.
    assert is_cot_stale(dt.date(2026, 6, 9), dt.date(2026, 6, 17)) is False


def test_old_report_is_stale():
    # Two weeks behind, no holiday in the window -> stale.
    assert is_cot_stale(dt.date(2026, 5, 26), dt.date(2026, 6, 17)) is True


def test_null_report_is_not_stale():
    assert is_cot_stale(None, dt.date(2026, 6, 17)) is False


def test_holiday_grace_tolerates_one_missed_release():
    # Juneteenth 2026-06-19 (Fri) is a federal holiday in the expected report's
    # release window. Read on 2026-06-22 (Mon): expected Tue = 06-16, release
    # Fri = 06-19 (holiday). A report at the prior Tue 06-09 must NOT flag stale.
    today = dt.date(2026, 6, 22)
    assert expected_cot_report_date(today) == dt.date(2026, 6, 16)
    assert is_cot_stale(dt.date(2026, 6, 9), today) is False
    # But two releases behind (06-02) is stale even with the grace.
    assert is_cot_stale(dt.date(2026, 6, 2), today) is True


# --- Curve NULL-vs-flat labelling -----------------------------------------

def test_curve_null_distinct_from_flat():
    assert curve_structure_class(None) == CURVE_NONE
    assert curve_structure_class("flat") == CURVE_FLAT
    assert curve_structure_class(None) != curve_structure_class("flat")


def test_curve_backwardation_class_and_label():
    assert curve_structure_class("backwardation") == CURVE_BACKWARDATION
    assert curve_structure_label("backwardation") == "backwardation"


def test_curve_null_label_says_no_curve():
    assert curve_structure_label(None) == "— (no curve)"
    assert curve_structure_label("flat") == "flat"


# --- Formatting -----------------------------------------------------------

def test_format_int_thousands_and_signed():
    assert format_int(1234567) == "1,234,567"
    assert format_int(-45000) == "-45,000"
    assert format_int(None) == "—"


def test_format_pct():
    assert format_pct(0.123) == "12.3%"
    assert format_pct(None) == "—"


def test_format_price():
    assert format_price(1234.5) == "1,234.50"
    assert format_price(None) == "—"


def test_format_date():
    assert format_date(dt.date(2026, 6, 9)) == "2026-06-09"
    assert format_date(None) == "—"


# --- Render path: fake engine (no live DB) --------------------------------

class _FakeRow:
    def __init__(self, **kw):
        self._m = kw

    @property
    def symbol(self):
        return self._m["symbol"]

    @property
    def net_spec(self):
        return self._m["net_spec"]

    @property
    def _mapping(self):
        return self._m


class _FakeConn:
    def __init__(self, cot_rows, history_rows, curve_rows):
        self._cot = cot_rows
        self._history = history_rows
        self._curve = curve_rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "curve_shape" in sql:
            return list(self._curve)
        if "net_spec" in sql and "report_date >=" in sql:
            return list(self._history)
        return list(self._cot)


class _FakeEngine:
    def __init__(self, cot_rows, history_rows, curve_rows):
        self._cot = cot_rows
        self._history = history_rows
        self._curve = curve_rows

    def connect(self):
        return _FakeConn(self._cot, self._history, self._curve)


def _cot_latest(symbol, nc_long, nc_short, oi, report_date=dt.date(2026, 6, 9)):
    return _FakeRow(
        symbol=symbol,
        report_date=report_date,
        noncomm_long=nc_long,
        noncomm_short=nc_short,
        open_interest=oi,
    )


def _curve_latest(symbol, structure, slope=0.06):
    return _FakeRow(
        symbol=symbol,
        date=dt.date(2026, 6, 16),
        front_price=70.0,
        back_price=72.0,
        spread=2.0,
        slope_pct=slope,
        structure=structure,
    )


def _history_rows(symbol, values):
    return [_FakeRow(symbol=symbol, net_spec=v) for v in values]


def test_build_view_extreme_sorts_first_nulls_last():
    # GC: huge net history giving a high index; SI: accruing (short history);
    # CL: mid index. Order should be extremes first, accruing LAST.
    long_hist = list(range(0, 200, 1))  # >= COT_MIN_HISTORY_WEEKS distinct values.
    gc_hist = _history_rows("GC", long_hist)
    cl_hist = _history_rows("CL", long_hist)
    cot_rows = [
        _cot_latest("GC", nc_long=199, nc_short=0, oi=1000),  # net 199 -> index ~100.
        _cot_latest("CL", nc_long=100, nc_short=0, oi=1000),  # net 100 -> index ~50.
        _cot_latest("SI", nc_long=50, nc_short=0, oi=1000),   # short history -> accruing.
    ]
    history = gc_hist + cl_hist + _history_rows("SI", [1, 2, 3])
    view = panel_c.build_view(
        _FakeEngine(cot_rows, history, []), today=dt.date(2026, 6, 17)
    )
    ordered = [r.symbol for r in view.cot_rows]
    assert ordered[0] == "GC"  # most extreme.
    assert ordered[-1] == "SI"  # accruing (NULL index) sinks last.
    si = next(r for r in view.cot_rows if r.symbol == "SI")
    assert si.index_value is None
    assert si.index_label.startswith("— (accruing 3/")
    assert si.crowding == CROWD_NONE


def test_build_view_crowded_long_flagged():
    long_hist = list(range(0, 200))
    cot_rows = [_cot_latest("GC", nc_long=199, nc_short=0, oi=1000)]
    history = _history_rows("GC", long_hist)
    view = panel_c.build_view(
        _FakeEngine(cot_rows, history, []), today=dt.date(2026, 6, 17)
    )
    gc = view.cot_rows[0]
    assert gc.index_value >= COT_CROWDED_LONG_THRESHOLD
    assert gc.crowding == CROWD_LONG
    assert gc.net_spec == 199
    assert gc.net_spec_pct_oi == 199 / 1000


def test_build_view_curve_strip_separate_from_table():
    cot_rows = [_cot_latest("CL", nc_long=10, nc_short=5, oi=100)]
    curve = [_curve_latest("CL", "backwardation"), _curve_latest("NG", None)]
    view = panel_c.build_view(
        _FakeEngine(cot_rows, _history_rows("CL", [1, 2]), curve),
        today=dt.date(2026, 6, 17),
    )
    # Curve is a separate strip, NOT extra columns in the 28-row table.
    assert {c.symbol for c in view.curve_cards} == {"CL", "NG"}
    ng = next(c for c in view.curve_cards if c.symbol == "NG")
    assert ng.structure is None
    assert ng.structure_label == "— (no curve)"
    cl_card = next(c for c in view.curve_cards if c.symbol == "CL")
    assert cl_card.structure_class == CURVE_BACKWARDATION
    # The energy COT row echoes the structure inline.
    cl_row = next(r for r in view.cot_rows if r.symbol == "CL")
    assert cl_row.structure_echo == "backwardation"


def test_build_view_empty_when_no_rows():
    view = panel_c.build_view(_FakeEngine([], [], []), today=dt.date(2026, 6, 17))
    assert view.is_empty is True


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
    return ProgrammingError("SELECT ...", {}, Exception('relation "cot" does not exist'))


def test_build_view_db_unreachable_returns_error_state_not_raise():
    view = panel_c.build_view(
        _RaisingEngine(_operational_error()), today=dt.date(2026, 6, 17)
    )
    assert view.error is True
    assert view.cot_rows == [] and view.curve_cards == []
    assert view.expected_report_date == dt.date(2026, 6, 9)  # still computed.


def test_build_view_missing_table_returns_error_state_not_raise():
    view = panel_c.build_view(
        _RaisingEngine(_programming_error()), today=dt.date(2026, 6, 17)
    )
    assert view.error is True
    assert view.cot_rows == [] and view.curve_cards == []


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


def test_panel_c_route_renders_table_and_strip(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    crowded = panel_c.CotRow(
        symbol="GC", name="Gold (COMEX)", report_date=dt.date(2026, 6, 9),
        noncomm_long=250000, noncomm_short=30000, net_spec=220000,
        net_spec_pct_oi=0.42, open_interest=520000, index_value=92.0,
        index_label="92", crowding=panel_c.CROWD_LONG,
        inference=panel_c.crowding_inference(panel_c.CROWD_LONG),
        history_weeks=156, structure_echo=None, stale=False,
    )
    accruing = panel_c.CotRow(
        symbol="LBR", name="Lumber (CME)", report_date=dt.date(2026, 6, 9),
        noncomm_long=1200, noncomm_short=900, net_spec=300,
        net_spec_pct_oi=0.05, open_interest=6000, index_value=None,
        index_label="— (accruing 40/156)", crowding=panel_c.CROWD_NONE,
        inference="", history_weeks=40, structure_echo=None, stale=False,
    )
    cl_card = panel_c.CurveCard(
        symbol="CL", structure="backwardation",
        structure_class=panel_c.CURVE_BACKWARDATION, structure_label="backwardation",
        slope_pct=-0.08, front_price=70.0, back_price=66.0, spread=-4.0,
        date=dt.date(2026, 6, 16),
    )
    ng_card = panel_c.CurveCard(
        symbol="NG", structure=None, structure_class=panel_c.CURVE_NONE,
        structure_label="— (no curve)", slope_pct=None, front_price=None,
        back_price=None, spread=None, date=dt.date(2026, 6, 16),
    )
    view = panel_c.PanelCView(
        cot_rows=[crowded, accruing], curve_cards=[cl_card, ng_card],
        expected_report_date=dt.date(2026, 6, 9),
    )
    monkeypatch.setattr(dashboard_main.panel_c, "build_view", lambda *a, **k: view)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/c")

    assert resp.status_code == 200
    body = resp.text
    assert "Panel C" in body
    assert "crowded-long" in body  # loud crowding class rendered.
    assert "220,000" in body  # thousands separators on net_spec.
    assert "— (accruing 40/156)" in body  # accruing label rendered.
    assert "— (no curve)" in body  # NULL curve distinct from flat.
    assert "all large specs" in body  # Legacy footnote present.
    assert "lean calls" in body or "don't be short puts" in body  # directional inference.


def test_panel_c_route_empty_state(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    empty = panel_c.PanelCView(
        cot_rows=[], curve_cards=[], expected_report_date=dt.date(2026, 6, 9)
    )
    monkeypatch.setattr(dashboard_main.panel_c, "build_view", lambda *a, **k: empty)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/c")

    assert resp.status_code == 200
    assert "No positioning data yet" in resp.text


def test_panel_c_route_error_state_not_500(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    errored = panel_c.PanelCView(
        cot_rows=[], curve_cards=[], expected_report_date=dt.date(2026, 6, 9), error=True
    )
    monkeypatch.setattr(dashboard_main.panel_c, "build_view", lambda *a, **k: errored)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/c")

    assert resp.status_code == 200
    assert "currently unavailable" in resp.text
    assert "No positioning data yet" not in resp.text
    assert "curve_shape" not in resp.text  # no leaked internal table name.


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
        pytest.skip("No Postgres reachable for Panel C integration test")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    alembic_command.upgrade(cfg, "head")

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cot WHERE symbol IN ('GC')"))
        conn.execute(text("DELETE FROM curve_shape WHERE symbol IN ('CL')"))
        # Seed >= COT_MIN_HISTORY_WEEKS weeks of GC net-spec history so the index
        # computes (varied values so the window isn't degenerate).
        base = dt.date(2026, 6, 9)
        for i in range(COT_MIN_HISTORY_WEEKS + 5):
            d = base - dt.timedelta(weeks=i)
            conn.execute(
                text(
                    "INSERT INTO cot "
                    "(symbol, report_date, noncomm_long, noncomm_short, comm_long, "
                    "comm_short, open_interest, source) "
                    "VALUES (:s,:d,:nl,:ns,0,0,:oi,'CFTC')"
                ),
                {"s": "GC", "d": d, "nl": 200000 + i * 100, "ns": 30000, "oi": 500000},
            )
        conn.execute(
            text(
                "INSERT INTO curve_shape "
                "(symbol, date, front_price, back_price, spread, slope_pct, structure, source) "
                "VALUES (:s,:d,:f,:b,:sp,:sl,'backwardation','yfinance')"
            ),
            {"s": "CL", "d": base, "f": 70.0, "b": 66.0, "sp": -4.0, "sl": -0.08},
        )
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM cot WHERE symbol IN ('GC')"))
            conn.execute(text("DELETE FROM curve_shape WHERE symbol IN ('CL')"))
        engine.dispose()


def test_build_view_reads_seeded_rows(seeded_engine):
    view = panel_c.build_view(seeded_engine, today=dt.date(2026, 6, 17))
    gc = next((r for r in view.cot_rows if r.symbol == "GC"), None)
    assert gc is not None
    assert gc.history_weeks >= COT_MIN_HISTORY_WEEKS
    assert gc.index_value is not None  # enough varied history -> a real index.
    cl = next((c for c in view.curve_cards if c.symbol == "CL"), None)
    assert cl is not None and cl.structure_class == CURVE_BACKWARDATION
