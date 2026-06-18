"""Panel B (Fundamentals / Inventory) — read-only view model for the dashboard.

Panel B converts raw inventory **levels** into **change** and
**position-in-own-history**. A raw absolute level (e.g. crude stocks =
421,000 thousand bbl) is useless alone — the panel surfaces the weekly
build/draw (energy), the YoY change (grains), and a *caveated* percentile in our
own stored history. It is fundamentals **context**: the decision still lives in
Panel D. It reads the EIA/USDA-sourced ``inventories`` table (written by
``etl/sources/eia.py`` and ``etl/sources/usda.py``).

This module is **read-only** — SELECT only, never a write. It holds the
per-series latest-row + bounded-history queries and the *pure* presentation logic
(weekly build/draw with native-unit + injection/withdrawal labelling, grain YoY,
percentile-in-own-history with the cold-start ``— (accruing M/N)`` and degenerate
``max==min`` → ``—`` guards, the Δ-vs-same-period-last-year seasonality default,
the neutral directional translation, and the cadence-bucketed release-aware
staleness) — pulled out as network-free functions so they unit-test without a
live DB, mirroring ``panel_a.py`` / ``panel_c.py``.

Deliberate constraints (from the Trader consult):

* **Change is the signal, not the level** — every row headlines a change.
* **Stock vs flow** — ``PET.WCRFPUS2.W`` / ``PET.WRPUPUS2.W`` are per-day FLOW
  rates, never given weekly draw/build framing.
* **No fabricated band** — the percentile is our-own-history (not the EIA 5-yr
  band), always carries a "not seasonally adjusted — NOT the EIA 5-yr band"
  caveat, and is suppressed below ``PANEL_B_MIN_HISTORY_*``.
* **Seasonality** — v1 always shows Δ-vs-same-period-last-year (the always-honest
  primary). The same-week-of-year band is a deferred upgrade.
* **Neutral framing** — fundamentals-bullish/bearish language is allowed; option-
  action / sell-instruction language is not (asserted by a banned-phrase test).
* **Grain coverage is thin** — 3 annual production + 3 quarterly stocks only;
  WASDE balance sheet + weekly Crop Progress are deferred (in-panel note).
"""

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import load_eia_series, load_usda_series

logger = logging.getLogger("dashboard.panel_b")

# --- Cadence buckets (the vocabulary; staleness is computed per bucket) -----

CADENCE_WEEKLY = "weekly"  # EIA petroleum/nat-gas stocks + flow proxies.
CADENCE_QUARTERLY = "quarterly"  # USDA grain stocks.
CADENCE_ANNUAL = "annual"  # USDA grain production.

# --- Series kinds (a row's economic shape, derived from config metadata) ----

KIND_ENERGY_STOCK = "energy_stock"  # EIA stock level → weekly build/draw.
KIND_ENERGY_FLOW = "energy_flow"  # EIA per-day rate → NEVER a build/draw.
KIND_GRAIN_STOCK = "grain_stock"  # USDA quarterly stocks → YoY %.
KIND_GRAIN_PRODUCTION = "grain_production"  # USDA annual production → YoY.

# Display groups, in render order. Tier-1 energy stocks lead; flow proxies and
# grains are de-emphasized context (Trader tiering).
GROUP_ENERGY_STOCKS = "energy_stocks"
GROUP_ENERGY_FLOW = "energy_flow"
GROUP_GRAIN_STOCKS = "grain_stocks"
GROUP_GRAIN_PRODUCTION = "grain_production"

GROUP_ORDER: list[tuple[str, str, str]] = [
    (GROUP_ENERGY_STOCKS, "Energy — Stocks (weekly build/draw)",
     "Crude, Cushing, gasoline, distillate, nat-gas working storage"),
    (GROUP_ENERGY_FLOW, "Energy — Flow rates (context, de-emphasized)",
     "Per-day rates — NOT inventory; week-to-week noisy"),
    (GROUP_GRAIN_STOCKS, "Grains — Quarterly stocks",
     "Last quarterly print (USDA Grain Stocks); YoY %"),
    (GROUP_GRAIN_PRODUCTION, "Grains — Annual production (backward-looking)",
     "Annual, Tier-3"),
]

