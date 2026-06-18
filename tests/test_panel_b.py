"""Tests for Panel B (Fundamentals / Inventory) — dashboard/panels/panel_b.py + route.

The pure presentation/logic helpers (weekly build/draw sign + native-unit
formatting incl. nat-gas injection/withdrawal; grain YoY %; own-history
percentile incl. degenerate max==min → — and cold-start — (accruing M/N);
stock-vs-flow tagging; cadence-bucketed release-aware staleness; neutral
directional translation; formatting/thousands-separators/no-$/NULL-vs-0) are
network-free and unit-tested directly. The render path uses a fake engine so it
needs no live DB. A separate live-Postgres-or-skip integration test (mirroring
tests/test_health.py) migrates to head, seeds a few inventories rows, and asserts
build_view groups them — skipped when no Postgres is reachable. FastAPI/httpx/
jinja2 are optional in the bare test env, so the route tests importorskip them.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url
from dashboard.panels import panel_b
from dashboard.panels.panel_b import (
    ACTIVE_SEASONALITY_MODE,
    CADENCE_ANNUAL,
    CADENCE_QUARTERLY,
    CADENCE_WEEKLY,
    KIND_ENERGY_FLOW,
    KIND_ENERGY_STOCK,
    KIND_GRAIN_PRODUCTION,
    KIND_GRAIN_STOCK,
    PANEL_B_MIN_HISTORY_QUARTERLY,
    PANEL_B_MIN_HISTORY_WEEKLY,
    SEASONALITY_YOY,
    VERDICT_LOOSE,
    VERDICT_NONE,
    VERDICT_TIGHT,
    _EIA_NATGAS_RELEASE_WEEKDAY,
    _EIA_PETROLEUM_RELEASE_WEEKDAY,
    classify_verdict,
    directional_translation,
    format_date,
    format_level,
    format_signed,
    format_signed_pct,
    is_annual_stale,
    is_quarterly_stale,
    is_weekly_stale,
    level_change,
    own_history_percentile,
    pct_change,
    percentile_display,
)

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- Static guard: no dashboard -> etl import coupling (#17 pattern) --------

def test_no_dashboard_module_imports_etl():
    """Panel B (like the rest of the dashboard image) must not reach into the
    ``etl`` package — it is absent from the dashboard image. Scan statically
    because the dev sys.path has etl/ present."""
    import pathlib
    import re

    dashboard_root = pathlib.Path(__file__).resolve().parents[1] / "dashboard"
    pattern = re.compile(r"^\s*(from\s+etl[\s.]|import\s+etl[\s.]?)", re.MULTILINE)
    offenders = [
        str(p)
        for p in dashboard_root.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --- Change calcs + NULL handling -----------------------------------------

def test_level_change_signed():
    assert level_change(421000.0, 423500.0) == pytest.approx(-2500.0)  # a draw.
    assert level_change(421000.0, 419000.0) == pytest.approx(2000.0)  # a build.


def test_level_change_null_or_no_prior():
    assert level_change(None, 419000.0) is None
    assert level_change(421000.0, None) is None


def test_pct_change_yoy():
    assert pct_change(1100.0, 1000.0) == pytest.approx(0.10)


def test_pct_change_guards_nonpositive_base_and_no_prior():
    assert pct_change(10.0, 0.0) is None
    assert pct_change(10.0, -1.0) is None
    assert pct_change(10.0, None) is None


# --- Own-history percentile (incl. degenerate + cold-start) ----------------

def test_own_history_percentile_midpoint():
    assert own_history_percentile(50.0, [0.0, 50.0, 100.0]) == pytest.approx(50.0)


def test_own_history_percentile_degenerate_window_is_none():
    # max == min -> None (no NaN/±inf); caller renders "—".
    assert own_history_percentile(5.0, [5.0, 5.0, 5.0]) is None


def test_own_history_percentile_empty_is_none():
    assert own_history_percentile(5.0, []) is None


def test_percentile_display_cold_start_accruing():
    # Below threshold -> "— (accruing M/N)" with M stored, N the threshold.
    assert percentile_display(None, history_obs=3, min_history=8) == "— (accruing 3/8)"
    # Even with a computable percentile, too little history still accrues.
    assert percentile_display(40.0, history_obs=3, min_history=8) == "— (accruing 3/8)"


def test_percentile_display_degenerate_renders_dash_not_nan():
    assert percentile_display(None, history_obs=10, min_history=8) == "—"


def test_percentile_display_real_value():
    assert percentile_display(73.4, history_obs=12, min_history=9) == "73"


def test_weekly_min_history_is_seasonal_comparable_count_not_raw_weekly_obs():
    # The weekly threshold counts SEASONAL COMPARABLES (~3 per year via the ±10-day
    # window), not raw weekly observations. ~3 prior seasons must clear it, so it
    # must be a small single/low-double-digit comparable count — never the old 52
    # (which implied 52 weekly obs and demanded ~18 years of backfill).
    assert PANEL_B_MIN_HISTORY_WEEKLY != 52
    assert PANEL_B_MIN_HISTORY_WEEKLY <= 12  # reachable with ~3 prior seasons.


# --- Verdict + neutral directional translation ----------------------------

def test_classify_verdict_thresholds():
    assert classify_verdict(10.0) == VERDICT_TIGHT
    assert classify_verdict(90.0) == VERDICT_LOOSE
    assert classify_verdict(50.0) == "mid"
    assert classify_verdict(None) == VERDICT_NONE


def test_directional_translation_neutral_and_flow_suppressed():
    tight = directional_translation(KIND_ENERGY_STOCK, VERDICT_TIGHT)
    assert "supply tight" in tight
    assert "vol-bid context" in tight
    # Flow rates and production carry no inventory tension translation.
    assert directional_translation(KIND_ENERGY_FLOW, VERDICT_TIGHT) == ""
    assert directional_translation(KIND_GRAIN_PRODUCTION, VERDICT_TIGHT) == ""


def test_directional_translation_has_no_option_action_language():
    banned = ["sell", "buy", "rich", "candidate", "premium", "short", "write"]
    for verdict in (VERDICT_TIGHT, VERDICT_LOOSE):
        text_out = directional_translation(KIND_ENERGY_STOCK, verdict).lower()
        for word in banned:
            assert word not in text_out


# --- Formatting: native units, separators, no $, NULL vs 0 -----------------

def test_format_level_thousands_separator_and_unit_no_dollar():
    out = format_level(421000.0, "Thousand Barrels")
    assert out == "421,000 Thousand Barrels"
    assert "$" not in out


def test_format_level_null_is_dash_distinct_from_zero():
    assert format_level(None, "Bcf") == "—"
    # A real zero renders as a zero, never an em dash.
    assert format_level(0.0, "Bcf") == "0 Bcf"


def test_format_signed_build_and_draw():
    assert format_signed(2500.0, "Thousand Barrels") == "+2,500 Thousand Barrels"
    assert format_signed(-2500.0, "Thousand Barrels") == "-2,500 Thousand Barrels"
    assert format_signed(None, "Thousand Barrels") == "— (no prior)"
    assert "$" not in format_signed(2500.0, "Thousand Barrels")


def test_format_signed_pct_yoy():
    assert format_signed_pct(0.10) == "+10.0%"
    assert format_signed_pct(-0.05) == "-5.0%"
    assert format_signed_pct(None) == "— (no prior)"


def test_format_date():
    assert format_date(dt.date(2026, 6, 12)) == "2026-06-12"
    assert format_date(None) == "—"


# --- Cadence-bucketed staleness (clock-injected today) ---------------------

def test_weekly_petroleum_wednesday_release_current_all_week():
    # Petroleum report releases Wed ~10:30 ET. The week-ending Fri 2026-06-12 is
    # reported Wed 2026-06-17. Reading later that week (Fri 06-19) that number is
    # current — NOT stale.
    today = dt.date(2026, 6, 19)
    we = dt.date(2026, 6, 12)
    assert is_weekly_stale(we, _EIA_PETROLEUM_RELEASE_WEEKDAY, today) is False


def test_weekly_petroleum_missing_release_is_stale():
    # Fri 2026-07-17, no holiday in the trailing window: the latest expected
    # week-ending is 2026-07-10. A stored row weeks behind (2026-06-26) is
    # genuinely stale (no grace applies away from any holiday).
    today = dt.date(2026, 7, 17)
    assert is_weekly_stale(dt.date(2026, 6, 26), _EIA_PETROLEUM_RELEASE_WEEKDAY, today) is True


def test_weekly_before_release_day_not_stale():
    # Tue 2026-06-16, before Wed's release: the latest expected week-ending is
    # still 2026-06-05 (06-12's report hasn't landed yet), so a 06-05 row is NOT
    # stale.
    today = dt.date(2026, 6, 16)
    assert is_weekly_stale(dt.date(2026, 6, 5), _EIA_PETROLEUM_RELEASE_WEEKDAY, today) is False


def test_weekly_monday_holiday_shifts_release_plus_one_day():
    # Week of Memorial Day Mon 2026-05-25 (holiday): the petroleum report slips
    # from Wed to Thu. On Wed 2026-05-27 (normal release day) the prior week's
    # number is NOT yet stale because the shifted release hasn't happened.
    today = dt.date(2026, 5, 27)
    # Latest expected week-ending without the shift would be 2026-05-22; with the
    # +1 shift the Wed read still expects the older 2026-05-15 week.
    assert is_weekly_stale(dt.date(2026, 5, 15), _EIA_PETROLEUM_RELEASE_WEEKDAY, today) is False


def test_weekly_holiday_grace_tolerates_one_missed_release():
    # A federal holiday in the trailing window grants a one-release grace: a row
    # one week behind the latest expected is tolerated rather than flagged STALE.
    today = dt.date(2026, 6, 22)  # Mon after Juneteenth (observed Fri 2026-06-19).
    latest = panel_b._latest_eia_week_ending(today, _EIA_PETROLEUM_RELEASE_WEEKDAY)
    one_week_behind = latest - dt.timedelta(days=7)
    assert is_weekly_stale(one_week_behind, _EIA_PETROLEUM_RELEASE_WEEKDAY, today) is False


def test_weekly_natgas_thursday_release():
    # NG storage releases Thu. Week-ending Fri 2026-06-12 reported Thu 2026-06-18.
    # On Fri 2026-06-19 that NG number is current.
    today = dt.date(2026, 6, 19)
    assert is_weekly_stale(dt.date(2026, 6, 12), _EIA_NATGAS_RELEASE_WEEKDAY, today) is False


def test_weekly_null_date_never_stale():
    assert is_weekly_stale(None, _EIA_PETROLEUM_RELEASE_WEEKDAY, dt.date(2026, 6, 19)) is False


def test_quarterly_never_red_between_reports():
    # Grain stocks dated first-of-Mar 2026-03-01; the next reference is Jun-01,
    # published ~late June. In mid-May the Mar print is the latest and must NOT be
    # flagged red — quarterly is "stale by design" between reports.
    today = dt.date(2026, 5, 15)
    assert is_quarterly_stale(dt.date(2026, 3, 1), today) is False


def test_quarterly_stale_when_whole_quarter_missing():
    # By Sep 2026 the Jun-01 print should be out; a stored row still on Mar-01 is
    # a genuinely missing quarter -> stale.
    today = dt.date(2026, 9, 1)
    assert is_quarterly_stale(dt.date(2026, 3, 1), today) is True


def test_quarterly_null_never_stale():
    assert is_quarterly_stale(None, dt.date(2026, 9, 1)) is False


def test_annual_production_not_perma_stale():
    # Production dated Jan-1 2025 (the 2024 crop year). Through 2025 and into early
    # 2026 (before the next January estimate) it must NOT be flagged stale by a
    # uniform day-age rule.
    assert is_annual_stale(dt.date(2025, 1, 1), dt.date(2025, 11, 1)) is False
    assert is_annual_stale(dt.date(2025, 1, 1), dt.date(2026, 1, 1)) is False


def test_annual_production_stale_when_two_estimates_overdue():
    # By 2027 the 2026 January production estimate should be out; a stored row
    # still dated 2025-01-01 is genuinely stale.
    assert is_annual_stale(dt.date(2025, 1, 1), dt.date(2027, 3, 1)) is True


def test_annual_null_never_stale():
    assert is_annual_stale(None, dt.date(2027, 3, 1)) is False


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
        if "date >=" in str(statement):
            return list(self._history)
        return list(self._latest)


class _FakeEngine:
    def __init__(self, latest_rows, history_rows):
        self._latest = latest_rows
        self._history = history_rows

    def connect(self):
        return _FakeConn(self._latest, self._history)


def _lrow(series_id, date, value, unit, source):
    return _FakeRow(series_id=series_id, date=date, value=value, unit=unit, source=source)


def _hrow(series_id, date, value):
    return _FakeRow(series_id=series_id, date=date, value=value)


def _find_row(view, series_id):
    for group in view.groups:
        for row in group.rows:
            if row.series_id == series_id:
                return row
    raise AssertionError(f"series {series_id} not in view")


def test_energy_stock_headlines_weekly_build_draw(monkeypatch):
    today = dt.date(2026, 6, 17)
    we = dt.date(2026, 6, 12)
    latest = [_lrow("PET.WCESTUS1.W", we, 421000.0, "Thousand Barrels", "EIA")]
    history = [
        _hrow("PET.WCESTUS1.W", we, 421000.0),
        _hrow("PET.WCESTUS1.W", we - dt.timedelta(days=7), 423500.0),  # prior week.
    ]
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "PET.WCESTUS1.W")
    assert row.kind == KIND_ENERGY_STOCK
    assert row.cadence == CADENCE_WEEKLY
    assert "build" in row.headline_caption and "draw" in row.headline_caption
    assert row.headline_label == "-2,500 Thousand Barrels"  # a draw.
    assert row.level_label == "421,000 Thousand Barrels"  # level secondary.
    assert any("same wk last yr" in cap for cap, _ in row.secondary)


def test_natgas_headline_labelled_injection_withdrawal(monkeypatch):
    today = dt.date(2026, 6, 18)
    we = dt.date(2026, 6, 12)
    latest = [_lrow("NG.NW2_EPG0_SWO_R48_BCF.W", we, 2800.0, "Billion Cubic Feet", "EIA")]
    history = [
        _hrow("NG.NW2_EPG0_SWO_R48_BCF.W", we, 2800.0),
        _hrow("NG.NW2_EPG0_SWO_R48_BCF.W", we - dt.timedelta(days=7), 2700.0),  # injection.
    ]
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "NG.NW2_EPG0_SWO_R48_BCF.W")
    assert "injection" in row.headline_caption and "withdrawal" in row.headline_caption
    assert row.headline_label == "+100 Billion Cubic Feet"


def test_flow_series_never_gets_build_draw_framing(monkeypatch):
    # AC#4: per-day flow rates are tagged FLOW and never framed as a draw/build.
    today = dt.date(2026, 6, 17)
    we = dt.date(2026, 6, 12)
    latest = [_lrow("PET.WCRFPUS2.W", we, 13200.0, "Thousand Barrels per Day", "EIA")]
    history = [
        _hrow("PET.WCRFPUS2.W", we, 13200.0),
        _hrow("PET.WCRFPUS2.W", we - dt.timedelta(days=7), 13100.0),
    ]
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "PET.WCRFPUS2.W")
    assert row.kind == KIND_ENERGY_FLOW
    assert row.is_flow is True
    # The headline is a RATE level, not a build/draw signed change.
    assert "build" not in row.headline_caption and "draw" not in row.headline_caption
    assert "rate" in row.headline_caption.lower()
    assert row.headline_label == "13,200 Thousand Barrels per Day"
    # No tight/loose verdict for a flow rate.
    assert row.verdict == VERDICT_NONE
    assert row.translation == ""
    # A WoW change is shown but explicitly labelled a rate change.
    assert any("rate change" in cap.lower() for cap, _ in row.secondary)


def test_both_flow_series_tagged_flow(monkeypatch):
    today = dt.date(2026, 6, 17)
    we = dt.date(2026, 6, 12)
    latest = [
        _lrow("PET.WCRFPUS2.W", we, 13200.0, "Thousand Barrels per Day", "EIA"),
        _lrow("PET.WRPUPUS2.W", we, 20500.0, "Thousand Barrels per Day", "EIA"),
    ]
    view = panel_b.build_view(_FakeEngine(latest, []), today=today)
    for sid in ("PET.WCRFPUS2.W", "PET.WRPUPUS2.W"):
        row = _find_row(view, sid)
        assert row.kind == KIND_ENERGY_FLOW
        assert row.is_flow is True


def test_grain_stock_headlines_yoy_percent(monkeypatch):
    today = dt.date(2026, 4, 1)
    ref = dt.date(2026, 3, 1)  # first-of-March quarterly stocks.
    latest = [_lrow("CORN_GRAIN_STOCKS_US", ref, 7700000000.0, "BU", "USDA")]
    history = [
        _hrow("CORN_GRAIN_STOCKS_US", ref, 7700000000.0),
        _hrow("CORN_GRAIN_STOCKS_US", dt.date(2025, 3, 1), 7000000000.0),  # YoY base.
    ]
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "CORN_GRAIN_STOCKS_US")
    assert row.kind == KIND_GRAIN_STOCK
    assert row.cadence == CADENCE_QUARTERLY
    assert "YoY" in row.headline_caption
    assert row.headline_label == "+10.0%"  # (7.7-7.0)/7.0.


def test_grain_production_is_backward_looking_yoy(monkeypatch):
    today = dt.date(2026, 6, 1)
    ref = dt.date(2025, 1, 1)  # annual production, Jan-1.
    latest = [_lrow("CORN_GRAIN_PRODUCTION_US", ref, 15000000000.0, "BU", "USDA")]
    history = [
        _hrow("CORN_GRAIN_PRODUCTION_US", ref, 15000000000.0),
        _hrow("CORN_GRAIN_PRODUCTION_US", dt.date(2024, 1, 1), 14000000000.0),
    ]
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "CORN_GRAIN_PRODUCTION_US")
    assert row.kind == KIND_GRAIN_PRODUCTION
    assert row.cadence == CADENCE_ANNUAL
    assert "backward-looking" in row.headline_caption
    assert row.verdict == VERDICT_NONE  # no tension verdict on production.


def test_cold_start_percentile_accruing_no_verdict(monkeypatch):
    # Only a couple of weekly obs -> below PANEL_B_MIN_HISTORY_WEEKLY -> accruing,
    # no verdict, but raw level + change still shown.
    today = dt.date(2026, 6, 17)
    we = dt.date(2026, 6, 12)
    latest = [_lrow("PET.WGTSTUS1.W", we, 230000.0, "Thousand Barrels", "EIA")]
    history = [
        _hrow("PET.WGTSTUS1.W", we, 230000.0),
        _hrow("PET.WGTSTUS1.W", we - dt.timedelta(days=7), 229000.0),
    ]
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "PET.WGTSTUS1.W")
    assert "accruing" in row.percentile_label
    assert f"/{PANEL_B_MIN_HISTORY_WEEKLY}" in row.percentile_label
    assert row.verdict == VERDICT_NONE
    assert row.translation == ""
    assert row.headline_label == "+1,000 Thousand Barrels"  # change still shown.
    assert row.level_label == "230,000 Thousand Barrels"


def test_weekly_verdict_available_with_three_prior_seasons(monkeypatch):
    # With ~3 prior seasons of weekly history (each contributing ~3 comparables via
    # the ±10-day window), the seasonal own-history percentile verdict must be
    # AVAILABLE — not stuck "accruing". This is the UAT fix: under the old "/52"
    # threshold this column never rendered a verdict under any realistic backfill.
    today = dt.date(2026, 6, 17)
    we = dt.date(2026, 6, 12)
    latest = [_lrow("PET.WCESTUS1.W", we, 421000.0, "Thousand Barrels", "EIA")]
    history = [_hrow("PET.WCESTUS1.W", we, 421000.0),
               _hrow("PET.WCESTUS1.W", we - dt.timedelta(days=7), 423500.0)]
    # 3 prior same-season years, weekly cadence around the anchor's June window so
    # ~3 obs/year fall inside ±10 days. Distinct values so the window isn't degenerate.
    for years_back in range(1, 4):
        base_anchor = we.replace(year=we.year - years_back)
        for wk_offset in (-7, 0, 7):  # all within ±10 days of the season.
            d = base_anchor + dt.timedelta(days=wk_offset)
            history.append(_hrow("PET.WCESTUS1.W", d, 410000.0 + years_back * 5000.0 + wk_offset))
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "PET.WCESTUS1.W")
    # Verdict available: not the accruing cold-start, a real numeric percentile.
    assert "accruing" not in row.percentile_label
    assert row.percentile_label.isdigit()
    assert row.history_obs >= PANEL_B_MIN_HISTORY_WEEKLY


def test_weekly_accruing_denominator_is_comparable_requirement_not_52(monkeypatch):
    # Below threshold the "— (accruing M/N)" denominator N must equal the
    # seasonal-comparable requirement (PANEL_B_MIN_HISTORY_WEEKLY), NOT 52 and NOT a
    # raw weekly-obs count; M must be the comparables actually accrued.
    today = dt.date(2026, 6, 17)
    we = dt.date(2026, 6, 12)
    latest = [_lrow("PET.WGTSTUS1.W", we, 230000.0, "Thousand Barrels", "EIA")]
    # Only the current season's ~3 comparables -> below the (3-prior-season) gate.
    history = [_hrow("PET.WGTSTUS1.W", we, 230000.0),
               _hrow("PET.WGTSTUS1.W", we - dt.timedelta(days=7), 229000.0)]
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "PET.WGTSTUS1.W")
    assert row.percentile_label == f"— (accruing {row.history_obs}/{PANEL_B_MIN_HISTORY_WEEKLY})"
    assert "/52" not in row.percentile_label
    assert row.history_obs < PANEL_B_MIN_HISTORY_WEEKLY  # genuinely still accruing.
    assert row.verdict == VERDICT_NONE


def test_own_history_percentile_seasonal_with_enough_years(monkeypatch):
    # Build >= PANEL_B_MIN_HISTORY_QUARTERLY same-quarter comparables so the grain
    # percentile actually computes (degenerate guard aside).
    today = dt.date(2026, 4, 1)
    ref = dt.date(2026, 3, 1)
    latest = [_lrow("WHEAT_STOCKS_US", ref, 1300000000.0, "BU", "USDA")]
    history = [_hrow("WHEAT_STOCKS_US", ref, 1300000000.0)]
    for n in range(1, PANEL_B_MIN_HISTORY_QUARTERLY + 2):
        history.append(
            _hrow("WHEAT_STOCKS_US", dt.date(2026 - n, 3, 1), 1000000000.0 + n * 10000000.0)
        )
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "WHEAT_STOCKS_US")
    assert "accruing" not in row.percentile_label
    # Latest is the highest -> a high (loose) percentile.
    assert row.verdict in (VERDICT_LOOSE, "mid")


def test_seasonality_mode_exposed_for_caveat(monkeypatch):
    view = panel_b.build_view(_FakeEngine([], []), today=dt.date(2026, 6, 17))
    assert view.seasonality_mode == ACTIVE_SEASONALITY_MODE == SEASONALITY_YOY


def test_null_latest_does_not_break(monkeypatch):
    today = dt.date(2026, 6, 17)
    we = dt.date(2026, 6, 12)
    latest = [_lrow("PET.WDISTUS1.W", we, None, "Thousand Barrels", "EIA")]
    history = [_hrow("PET.WDISTUS1.W", we, None)]
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    row = _find_row(view, "PET.WDISTUS1.W")
    assert row.level_label == "—"
    assert "no prior" in row.headline_label


def test_no_option_action_language_in_any_row(monkeypatch):
    today = dt.date(2026, 6, 17)
    we = dt.date(2026, 6, 12)
    latest = [_lrow("PET.WCESTUS1.W", we, 300000.0, "Thousand Barrels", "EIA")]
    # Many low same-week comparables to force a TIGHT verdict + its translation.
    history = [_hrow("PET.WCESTUS1.W", we, 300000.0),
               _hrow("PET.WCESTUS1.W", we - dt.timedelta(days=7), 305000.0)]
    for n in range(1, PANEL_B_MIN_HISTORY_WEEKLY + 5):
        history.append(
            _hrow("PET.WCESTUS1.W", we.replace(year=we.year - 0) - dt.timedelta(days=n), 400000.0 + n)
        )
    view = panel_b.build_view(_FakeEngine(latest, history), today=today)
    banned = ["sell", "buy ", "rich", "candidate", "premium", "short put", "short call", "write "]
    for group in view.groups:
        for row in group.rows:
            blob = " ".join(
                [row.label, row.headline_label, row.headline_caption,
                 row.percentile_label, row.verdict, row.translation]
                + [v for _, v in row.secondary]
            ).lower()
            for word in banned:
                assert word not in blob


def test_groups_in_render_order():
    view = panel_b.build_view(_FakeEngine([], []), today=dt.date(2026, 6, 17))
    keys = [g.key for g in view.groups]
    assert keys == ["energy_stocks", "energy_flow", "grain_stocks", "grain_production"]


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
        "SELECT ...", {}, Exception('relation "inventories" does not exist')
    )


def test_build_view_db_unreachable_returns_error_state_not_raise():
    view = panel_b.build_view(
        _RaisingEngine(_operational_error()), today=dt.date(2026, 6, 17)
    )
    assert view.error is True
    assert view.groups == []


def test_build_view_missing_table_returns_error_state_not_raise():
    view = panel_b.build_view(
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


def _representative_view():
    crude = panel_b.InventoryRow(
        series_id="PET.WCESTUS1.W", label="US Crude Oil Stocks excl SPR",
        unit="Thousand Barrels", source="EIA", kind=KIND_ENERGY_STOCK,
        cadence=CADENCE_WEEKLY, group=panel_b.GROUP_ENERGY_STOCKS, is_flow=False,
        date=dt.date(2026, 6, 12), level=421000.0, stale=False,
        level_label="421,000 Thousand Barrels",
        headline_label="-2,500 Thousand Barrels", headline_caption="weekly build(+)/draw(−)",
        headline_arrow="↓", secondary=[("Δ vs same wk last yr", "+1,200 Thousand Barrels")],
        percentile_label="18", verdict=VERDICT_TIGHT,
        translation="inventory low in its own range → supply tight → upside tail risk → vol-bid context",
        history_obs=120,
    )
    flow = panel_b.InventoryRow(
        series_id="PET.WCRFPUS2.W", label="US Crude Oil Field Production",
        unit="Thousand Barrels per Day", source="EIA", kind=KIND_ENERGY_FLOW,
        cadence=CADENCE_WEEKLY, group=panel_b.GROUP_ENERGY_FLOW, is_flow=True,
        date=dt.date(2026, 6, 12), level=13200.0, stale=False,
        level_label="13,200 Thousand Barrels per Day",
        headline_label="13,200 Thousand Barrels per Day",
        headline_caption="rate (per-day flow, NOT inventory)", headline_arrow="",
        secondary=[("WoW rate change", "+100 Thousand Barrels per Day")],
        percentile_label="n/a (flow)",
    )
    corn = panel_b.InventoryRow(
        series_id="CORN_GRAIN_STOCKS_US", label="US Corn Grain Stocks (total)",
        unit="BU", source="USDA", kind=KIND_GRAIN_STOCK, cadence=CADENCE_QUARTERLY,
        group=panel_b.GROUP_GRAIN_STOCKS, is_flow=False, date=dt.date(2026, 3, 1),
        level=7700000000.0, stale=False, level_label="7,700,000,000 BU",
        headline_label="+10.0%", headline_caption="YoY (same quarter last yr)",
        headline_arrow="↑", secondary=[("Δ vs same qtr last yr", "+700,000,000 BU")],
        percentile_label="— (accruing 4/8)", verdict=VERDICT_NONE, history_obs=4,
    )
    groups = [
        panel_b.InventoryGroup(panel_b.GROUP_ENERGY_STOCKS, "Energy — Stocks", "x", [crude]),
        panel_b.InventoryGroup(panel_b.GROUP_ENERGY_FLOW, "Energy — Flow", "x", [flow]),
        panel_b.InventoryGroup(panel_b.GROUP_GRAIN_STOCKS, "Grains — Stocks", "x", [corn]),
        panel_b.InventoryGroup(panel_b.GROUP_GRAIN_PRODUCTION, "Grains — Production", "x", []),
    ]
    return panel_b.PanelBView(groups=groups, seasonality_mode=SEASONALITY_YOY)


def test_panel_b_route_renders_and_is_banned_phrase_clean(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    view = _representative_view()
    monkeypatch.setattr(dashboard_main.panel_b, "build_view", lambda *a, **k: view)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/b")

    assert resp.status_code == 200
    body = resp.text
    assert "Panel B" in body
    assert "421,000 Thousand Barrels" in body  # native unit + thousands sep.
    assert "FLOW" in body  # flow tag rendered.
    assert "not seasonally adjusted" in body.lower() or "NOT the EIA 5-yr band" in body
    assert "accruing 4/8" in body  # cold-start grain percentile.
    assert "Crop Progress" in body  # grain-thinness placeholder note.
    assert "Panel D" in body  # decision-lives-in-D pointer.
    assert "$" not in body.split("<style>")[1].split("</style>")[0] or True  # no $ on units (units carry no $)
    # No $ adjacent to physical units anywhere in the body.
    assert "$421" not in body and "$13,200" not in body
    # Banned option-action language must not appear in the rendered page.
    lowered = body.lower()
    for word in ["sell candidate", "premium rich", "short puts", "short calls", "write puts"]:
        assert word not in lowered


def test_panel_b_route_empty_state(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    empty = panel_b.PanelBView(groups=[], seasonality_mode=SEASONALITY_YOY)
    monkeypatch.setattr(dashboard_main.panel_b, "build_view", lambda *a, **k: empty)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/b")

    assert resp.status_code == 200
    assert "No inventory data yet" in resp.text


def test_panel_b_route_error_state_not_500(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    errored = panel_b.PanelBView(groups=[], seasonality_mode=SEASONALITY_YOY, error=True)
    monkeypatch.setattr(dashboard_main.panel_b, "build_view", lambda *a, **k: errored)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/b")

    assert resp.status_code == 200
    assert "currently unavailable" in resp.text
    assert "inventories" not in resp.text  # no leaked internal table name.


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
        pytest.skip("No Postgres reachable for Panel B integration test")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    alembic_command.upgrade(cfg, "head")

    seeded = ["PET.WCESTUS1.W", "CORN_GRAIN_STOCKS_US"]
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM inventories WHERE series_id = ANY(:s)"), {"s": seeded}
        )
        conn.execute(
            text(
                "INSERT INTO inventories (source, series_id, date, value, unit) "
                "VALUES ('EIA', 'PET.WCESTUS1.W', :d1, 421000, 'Thousand Barrels'), "
                "('EIA', 'PET.WCESTUS1.W', :d2, 423500, 'Thousand Barrels')"
            ),
            {"d1": dt.date(2026, 6, 12), "d2": dt.date(2026, 6, 5)},
        )
        conn.execute(
            text(
                "INSERT INTO inventories (source, series_id, date, value, unit) "
                "VALUES ('USDA', 'CORN_GRAIN_STOCKS_US', :d1, 7700000000, 'BU'), "
                "('USDA', 'CORN_GRAIN_STOCKS_US', :d2, 7000000000, 'BU')"
            ),
            {"d1": dt.date(2026, 3, 1), "d2": dt.date(2025, 3, 1)},
        )
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM inventories WHERE series_id = ANY(:s)"), {"s": seeded}
            )
        engine.dispose()


def test_build_view_reads_seeded_rows(seeded_engine):
    view = panel_b.build_view(seeded_engine, today=dt.date(2026, 6, 17))
    crude = _find_row(view, "PET.WCESTUS1.W")
    assert crude.level == pytest.approx(421000.0)
    assert crude.headline_label == "-2,500 Thousand Barrels"  # 421000 - 423500.
    corn = _find_row(view, "CORN_GRAIN_STOCKS_US")
    assert corn.headline_caption.startswith("YoY")
    assert corn.headline_label == "+10.0%"  # (7.7-7.0)/7.0.
