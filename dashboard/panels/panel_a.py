"""Panel A (Macro / Cross-Asset) — read-only view model for the dashboard.

Panel A is the **cross-asset weather backdrop** for a commodity-options seller:
the broad USD (DXY proxy), real rates & breakevens, realized inflation,
growth/labor, and the VIX risk regime — presented as **context, not decisions**
(Panel D is where decisions live). It reads the FRED-sourced ``macro_metrics``
table (written by ``etl/sources/fred.py``).

This module is **read-only** — SELECT only, never a write. It holds the
per-series latest-row + bounded-history queries and the *pure* presentation
logic — frequency-aware change calcs (daily ~1m/~3m level change; monthly MoM
and YoY; the inflation-index YoY headline computed from the stored 12-months-
prior row; quarterly prior-quarter change), frequency-aware staleness (daily
trading-day-aware; monthly/quarterly *release-aware* — FRED dates a row by the
reference-period start, so we flag only when a newer reference period should
already have been published), and honest NULL /
"— (no prior)" labelling — pulled out as network-free functions so they
unit-test without a live DB, mirroring ``panel_c.py`` / ``panel_d.py``.

Deliberate constraints (from the Trader consult):

* ``DFII10`` is shown **directly** — the real yield is never recomputed as
  ``nominal − breakeven`` (FRED already provides it; a recompute would disagree).
* ``VIXCLS`` is framed as the cross-asset / equity-vol **regime** — no IV-rank
  or percentile here (that would imply a tradeable rich/cheap signal); for
  commodity vol the user goes to Panel D (GVZ/OVX).
* Coloring is **neutral** — no good/bad red/green, no loud "sell candidate"
  treatment, and no option-action language anywhere. Panel A is weather.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import load_fred_series

logger = logging.getLogger("dashboard.panel_a")

# --- Frequencies & buckets (config-driven; constants are the vocabulary) ---

FREQ_DAILY = "daily"
FREQ_MONTHLY = "monthly"
FREQ_QUARTERLY = "quarterly"

GROUP_USD = "usd"
GROUP_REAL_RATES = "real_rates"
GROUP_INFLATION = "inflation"
GROUP_GROWTH_LABOR = "growth_labor"
GROUP_RISK_REGIME = "risk_regime"

# Display order + headings for the five buckets. USD and real-rates lead and
# carry the primary visual weight; growth/labor is the most compact; risk-regime
# (VIX) is last as cross-asset context.
GROUP_ORDER: list[tuple[str, str, str]] = [
    (GROUP_USD, "US Dollar", "Broad trade-weighted USD — the cross-commodity backdrop"),
    (GROUP_REAL_RATES, "Real Rates & Inflation Expectations", "10y real yield, breakeven, nominal"),
    (GROUP_INFLATION, "Realized Inflation", "YoY headline; raw index secondary"),
    (GROUP_GROWTH_LABOR, "Growth & Labor", "Context only"),
    (GROUP_RISK_REGIME, "Risk Regime", "Cross-asset / equity-vol — see Panel D for commodity vol"),
]

# The three realized-inflation indices whose YoY % is the headline (the raw
# ~310 index level is misleading shown as if it were a rate).
_INFLATION_INDEX_IDS = frozenset({"CPIAUCSL", "PCEPI", "PPIACO"})

# Series whose level is itself a percent/rate (render with a trailing %): yields,
# breakevens, the unemployment rate. The change for these is a level difference
# in percentage *points*, not a percent-of-percent.
_RATE_LEVEL_IDS = frozenset({"DGS10", "DFII10", "T10YIE", "UNRATE"})

# VIX: context, no IV-rank. Carried as a constant so the template/route never
# re-derives the "no rank" rule.
_VIX_ID = "VIXCLS"

# The broad trade-weighted USD index (DXY proxy). It is a daily series, but its
# ~1m/~3m change reads more naturally as a *percent* of the index (a +1.70-pt
# move off a ~121.5 base is +1.4%) than as raw index points — and a percent maps
# cleanly onto the "commodity headwind/tailwind" gloss. It stays on the neutral
# arrow + honest no-prior path; only the change *units* differ from the other
# daily series, which keep percentage-point/level-point rendering.
_USD_INDEX_ID = "DTWEXBGS"

# VIX regime band cutoffs — a single named place (the same "one named place"
# pattern as _RATE_LEVEL_IDS / _INFLATION_INDEX_IDS). These are *descriptive*
# regime vocabulary, NOT a rich/cheap or tradeable threshold, and carry no
# IV-rank/percentile (a hard #15 invariant for VIX). Boundary handling is fixed
# here and asserted in tests: < 15 → calm, 15 ≤ level ≤ 25 → normal, > 25 →
# stressed. A NULL level yields no band (never infer a regime from a missing
# number).
_VIX_CALM_BELOW = 15.0
_VIX_STRESSED_ABOVE = 25.0
_VIX_BAND_CALM = "calm"
_VIX_BAND_NORMAL = "normal"
_VIX_BAND_STRESSED = "stressed"

# Commodity-linkage gloss strings for the trend context clauses. Descriptive
# backdrop only — deliberately free of any buy/sell / "premium rich" /
# "sell candidate" / "short" / "write" language (#15 AC#8). A firming USD is a
# broad headwind for dollar-priced commodities; a rising 10y real yield raises
# the carry cost of holding non-yielding metals, a precious-metals headwind.
_USD_FIRMING_GLOSS = "firming → commodity headwind"
_USD_SOFTENING_GLOSS = "softening → commodity tailwind"
_REAL_RATE_RISING_GLOSS = "rising → headwind for precious metals"
_REAL_RATE_FALLING_GLOSS = "falling → tailwind for precious metals"

# The real-yield series the real-rate trend clause reads (DFII10 shown directly,
# never recomputed as nominal − breakeven).
_REAL_YIELD_ID = "DFII10"

# Neutral "~flat" phrasing for a move too small to survive the displayed
# rounding — descriptive backdrop, no directional commodity-impact claim, no
# option-action language (so it still passes the banned-phrase assertion).
_USD_FLAT_GLOSS = "~flat"
_REAL_RATE_FLAT_GLOSS = "~flat"

# Trend-clause deadbands (one named place, mirroring the _VIX_*/_RATE_LEVEL_IDS
# constant-block pattern). The directional firming/softening (USD) and
# rising/falling (real yield) gloss must fire ONLY when the change is large
# enough to survive the rounding the headline itself uses — otherwise a
# sub-rounding drift renders a confident regime narrative against a flat-looking
# number (the Trader UAT bug). The thresholds are the smallest move that still
# rounds to a non-zero headline:
#   * USD change is fractional, shown via format_pct_change as ±0.0% (1 decimal
#     percent) → a |fraction| < 0.0005 rounds to "+0.0%"/"-0.0%", so the
#     deadband is 0.0005 (0.05%).
#   * Real-yield change is in percentage points, shown via format_points
#     (is_rate=True) as ±0.00 pp (2 decimals) → a |change| < 0.005 rounds to
#     "+0.00 pp", so the deadband is 0.005 pp.
# A move at/inside the band renders the neutral "~flat" clause instead.
_USD_TREND_DEADBAND = 0.0005
_REAL_RATE_TREND_DEADBAND = 0.005

# Frequency-aware staleness for low-cadence FRED series. FRED dates every
# observation by the reference-period START (May CPI is dated 2026-05-01, but is
# only published ~June 11). So raw day-age is wrong: the freshest available
# monthly print is already ~41 days old on its release day. Staleness must be
# *release-aware* — compare the stored reference period to the most recent
# reference period whose expected publication date has already passed as of
# ``today``, flagging only when a genuinely-newer period should already exist.
#
# Pragmatic publication-lag model (exact FRED release calendar not required):
# a reference period ending on date E is expected published ~_MONTHLY_PUB_LAG /
# _QUARTERLY_PUB_LAG days after E. The lags are deliberately generous so a slow
# release never cries wolf; a whole missing period still trips the bar.
_MONTHLY_PUB_LAG_DAYS = 20
_QUARTERLY_PUB_LAG_DAYS = 35

# Daily change windows in *trading days* (~21/session-month). We map these to a
# target calendar date and then take the nearest stored row at/just-before it.
_ONE_MONTH_TRADING_DAYS = 21
_THREE_MONTH_TRADING_DAYS = 63

# US market holidays — the same fixed, host-clock-injectable table style as
# panel_d. Used only for the daily trading-day staleness bar. Extend as years
# roll; a missing future holiday risks at most one false STALE badge, never bad
# data.
_US_MARKET_HOLIDAYS: frozenset[dt.date] = frozenset(
    {
        dt.date(2025, 1, 1),
        dt.date(2025, 1, 20),
        dt.date(2025, 2, 17),
        dt.date(2025, 4, 18),
        dt.date(2025, 5, 26),
        dt.date(2025, 6, 19),
        dt.date(2025, 7, 4),
        dt.date(2025, 9, 1),
        dt.date(2025, 11, 27),
        dt.date(2025, 12, 25),
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 19),
        dt.date(2026, 2, 16),
        dt.date(2026, 4, 3),
        dt.date(2026, 5, 25),
        dt.date(2026, 6, 19),
        dt.date(2026, 7, 3),
        dt.date(2026, 9, 7),
        dt.date(2026, 11, 26),
        dt.date(2026, 12, 25),
    }
)


# --- Trading-session helpers (pure, clock-injectable) ---------------------

def is_trading_session(day: dt.date) -> bool:
    """True if ``day`` is a weekday that is not a US market holiday."""
    return day.weekday() < 5 and day not in _US_MARKET_HOLIDAYS


def last_expected_session(today: dt.date) -> dt.date:
    """The most recent trading session strictly before ``today`` (the same
    weekend/holiday-aware definition panel_d uses for its daily bar)."""
    day = today - dt.timedelta(days=1)
    while not is_trading_session(day):
        day -= dt.timedelta(days=1)
    return day


# --- Frequency-aware staleness (pure) -------------------------------------

def _end_of_month(year: int, month: int) -> dt.date:
    """Last calendar day of ``(year, month)``."""
    if month == 12:
        return dt.date(year, 12, 31)
    return dt.date(year, month + 1, 1) - dt.timedelta(days=1)


def _latest_published_month(today: dt.date, lag_days: int) -> tuple[int, int]:
    """The most recent (year, month) reference period whose expected publication
    date (end-of-month + ``lag_days``) has already passed as of ``today``."""
    year, month = today.year, today.month
    while _end_of_month(year, month) + dt.timedelta(days=lag_days) > today:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return (year, month)


def _latest_published_quarter(today: dt.date, lag_days: int) -> tuple[int, int]:
    """The most recent (year, quarter-end-month) whose expected publication date
    (quarter-end + ``lag_days``) has already passed as of ``today``."""
    year = today.year
    q_end_month = ((today.month - 1) // 3) * 3 + 3
    while _end_of_month(year, q_end_month) + dt.timedelta(days=lag_days) > today:
        q_end_month -= 3
        if q_end_month == 0:
            year, q_end_month = year - 1, 12
    return (year, q_end_month)


def is_stale(date: Optional[dt.date], freq: str, today: dt.date) -> bool:
    """Whether the latest stored observation is materially past its next
    expected print, given the series' native cadence.

    FRED dates each observation by the reference-period START, not the release
    date, so monthly/quarterly staleness is **release-aware**, not day-age based:
    we compare the stored reference period against the most recent period whose
    expected publication has already passed, and flag only when a genuinely-newer
    period should already exist. This means the freshest available print is never
    STALE on its own release day, while a whole missing period still trips it.

    * daily → older than the last expected trading session (weekend/holiday
      aware), reusing panel_d's "prior session" model.
    * monthly → the stored reference month is older than the most recent month
      whose expected publication (end-of-month + ``_MONTHLY_PUB_LAG_DAYS``) has
      passed ``today``.
    * quarterly → same, for quarters with ``_QUARTERLY_PUB_LAG_DAYS``.

    A NULL date is never flagged stale.
    """
    if date is None:
        return False
    if freq == FREQ_DAILY:
        return date < last_expected_session(today)
    if freq == FREQ_MONTHLY:
        return (date.year, date.month) < _latest_published_month(today, _MONTHLY_PUB_LAG_DAYS)
    if freq == FREQ_QUARTERLY:
        q_end_month = ((date.month - 1) // 3) * 3 + 3
        return (date.year, q_end_month) < _latest_published_quarter(today, _QUARTERLY_PUB_LAG_DAYS)
    return False


# --- Prior-row selection (pure) -------------------------------------------

def nearest_prior(
    history: list[tuple[dt.date, Optional[float]]],
    target: dt.date,
    floor: Optional[dt.date] = None,
) -> Optional[tuple[dt.date, float]]:
    """The stored observation at/just-before ``target`` with a non-NULL value.

    ``history`` is (date, value) newest-first. We walk to the first row on or
    before ``target`` that carries a real value; an optional ``floor`` bounds how
    far back we will reach (so a daily ~1m comparison never silently picks a row
    from a year ago when the series has a gap). Returns ``None`` when no
    comparable row exists in range — the caller degrades to "— (no prior)" rather
    than fabricating a ``0`` change.
    """
    for date, value in history:
        if date > target:
            continue
        if floor is not None and date < floor:
            return None
        if value is not None:
            return (date, float(value))
    return None


# --- Change calcs (pure) --------------------------------------------------

def level_change(
    latest: Optional[float], prior: Optional[tuple[dt.date, float]]
) -> Optional[float]:
    """Absolute level change ``latest − prior`` (percentage *points* for rate
    series, index points otherwise). NULL latest or no prior → None."""
    if latest is None or prior is None:
        return None
    return latest - prior[1]


def pct_change(
    latest: Optional[float], prior: Optional[tuple[dt.date, float]]
) -> Optional[float]:
    """Fractional change ``(latest − prior) / prior`` (e.g. 0.031 == +3.1%). Used
    for the inflation-index YoY headline and the broad-USD trend. NULL latest, no
    prior, or a non-positive prior (no meaningful base) → None."""
    if latest is None or prior is None:
        return None
    base = prior[1]
    if base <= 0:
        return None
    return (latest - base) / base


def direction_arrow(change: Optional[float]) -> str:
    """A NEUTRAL direction glyph — never colored good/bad. Up/down/flat/unknown."""
    if change is None:
        return ""
    if change > 0:
        return "↑"  # ↑
    if change < 0:
        return "↓"  # ↓
    return "→"  # → (flat)


# --- Formatting (CLAUDE.md conventions) -----------------------------------

def format_level(value: Optional[float], is_rate: bool) -> str:
    """Latest level: rate series as a percent with two decimals, index/level
    series with thousands separators and two decimals. NULL → em dash."""
    if value is None:
        return "—"
    if is_rate:
        return f"{value:,.2f}%"
    return f"{value:,.2f}"


def format_points(value: Optional[float], is_rate: bool) -> str:
    """A signed level change. Rate series in percentage *points* (``pp``),
    others in plain points with thousands separators. NULL prior → "— (no prior)"."""
    if value is None:
        return "— (no prior)"
    if is_rate:
        return f"{value:+,.2f} pp"
    return f"{value:+,.2f}"


def format_pct_change(value: Optional[float]) -> str:
    """A signed fractional change as a percent (0.031 → "+3.1%"), or the honest
    no-prior label."""
    if value is None:
        return "— (no prior)"
    return f"{value * 100:+,.1f}%"


def format_date(value: Optional[dt.date]) -> str:
    if value is None:
        return "—"
    return value.isoformat()


# --- VIX regime band + trend context clauses (pure) -----------------------

def vix_band(level: Optional[float]) -> Optional[str]:
    """The descriptive VIX regime band for ``level`` — calm/normal/stressed —
    using the cutoffs in the named constant block above.

    Boundary handling is fixed here: ``< 15`` → calm, ``15 ≤ level ≤ 25`` →
    normal, ``> 25`` → stressed (15 and 25 themselves are normal). A NULL level
    yields ``None`` (no band) — never infer a regime from a missing number, and
    "no band" is deliberately distinct from "calm". This is a plain level→regime
    label, NOT an IV-rank/percentile (a hard #15 invariant for VIX).
    """
    if level is None:
        return None
    if level < _VIX_CALM_BELOW:
        return _VIX_BAND_CALM
    if level > _VIX_STRESSED_ABOVE:
        return _VIX_BAND_STRESSED
    return _VIX_BAND_NORMAL


def usd_trend_clause(pct: Optional[float], window: str = "1m") -> Optional[str]:
    """A one-line descriptive USD-trend context clause with a commodity-linkage
    gloss, e.g. "Broad USD +1.4%/1m — firming → commodity headwind".

    ``pct`` is the already-computed fractional change (off the same nearest-prior
    row Panel A already selected). A NULL/no-prior change yields ``None`` (no
    clause) rather than a fabricated direction. A move too small to survive the
    headline's rounding (|pct| < ``_USD_TREND_DEADBAND``) renders the neutral
    "~flat" gloss, never firming/softening — so a sub-rounding drift cannot read
    as a regime signal. Context only — no option-action language.
    """
    if pct is None:
        return None
    move = format_pct_change(pct)
    if pct > _USD_TREND_DEADBAND:
        gloss = _USD_FIRMING_GLOSS
    elif pct < -_USD_TREND_DEADBAND:
        gloss = _USD_SOFTENING_GLOSS
    else:
        gloss = _USD_FLAT_GLOSS
    return f"Broad USD {move}/{window} — {gloss}"


def real_rate_trend_clause(change: Optional[float], window: str = "1m") -> Optional[str]:
    """A one-line descriptive real-rate-trend context clause with a
    precious-metals-linkage gloss, e.g.
    "10y real yield +0.20 pp/1m — rising → headwind for precious metals".

    ``change`` is the already-computed percentage-*point* level change in DFII10.
    A NULL/no-prior change yields ``None`` (no clause). A move too small to
    survive the headline's rounding (|change| < ``_REAL_RATE_TREND_DEADBAND``)
    renders the neutral "~flat" gloss, never rising/falling — so a sub-rounding
    drift cannot read as a precious-metals regime signal. Context only — no
    option-action language.
    """
    if change is None:
        return None
    move = format_points(change, is_rate=True)
    if change > _REAL_RATE_TREND_DEADBAND:
        gloss = _REAL_RATE_RISING_GLOSS
    elif change < -_REAL_RATE_TREND_DEADBAND:
        gloss = _REAL_RATE_FALLING_GLOSS
    else:
        gloss = _REAL_RATE_FLAT_GLOSS
    return f"10y real yield {move}/{window} — {gloss}"


# --- View-model rows ------------------------------------------------------

@dataclass
class MacroRow:
    series_id: str
    label: str
    freq: str
    group: str
    date: Optional[dt.date]
    level: Optional[float]
    is_rate: bool
    stale: bool

    # Display strings (pre-resolved so the template stays declarative).
    level_label: str = "—"
    # Headline change — the most important read for this row's frequency.
    headline_label: str = "— (no prior)"
    headline_caption: str = ""
    headline_arrow: str = ""
    # Secondary change(s) — e.g. the daily ~3m, the monthly MoM, or the raw index.
    secondary: list[tuple[str, str]] = field(default_factory=list)
    # VIX-only descriptive regime band (calm/normal/stressed); None elsewhere and
    # on a NULL VIX level. A plain level→regime label, never an IV-rank.
    band: Optional[str] = None
    # Raw ~1m change carried for the trend-clause builder (the percent of change
    # for the USD index, the percentage-point change for the real-yield series);
    # None when there is no comparable prior. Not rendered directly.
    one_month_change: Optional[float] = None


@dataclass
class MacroGroup:
    key: str
    title: str
    subtitle: str
    rows: list[MacroRow]


@dataclass
class PanelAView:
    groups: list[MacroGroup]
    last_session: dt.date
    error: bool = False
    # Descriptive context clauses (None → render nothing, never a fabricated
    # direction): the broad-USD ~1m trend and the 10y-real-yield ~1m trend.
    usd_clause: Optional[str] = None
    real_rate_clause: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return not any(g.rows for g in self.groups)


# --- Read-only queries ----------------------------------------------------

# Latest stored observation per series with a non-NULL value (the "." FRED
# sentinel is already mapped to NULL by the ETL — a NULL latest still renders
# honestly, but for the headline level we want the freshest *real* number while
# the change calc independently sees the full history).
_LATEST_BY_SERIES_SQL = text(
    """
    SELECT DISTINCT ON (series_id)
        series_id, date, value
    FROM macro_metrics
    WHERE series_id = ANY(:series)
    ORDER BY series_id, date DESC
    """
)

# Bounded history per series (newest-first) for the change windows. The window
# is generous enough for a daily ~3m lookback and a monthly/quarterly YoY +
# prior-period; a fixed cutoff keeps the read cheap and the (series_id, date DESC)
# index from 0002 serves it.
_HISTORY_SQL = text(
    """
    SELECT series_id, date, value
    FROM macro_metrics
    WHERE series_id = ANY(:series)
      AND date >= :since
    ORDER BY series_id, date DESC
    """
)


def _series_meta() -> list[dict]:
    """The canonical Panel A series with their frequency/group metadata, read
    from config/fred_series.yaml (never hardcoded). Defaults keep an
    older config (no freq/group fields) rendering rather than crashing."""
    config = load_fred_series()
    out: list[dict] = []
    for entry in config.get("series", []):
        if entry.get("panel") != "A":
            continue
        out.append(
            {
                "id": entry["id"],
                "label": entry.get("label", entry["id"]),
                "freq": entry.get("freq", FREQ_DAILY),
                "group": entry.get("group", GROUP_RISK_REGIME),
            }
        )
    return out


def build_view(engine: Engine, today: Optional[dt.date] = None) -> PanelAView:
    """Assemble the Panel A view model with a single read-only pass over
    ``macro_metrics``. ``today`` is injectable so the change windows and
    frequency-aware staleness are testable without the wall clock."""
    today = today or dt.date.today()
    meta = _series_meta()
    series_ids = [m["id"] for m in meta]
    last_session = last_expected_session(today)
    # Reach back ~14 months so a monthly/quarterly YoY + a prior period are in
    # range, with slack for revision gaps.
    since = (today - dt.timedelta(days=430)).isoformat()

    latest: dict[str, dict] = {}
    history: dict[str, list[tuple[dt.date, Optional[float]]]] = {}
    if series_ids:
        try:
            with engine.connect() as conn:
                for row in conn.execute(_LATEST_BY_SERIES_SQL, {"series": series_ids}):
                    latest[row.series_id] = row._mapping
                for row in conn.execute(_HISTORY_SQL, {"series": series_ids, "since": since}):
                    value = None if row.value is None else float(row.value)
                    history.setdefault(row.series_id, []).append((row.date, value))
        except (OperationalError, ProgrammingError):
            # DB unreachable (OperationalError) or a pre-migration DB without the
            # macro_metrics table (ProgrammingError): one failing condition must
            # not 500 the dashboard (CLAUDE.md §4). Render the honest error state.
            # Static message — never log the DSN/credentials (mirrors /health).
            logger.exception("Panel A read failed; rendering data-unavailable state")
            return PanelAView(groups=[], last_session=last_session, error=True)

    rows = [_build_row(m, latest, history, today) for m in meta]
    groups = _group_rows(rows)

    by_id = {row.series_id: row for row in rows}
    usd_row = by_id.get(_USD_INDEX_ID)
    real_row = by_id.get(_REAL_YIELD_ID)
    usd_clause = usd_trend_clause(usd_row.one_month_change) if usd_row else None
    real_rate_clause = (
        real_rate_trend_clause(real_row.one_month_change) if real_row else None
    )

    return PanelAView(
        groups=groups,
        last_session=last_session,
        usd_clause=usd_clause,
        real_rate_clause=real_rate_clause,
    )


def _build_row(
    meta: dict,
    latest: dict[str, dict],
    history: dict[str, list[tuple[dt.date, Optional[float]]]],
    today: dt.date,
) -> MacroRow:
    series_id = meta["id"]
    freq = meta["freq"]
    group = meta["group"]
    is_rate = series_id in _RATE_LEVEL_IDS

    data = latest.get(series_id)
    date = data["date"] if data is not None else None
    level = None
    if data is not None and data["value"] is not None:
        level = float(data["value"])

    hist = history.get(series_id, [])

    row = MacroRow(
        series_id=series_id,
        label=meta["label"],
        freq=freq,
        group=group,
        date=date,
        level=level,
        is_rate=is_rate,
        stale=is_stale(date, freq, today),
        level_label=format_level(level, is_rate),
    )

    if freq == FREQ_DAILY:
        _fill_daily(row, level, date, hist)
    elif freq == FREQ_MONTHLY:
        _fill_monthly(row, series_id, level, date, hist, is_rate)
    elif freq == FREQ_QUARTERLY:
        _fill_quarterly(row, level, date, hist)
    return row


def _fill_daily(
    row: MacroRow,
    level: Optional[float],
    date: Optional[dt.date],
    hist: list[tuple[dt.date, Optional[float]]],
) -> None:
    """Daily series: ~1m headline level change + ~3m secondary, with a neutral
    arrow. VIX is a daily series but carries no rank (handled by omission — we
    only ever compute a plain level change here, then tag a descriptive regime
    band). The broad-USD index (DTWEXBGS) renders its ~1m/~3m change as a
    *percent* of the index, not raw index points; the rest of the daily path
    keeps percentage-point / level-point rendering."""
    anchor = date if date is not None else (hist[0][0] if hist else None)
    one_m = None
    three_m = None
    if anchor is not None:
        one_m_target = anchor - dt.timedelta(days=_ONE_MONTH_TRADING_DAYS * 7 // 5)
        three_m_target = anchor - dt.timedelta(days=_THREE_MONTH_TRADING_DAYS * 7 // 5)
        one_m = nearest_prior(hist, one_m_target)
        three_m = nearest_prior(hist, three_m_target)

    if row.series_id == _USD_INDEX_ID:
        one_pct = pct_change(level, one_m)
        three_pct = pct_change(level, three_m)
        row.headline_label = format_pct_change(one_pct)
        row.headline_caption = "~1m change"
        row.headline_arrow = direction_arrow(one_pct)
        row.secondary = [("~3m change", format_pct_change(three_pct))]
        row.one_month_change = one_pct
    else:
        one_change = level_change(level, one_m)
        three_change = level_change(level, three_m)
        row.headline_label = format_points(one_change, row.is_rate)
        row.headline_caption = "~1m change"
        row.headline_arrow = direction_arrow(one_change)
        row.secondary = [("~3m change", format_points(three_change, row.is_rate))]
        row.one_month_change = one_change

    if row.series_id == _VIX_ID:
        row.band = vix_band(level)


def _fill_monthly(
    row: MacroRow,
    series_id: str,
    level: Optional[float],
    date: Optional[dt.date],
    hist: list[tuple[dt.date, Optional[float]]],
    is_rate: bool,
) -> None:
    """Monthly series: MoM and YoY. For the inflation indices the YoY % computed
    from the stored 12-months-prior row is the headline; the raw index is shown
    only secondarily. For PAYEMS/UNRATE the level change (jobs added / rate move)
    is the natural read, so the headline is the MoM level change."""
    anchor = date
    mom_prior = None
    yoy_prior = None
    if anchor is not None:
        # ~1 month prior (28–35d window via nearest-prior at ~30d).
        mom_prior = nearest_prior(
            hist,
            anchor - dt.timedelta(days=20),
            floor=anchor - dt.timedelta(days=45),
        )
        # ~12 months prior.
        yoy_prior = nearest_prior(
            hist,
            anchor - dt.timedelta(days=350),
            floor=anchor - dt.timedelta(days=400),
        )

    if series_id in _INFLATION_INDEX_IDS:
        yoy = pct_change(level, yoy_prior)
        row.headline_label = format_pct_change(yoy)
        row.headline_caption = "YoY"
        row.headline_arrow = direction_arrow(yoy)
        mom_pct = pct_change(level, mom_prior)
        row.secondary = [
            ("MoM", format_pct_change(mom_pct)),
            ("index", format_level(level, False)),
        ]
    else:
        mom = level_change(level, mom_prior)
        yoy = level_change(level, yoy_prior)
        row.headline_label = format_points(mom, is_rate)
        row.headline_caption = "MoM"
        row.headline_arrow = direction_arrow(mom)
        row.secondary = [("YoY", format_points(yoy, is_rate))]


def _fill_quarterly(
    row: MacroRow,
    level: Optional[float],
    date: Optional[dt.date],
    hist: list[tuple[dt.date, Optional[float]]],
) -> None:
    """Quarterly (GDPC1): latest + prior-quarter change, clearly marked
    quarterly."""
    anchor = date
    prior = None
    if anchor is not None:
        prior = nearest_prior(
            hist,
            anchor - dt.timedelta(days=80),
            floor=anchor - dt.timedelta(days=130),
        )
    qoq = level_change(level, prior)
    qoq_pct = pct_change(level, prior)
    row.headline_label = format_pct_change(qoq_pct)
    row.headline_caption = "prior-quarter (quarterly)"
    row.headline_arrow = direction_arrow(qoq)
    row.secondary = [("level Δ", format_points(qoq, False))]


def _group_rows(rows: list[MacroRow]) -> list[MacroGroup]:
    by_group: dict[str, list[MacroRow]] = {}
    for row in rows:
        by_group.setdefault(row.group, []).append(row)
    groups: list[MacroGroup] = []
    for key, title, subtitle in GROUP_ORDER:
        groups.append(MacroGroup(key=key, title=title, subtitle=subtitle, rows=by_group.get(key, [])))
    return groups