# The two EIA flow proxies — per-day rates, NOT inventory stocks. A single named
# place so the stock-vs-flow rule is never re-hardcoded; the kind is otherwise
# inferred from the unit ("... per Day"), and membership here is the explicit
# belt-and-braces guard the AC#4 test asserts against.
_EIA_FLOW_SERIES_IDS = frozenset({"PET.WCRFPUS2.W", "PET.WRPUPUS2.W"})

# Nat-gas working storage — the weekly change reads as injection(+)/withdrawal(−),
# not build/draw. One named place.
_NATGAS_SERIES_IDS = frozenset({"NG.NW2_EPG0_SWO_R48_BCF.W"})

# Position-in-own-history minimums (a single source of truth, never re-hardcoded
# — the Panel C cold-start pattern). Below the threshold we render
# "— (accruing M/N)" with NO tight/loose verdict, only raw level + change.
#
# CRITICAL: these count SEASONAL COMPARABLES, not raw weekly/quarterly observations.
# The percentile is computed over same-period-of-year comparables — the stored
# values within ``window_days`` of the anchor's calendar date in prior years (see
# ``_same_period_last_year_history``). The verdict is available once we have enough
# prior *seasons*, per the consult rule "same-week-of-year with <3 prior years →
# accruing". So M (accrued) and N (required) below are both comparable counts, and
# "— (accruing M/N)" is honest about what it's counting (NOT raw obs).
#
# Weekly: the ±10-day window (window_days=10 → a 21-day span) captures ~3 weekly
# observations per year (offsets 0, ±7; ±14 is out of range). So "~3 prior seasons
# of history" ≈ 3 years × ~3/yr ≈ 9 seasonal comparables. 9 is the floor that ~3
# prior years reliably clears (3 prior years stored → ~11 comparables; 2 prior →
# ~8, correctly still accruing), tolerating ±1–2 calendar-alignment jitter. The old
# "52" implied 52 weekly obs and never cleared (~18 years of backfill required).
PANEL_B_MIN_HISTORY_WEEKLY = 9  # ~3 prior same-season years (≈3 comparables/yr).
# Quarterly: the ±45-day window admits exactly the same-quarter print each year
# (the adjacent quarter is ~90 days away), so it's ~1 comparable per year. 8
# comparables ≈ 8 prior years of same-quarter stocks.
PANEL_B_MIN_HISTORY_QUARTERLY = 8  # ~8 prior same-quarter years (≈1 comparable/yr).
# Annual production is rendered "n/a (annual)" (no percentile — backward-looking),
# so this is presently unused; kept for symmetry. Were it wired, ~1 comparable/yr.
PANEL_B_MIN_HISTORY_ANNUAL = 5  # ~5 prior same-period years (≈1 comparable/yr).

# Tight/loose verdict cutoffs on the [0,100] own-history percentile. Descriptive
# tension labels, NOT the EIA 5-yr band and NOT an option signal — always paired
# with the loud "not seasonally adjusted" caveat in-panel.
_TIGHT_BELOW = 20.0
_LOOSE_ABOVE = 80.0
VERDICT_TIGHT = "tight"
VERDICT_LOOSE = "loose"
VERDICT_MID = "mid"
VERDICT_NONE = ""  # accruing / degenerate — no verdict.

# Seasonality mode the view model is computing. v1 ships the always-honest
# raw-YoY primary; the same-week-of-year band is a deferred upgrade (it needs
# ≥3 stored years per week, which the backfill may not yet have). The template
# reads this so its caveat matches what was actually computed.
SEASONALITY_YOY = "yoy"
SEASONALITY_SAME_WEEK_BAND = "same_week_band"
ACTIVE_SEASONALITY_MODE = SEASONALITY_YOY

# US federal holidays — Monday holidays push the EIA weekly release +1 day, and a
# holiday in the release window grants a one-release grace before a STALE flag
# (the Panel C grace pattern). Fixed, host-clock-injectable; extend as years roll.
_US_FEDERAL_HOLIDAYS: frozenset[dt.date] = frozenset(
    {
        dt.date(2025, 1, 1),
        dt.date(2025, 1, 20),
        dt.date(2025, 2, 17),
        dt.date(2025, 5, 26),
        dt.date(2025, 6, 19),
        dt.date(2025, 7, 4),
        dt.date(2025, 9, 1),
        dt.date(2025, 11, 27),
        dt.date(2025, 12, 25),
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 19),
        dt.date(2026, 2, 16),
        dt.date(2026, 5, 25),
        dt.date(2026, 6, 19),
        dt.date(2026, 7, 3),
        dt.date(2026, 9, 7),
        dt.date(2026, 11, 26),
        dt.date(2026, 12, 25),
    }
)

# EIA weekly release weekdays (Mon=0). Petroleum status report ~Wed 10:30 ET,
# nat-gas storage report ~Thu 10:30 ET. A Monday federal holiday slips each +1.
_EIA_PETROLEUM_RELEASE_WEEKDAY = 2  # Wednesday.
_EIA_NATGAS_RELEASE_WEEKDAY = 3  # Thursday.

# A weekly value dated more than this many days before the latest expected
# week-ending is genuinely missing a release. EIA dates a row by the Friday
# week-ending; the report covering it lands the following Wed/Thu. We allow one
# normal weekly gap (7 days) plus slack, then one extra week of holiday grace.
_WEEKLY_STALE_AFTER_DAYS = 9


# --- Trading-week / release helpers (pure, clock-injectable) ---------------

def _is_monday_holiday_week(today: dt.date) -> bool:
    """Whether the Monday of ``today``'s week is a US federal holiday (which
    pushes that week's EIA petroleum/NG release +1 day)."""
    monday = today - dt.timedelta(days=today.weekday())
    return monday in _US_FEDERAL_HOLIDAYS


def _latest_eia_week_ending(today: dt.date, release_weekday: int) -> dt.date:
    """The most recent Friday week-ending whose EIA report (released on
    ``release_weekday``, +1 after a Monday holiday) has already published as of
    ``today``.

    EIA dates each observation by the Friday ending the reported week; the report
    covering week-ending Friday F is released the following ``release_weekday``
    (Wed petroleum / Thu NG). So the freshest week-ending we can expect to have
    is the one whose release day is on/before ``today``.
    """
    # Walk back to the most recent release day on/before today, accounting for a
    # Monday-holiday +1 shift in that release week.
    probe = today
    while True:
        shift = 1 if _is_monday_holiday_week(probe) else 0
        effective_release = (release_weekday + shift) % 7
        if probe.weekday() == effective_release:
            break
        probe -= dt.timedelta(days=1)
    # ``probe`` is the release day; the week it reports ended the *prior* Friday.
    friday = probe
    while friday.weekday() != 4:  # Friday.
        friday -= dt.timedelta(days=1)
    return friday


# --- Cadence-bucketed staleness (pure, clock-injectable) -------------------

def is_weekly_stale(date: Optional[dt.date], release_weekday: int, today: dt.date) -> bool:
    """Whether a weekly EIA row is stale = the next expected release has passed
    without a newer week-ending arriving.

    Release-aware (NOT raw day-age): a Wed-released crude number is current ALL
    week. A row is stale only when its week-ending precedes the latest expected
    week-ending; a one-release **holiday grace** tolerates the normal
    holiday-delayed week. A NULL date is never flagged.
    """
    if date is None:
        return False
    latest_expected = _latest_eia_week_ending(today, release_weekday)
    if date >= latest_expected:
        return False
    # Older than expected. Grant a one-release grace if a holiday fell in the
    # most recent release window (EIA routinely slips a day/week around holidays).
    grace = _had_recent_holiday(today)
    cutoff = latest_expected - dt.timedelta(days=7 if grace else 0)
    return date < cutoff


def _had_recent_holiday(today: dt.date) -> bool:
    """Whether a federal holiday fell in the trailing ~9 days (its release window),
    justifying a one-release grace before flagging weekly STALE."""
    for offset in range(0, _WEEKLY_STALE_AFTER_DAYS + 1):
        if (today - dt.timedelta(days=offset)) in _US_FEDERAL_HOLIDAYS:
            return True
    return False


def next_quarterly_stocks_date(date: dt.date) -> dt.date:
    """The next USDA Grain Stocks reference date after ``date`` — stocks are dated
    first-of-Sep/Dec/Mar/Jun, so the next is +3 months (the report itself lands
    a few weeks later). Used for the 'next report ~' expected-quiet label."""
    month = date.month
    # Map any month into the Sep/Dec/Mar/Jun cycle: step +3 months from the
    # current reference month.
    next_month = month + 3
    next_year = date.year
    if next_month > 12:
        next_month -= 12
        next_year += 1
    return dt.date(next_year, next_month, 1)


def is_quarterly_stale(date: Optional[dt.date], today: dt.date) -> bool:
    """USDA quarterly stocks are 'stale by design' between reports and must NEVER
    flag red unless a *whole* reference quarter is missing. A NULL date is never
    flagged. We flag only when the next reference date's expected publication
    (~+3 weeks) has passed without the newer print arriving."""
    if date is None:
        return False
    nxt = next_quarterly_stocks_date(date)
    expected_publication = nxt + dt.timedelta(days=30)
    return today > expected_publication


def is_annual_stale(date: Optional[dt.date], today: dt.date) -> bool:
    """USDA annual production. An annual cadence must NOT be painted permanently
    stale by a uniform day-age rule. We flag only when a whole year+ has elapsed
    past the next January reference + its publication lag. A NULL date is never
    flagged."""
    if date is None:
        return False
    next_ref = dt.date(date.year + 1, 1, 1)
    # The next annual production estimate publishes ~the following January.
    expected_publication = dt.date(next_ref.year + 1, 1, 15)
    return today > expected_publication


def is_stale(date: Optional[dt.date], cadence: str, release_weekday: int, today: dt.date) -> bool:
    """Dispatch staleness by cadence bucket (NOT one cross-table rule)."""
    if cadence == CADENCE_WEEKLY:
        return is_weekly_stale(date, release_weekday, today)
    if cadence == CADENCE_QUARTERLY:
        return is_quarterly_stale(date, today)
    if cadence == CADENCE_ANNUAL:
        return is_annual_stale(date, today)
    return False


# --- Change calcs (pure) --------------------------------------------------

def level_change(latest: Optional[float], prior: Optional[float]) -> Optional[float]:
    """Absolute change ``latest − prior`` in the native unit. NULL latest or no
    prior → None (the caller renders the honest no-prior label)."""
    if latest is None or prior is None:
        return None
    return latest - prior


def pct_change(latest: Optional[float], prior: Optional[float]) -> Optional[float]:
    """Fractional change ``(latest − prior) / prior`` (0.031 == +3.1%). Used for
    the grain YoY headline. NULL latest, no prior, or a non-positive base → None."""
    if latest is None or prior is None or prior <= 0:
        return None
    return (latest - prior) / prior


# --- Position-in-own-history percentile (pure) ----------------------------

def own_history_percentile(value: float, history: list[float]) -> Optional[float]:
    """Percentile of ``value`` within ``history`` (its own stored same-period
    comparables, value included), 0–100.

    Returns None on a degenerate window (``max == min`` — flat history / single
    distinct value) so the division never produces ``NaN``/``±inf``; the caller
    renders ``—`` (no fabricated tight/loose). ``history`` is assumed non-empty.
    """
    if not history:
        return None
    lo = min(history)
    hi = max(history)
    if hi == lo:
        return None
    return 100.0 * (value - lo) / (hi - lo)


def classify_verdict(percentile: Optional[float]) -> str:
    """A descriptive tension verdict from the own-history percentile: ≤20 tight,
    ≥80 loose, else mid. A NULL percentile (accruing / degenerate) → no verdict.
    NOT the EIA band, NOT an option signal — the caveat says so in-panel."""
    if percentile is None:
        return VERDICT_NONE
    if percentile <= _TIGHT_BELOW:
        return VERDICT_TIGHT
    if percentile >= _LOOSE_ABOVE:
        return VERDICT_LOOSE
    return VERDICT_MID


def percentile_display(
    percentile: Optional[float], history_obs: int, min_history: int
) -> str:
    """Render the own-history percentile, or the cold-start/degenerate label.

    * Enough history + a real percentile → ``"NN"`` (a 0–100 percentile).
    * Too little history → ``"— (accruing M/N)"`` (M stored, N the threshold).
    * Enough history but a degenerate (flat) window → ``"—"``.
    """
    if history_obs < min_history:
        return f"— (accruing {history_obs}/{min_history})"
    if percentile is None:
        return "—"
    return f"{percentile:,.0f}"


# --- Directional translation (pure, NEUTRAL — no option-action language) ---

def directional_translation(kind: str, verdict: str) -> str:
    """The fundamentals read rendered in-panel — NEUTRAL: tight inventory → supply
    tight → upside tail/move risk → vol bid = premium-rich CONTEXT (decision in
    Panel D). Fundamentals-bullish/bearish is allowed; option-action /
    sell-instruction language is NOT (no sell/short/write). Empty when there is no
    trustworthy verdict (accruing / mid / flow-rate / production)."""
    if kind in (KIND_ENERGY_FLOW, KIND_GRAIN_PRODUCTION):
        return ""  # flow rates & backward-looking production carry no tension read.
    if verdict == VERDICT_TIGHT:
        return "inventory low in its own range → supply tight → upside tail risk → vol-bid context"
    if verdict == VERDICT_LOOSE:
        return "inventory high in its own range → supply ample → downside cushion → vol-quiet context"
    return ""


# --- Formatting (CLAUDE.md conventions; native physical units, no $) -------

def format_level(value: Optional[float], unit: str) -> str:
    """Latest level with thousands separators + the native physical unit. NULL →
    em dash (distinct from a real 0). NO ``$`` — barrels/Bcf/bushels are not USD."""
    if value is None:
        return "—"
    return f"{value:,.0f} {unit}"


def format_signed(value: Optional[float], unit: str) -> str:
    """A signed native-unit change (build ``+`` / draw ``−``) with thousands
    separators. NULL prior → the honest no-prior label. NO ``$``."""
    if value is None:
        return "— (no prior)"
    return f"{value:+,.0f} {unit}"


def format_signed_pct(value: Optional[float]) -> str:
    """A signed fractional change as a percent (0.031 → "+3.1%"), or the honest
    no-prior label. Used for the grain YoY headline."""
    if value is None:
        return "— (no prior)"
    return f"{value * 100:+,.1f}%"


def format_date(value: Optional[dt.date]) -> str:
    if value is None:
        return "—"
    return value.isoformat()


def build_change_arrow(change: Optional[float]) -> str:
    """A NEUTRAL build/draw glyph — never colored good/bad."""
    if change is None:
        return ""
    if change > 0:
        return "↑"
    if change < 0:
        return "↓"
    return "→"


# --- View-model rows ------------------------------------------------------

@dataclass
class InventoryRow:
    series_id: str
    label: str
    unit: str
    source: str
    kind: str
    cadence: str
    group: str
    is_flow: bool
    date: Optional[dt.date]
    level: Optional[float]
    stale: bool

    # Display strings (pre-resolved so the template stays declarative).
    level_label: str = "—"
    # Headline change — weekly build/draw (energy) or YoY (grains).
    headline_label: str = "— (no prior)"
    headline_caption: str = ""
    headline_arrow: str = ""
    # Secondary read(s): Δ vs same week/quarter last year, WoW rate (flow), etc.
    secondary: list[tuple[str, str]] = field(default_factory=list)
    # Own-history percentile + caveated verdict (suppressed below threshold).
    percentile_label: str = "—"
    verdict: str = VERDICT_NONE
    translation: str = ""
    history_obs: int = 0


@dataclass
class InventoryGroup:
    key: str
    title: str
    subtitle: str
    rows: list[InventoryRow]


@dataclass
class PanelBView:
    groups: list[InventoryGroup]
    seasonality_mode: str
    error: bool = False

    @property
    def is_empty(self) -> bool:
        return not any(g.rows for g in self.groups)


# --- Read-only queries ----------------------------------------------------

# Latest stored observation per (source, series_id) with the freshest date.
_LATEST_SQL = text(
    """
    SELECT DISTINCT ON (source, series_id)
        source, series_id, date, value, unit
    FROM inventories
    WHERE series_id = ANY(:series)
    ORDER BY source, series_id, date DESC
    """
)

# Bounded history per series (newest-first) for the change windows + the
# own-history percentile. The window is generous enough for a weekly ~52w YoY +
# a multi-year percentile; the (series_id, date DESC) index from 0002 serves it.
_HISTORY_SQL = text(
    """
    SELECT series_id, date, value
    FROM inventories
    WHERE series_id = ANY(:series)
      AND date >= :since
    ORDER BY series_id, date DESC
    """
)


def _series_meta() -> list[dict]:
    """The canonical Panel B series with their kind/cadence/group metadata,
    derived from config/eia_series.yaml + config/usda_series.yaml (never
    hardcoded). An unknown/extra config key is ignored, not crashed on."""
    out: list[dict] = []

    for entry in load_eia_series().get("series", []):
        if entry.get("panel") != "B":
            continue
        series_id = entry["id"]
        unit = entry.get("unit", "")
        is_flow = series_id in _EIA_FLOW_SERIES_IDS or "per Day" in unit
        kind = KIND_ENERGY_FLOW if is_flow else KIND_ENERGY_STOCK
        group = GROUP_ENERGY_FLOW if is_flow else GROUP_ENERGY_STOCKS
        out.append(
            {
                "id": series_id,
                "label": entry.get("label", series_id),
                "unit": unit,
                "source": "EIA",
                "kind": kind,
                "cadence": CADENCE_WEEKLY,
                "group": group,
                "is_flow": is_flow,
            }
        )

    for entry in load_usda_series().get("series", []):
        if entry.get("panel") != "B":
            continue
        series_id = entry["id"]
        is_production = "PRODUCTION" in series_id.upper()
        if is_production:
            kind, cadence, group = (
                KIND_GRAIN_PRODUCTION, CADENCE_ANNUAL, GROUP_GRAIN_PRODUCTION
            )
        else:
            kind, cadence, group = (
                KIND_GRAIN_STOCK, CADENCE_QUARTERLY, GROUP_GRAIN_STOCKS
            )
        out.append(
            {
                "id": series_id,
                "label": entry.get("label", series_id),
                "unit": entry.get("unit", ""),
                "source": "USDA",
                "kind": kind,
                "cadence": cadence,
                "group": group,
                "is_flow": False,
            }
        )

    return out


def build_view(engine: Engine, today: Optional[dt.date] = None) -> PanelBView:
    """Assemble the Panel B view model with a single read-only pass over
    ``inventories``. ``today`` is injectable so the change windows and
    cadence-bucketed staleness are testable without the wall clock."""
    today = today or dt.date.today()
    meta = _series_meta()
    series_ids = [m["id"] for m in meta]
    # Reach back ~10 years so a weekly own-history percentile and a YoY prior are
    # in range (the percentile is most meaningful with several seasons stored).
    since = (today - dt.timedelta(days=3660)).isoformat()

    latest: dict[str, dict] = {}
    history: dict[str, list[tuple[dt.date, Optional[float]]]] = {}
    if series_ids:
        try:
            with engine.connect() as conn:
                for row in conn.execute(_LATEST_SQL, {"series": series_ids}):
                    latest[row.series_id] = row._mapping
                for row in conn.execute(_HISTORY_SQL, {"series": series_ids, "since": since}):
                    value = None if row.value is None else float(row.value)
                    history.setdefault(row.series_id, []).append((row.date, value))
        except (OperationalError, ProgrammingError):
            # DB unreachable (OperationalError) or a pre-migration DB without the
            # inventories table (ProgrammingError): one failing condition must not
            # 500 the dashboard (CLAUDE.md §4). Render the honest error state.
            # Static message — never log the DSN/credentials (mirrors /health).
            logger.exception("Panel B read failed; rendering data-unavailable state")
            return PanelBView(
                groups=[], seasonality_mode=ACTIVE_SEASONALITY_MODE, error=True
            )

    rows = [_build_row(m, latest, history, today) for m in meta]
    groups = _group_rows(rows)
    return PanelBView(groups=groups, seasonality_mode=ACTIVE_SEASONALITY_MODE)


def _build_row(
    meta: dict,
    latest: dict[str, dict],
    history: dict[str, list[tuple[dt.date, Optional[float]]]],
    today: dt.date,
) -> InventoryRow:
    series_id = meta["id"]
    kind = meta["kind"]
    cadence = meta["cadence"]
    unit = meta["unit"]

    data = latest.get(series_id)
    date = data["date"] if data is not None else None
    level = None
    if data is not None and data["value"] is not None:
        level = float(data["value"])
    # Prefer the config unit; fall back to whatever the ETL stored.
    if not unit and data is not None and data["unit"]:
        unit = data["unit"]

    hist = history.get(series_id, [])

    release_weekday = (
        _EIA_NATGAS_RELEASE_WEEKDAY
        if series_id in _NATGAS_SERIES_IDS
        else _EIA_PETROLEUM_RELEASE_WEEKDAY
    )

    row = InventoryRow(
        series_id=series_id,
        label=meta["label"],
        unit=unit,
        source=meta["source"],
        kind=kind,
        cadence=cadence,
        group=meta["group"],
        is_flow=meta["is_flow"],
        date=date,
        level=level,
        stale=is_stale(date, cadence, release_weekday, today),
        level_label=format_level(level, unit),
    )

    if kind == KIND_ENERGY_STOCK:
        _fill_energy_stock(row, series_id, level, date, hist, unit)
    elif kind == KIND_ENERGY_FLOW:
        _fill_energy_flow(row, level, date, hist, unit)
    elif kind == KIND_GRAIN_STOCK:
        _fill_grain_stock(row, level, date, hist)
    elif kind == KIND_GRAIN_PRODUCTION:
        _fill_grain_production(row, level, date, hist)

    return row


def _nearest(
    hist: list[tuple[dt.date, Optional[float]]],
    target: dt.date,
    floor: dt.date,
) -> Optional[float]:
    """The non-NULL value at/just-before ``target`` (newest-first ``hist``),
    bounded below by ``floor`` so a gap never silently reaches across a year.
    None when no comparable row exists in range."""
    for date, value in hist:
        if date > target:
            continue
        if date < floor:
            return None
        if value is not None:
            return value
    return None


def _same_period_last_year_history(
    hist: list[tuple[dt.date, Optional[float]]], anchor: dt.date, window_days: int
) -> list[float]:
    """Own-history values comparable to ``anchor``'s period-of-year: for each
    prior year, the stored value within ``window_days`` of the same calendar date.
    This keeps the percentile seasonality-aware (vs the same week/quarter across
    years), not a raw all-history percentile that a seasonal trough would skew.
    Includes the anchor's own value."""
    comparables: list[float] = []
    for date, value in hist:
        if value is None:
            continue
        # Distance from the anchor's month/day in either direction within a year.
        same_year_anchor = anchor.replace(year=date.year) if _valid_replace(anchor, date.year) else None
        if same_year_anchor is None:
            continue
        delta = abs((date - same_year_anchor).days)
        if delta <= window_days:
            comparables.append(value)
    return comparables


def _valid_replace(anchor: dt.date, year: int) -> bool:
    try:
        anchor.replace(year=year)
        return True
    except ValueError:  # Feb 29 in a non-leap year.
        return False


def _percentile_for(
    row: InventoryRow,
    level: Optional[float],
    date: Optional[dt.date],
    hist: list[tuple[dt.date, Optional[float]]],
    min_history: int,
    window_days: int,
) -> None:
    """Compute the seasonality-aware own-history percentile + caveated verdict
    and the neutral translation; suppress below ``min_history``."""
    comparables: list[float] = []
    if level is not None and date is not None:
        comparables = _same_period_last_year_history(hist, date, window_days)
    obs = len(comparables)
    row.history_obs = obs
    percentile = None
    if level is not None and obs >= min_history:
        percentile = own_history_percentile(level, comparables)
    row.percentile_label = percentile_display(percentile, obs, min_history)
    # A verdict (and its translation) only when we have a trustworthy percentile.
    row.verdict = classify_verdict(percentile) if obs >= min_history else VERDICT_NONE
    row.translation = directional_translation(row.kind, row.verdict)


def _fill_energy_stock(
    row: InventoryRow,
    series_id: str,
    level: Optional[float],
    date: Optional[dt.date],
    hist: list[tuple[dt.date, Optional[float]]],
    unit: str,
) -> None:
    """EIA weekly stock: headline = weekly build(+)/draw(−) in the native unit
    (nat-gas labelled injection/withdrawal); secondary = Δ vs same week last
    year; plus a seasonality-aware own-history percentile."""
    is_natgas = series_id in _NATGAS_SERIES_IDS
    wow_prior = None
    yoy_prior = None
    if date is not None:
        wow_prior = _nearest(hist, date - dt.timedelta(days=7), floor=date - dt.timedelta(days=13))
        yoy_prior = _nearest(hist, date - dt.timedelta(days=364), floor=date - dt.timedelta(days=378))

    wow = level_change(level, wow_prior)
    yoy = level_change(level, yoy_prior)
    row.headline_label = format_signed(wow, unit)
    row.headline_caption = (
        "weekly injection(+)/withdrawal(−)" if is_natgas else "weekly build(+)/draw(−)"
    )
    row.headline_arrow = build_change_arrow(wow)
    row.secondary = [("Δ vs same wk last yr", format_signed(yoy, unit))]
    _percentile_for(row, level, date, hist, PANEL_B_MIN_HISTORY_WEEKLY, window_days=10)


def _fill_energy_flow(
    row: InventoryRow,
    level: Optional[float],
    date: Optional[dt.date],
    hist: list[tuple[dt.date, Optional[float]]],
    unit: str,
) -> None:
    """EIA per-day FLOW rate: NEVER a weekly build/draw. Headline = the rate
    level; secondary = a clearly-labelled week-over-week *rate change* (not an
    inventory draw) and Δ vs same week last year. No tight/loose verdict — a flow
    rate has no inventory tension percentile."""
    wow_prior = None
    yoy_prior = None
    if date is not None:
        wow_prior = _nearest(hist, date - dt.timedelta(days=7), floor=date - dt.timedelta(days=13))
        yoy_prior = _nearest(hist, date - dt.timedelta(days=364), floor=date - dt.timedelta(days=378))
    wow_rate = level_change(level, wow_prior)
    yoy_rate = level_change(level, yoy_prior)
    row.headline_label = format_level(level, unit)
    row.headline_caption = "rate (per-day flow, NOT inventory)"
    row.headline_arrow = ""
    row.secondary = [
        ("WoW rate change", format_signed(wow_rate, unit)),
        ("Δ vs same wk last yr", format_signed(yoy_rate, unit)),
    ]
    # No percentile/verdict/translation for a flow rate (left at defaults).
    row.percentile_label = "n/a (flow)"


def _fill_grain_stock(
    row: InventoryRow,
    level: Optional[float],
    date: Optional[dt.date],
    hist: list[tuple[dt.date, Optional[float]]],
) -> None:
    """USDA quarterly stocks: headline = YoY % vs the same quarter last year;
    absolute level secondary; plus a seasonality-aware own-history percentile."""
    yoy_prior = None
    if date is not None:
        yoy_prior = _nearest(hist, date - dt.timedelta(days=365), floor=date - dt.timedelta(days=400))
    yoy_pct = pct_change(level, yoy_prior)
    yoy_abs = level_change(level, yoy_prior)
    row.headline_label = format_signed_pct(yoy_pct)
    row.headline_caption = "YoY (same quarter last yr)"
    row.headline_arrow = build_change_arrow(yoy_pct)
    row.secondary = [("Δ vs same qtr last yr", format_signed(yoy_abs, row.unit))]
    _percentile_for(row, level, date, hist, PANEL_B_MIN_HISTORY_QUARTERLY, window_days=45)


def _fill_grain_production(
    row: InventoryRow,
    level: Optional[float],
    date: Optional[dt.date],
    hist: list[tuple[dt.date, Optional[float]]],
) -> None:
    """USDA annual production (Tier-3, backward-looking): headline = YoY change vs
    the prior year, rendered small. No tight/loose verdict — annual production is
    not an inventory tension read."""
    prior = None
    if date is not None:
        prior = _nearest(hist, date - dt.timedelta(days=366), floor=date - dt.timedelta(days=500))
    yoy_abs = level_change(level, prior)
    yoy_pct = pct_change(level, prior)
    row.headline_label = format_signed_pct(yoy_pct)
    row.headline_caption = "YoY (prior year) — backward-looking"
    row.headline_arrow = build_change_arrow(yoy_abs)
    row.secondary = [("Δ vs prior yr", format_signed(yoy_abs, row.unit))]
    # No percentile/verdict/translation for backward-looking production.
    row.percentile_label = "n/a (annual)"


def _group_rows(rows: list[InventoryRow]) -> list[InventoryGroup]:
    by_group: dict[str, list[InventoryRow]] = {}
    for row in rows:
        by_group.setdefault(row.group, []).append(row)
    groups: list[InventoryGroup] = []
    for key, title, subtitle in GROUP_ORDER:
        groups.append(
            InventoryGroup(key=key, title=title, subtitle=subtitle, rows=by_group.get(key, []))
        )
    return groups
