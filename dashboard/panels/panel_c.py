"""Panel C (Positioning & Flow) — read-only view model for the dashboard.

Panel C surfaces two distinct, deliberately-separate positioning signals for a
premium seller:

* a **COT positioning table** over all ~28 CFTC Legacy futures-only contracts
  (``cot``, written by ``etl/sources/cftc.py``), headlined by a 3-year **COT
  index** of the net large-spec position — flagging which contracts have specs
  *crowded long/short* and therefore which side of the tail risk you should NOT
  be short; and
* a small **energy curve-shape strip** (CL/BZ/NG/RB/HO) from ``curve_shape``
  (written by ``etl/sources/curve_shape.py``), flagging contango/backwardation.

This module is **read-only** — SELECT only, never a write. It holds the
per-symbol latest-row + history queries and the *pure* presentation logic (COT
index computation, 80/20 crowding classification + directional inference, the
ABS-from-50 NULLS-LAST sort key, the weekly Tue→Fri expected-report-date
staleness model with holiday grace, the cold-start ``accruing M/156`` labelling,
and the curve NULL-vs-flat labelling) — pulled out as network-free functions so
they unit-test without a live DB, mirroring ``dashboard/panels/panel_d.py``.

CONSTRAINT — Legacy split: non-comm = ALL large specs; there is no
managed-money column in the Legacy report, so the metric is **net large-spec**,
not MM-net. The in-panel footnote says so explicitly.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import load_cftc_markets, load_curve_config

logger = logging.getLogger("dashboard.panel_c")

# --- Named config constants (referenced everywhere; never re-hardcoded) ---

# 3-year COT index lookback, in weeks. The index window AND the accruing
# denominator both read this single constant (Panel D threshold style).
COT_INDEX_LOOKBACK_WEEKS = 156

# Below this many stored weeks the index is not trustworthy — render the
# cold-start "— (accruing M/156)" state instead of fabricating an extreme.
COT_MIN_HISTORY_WEEKS = 104

# Crowding thresholds on the [0, 100] COT index.
COT_CROWDED_LONG_THRESHOLD = 80
COT_CROWDED_SHORT_THRESHOLD = 20

# Crowding classes — the template maps these to loud row treatments.
CROWD_LONG = "crowded-long"
CROWD_SHORT = "crowded-short"
CROWD_NONE = "default"

# US federal holidays after which CFTC slips the COT release. Reused for the
# one-release holiday grace before flagging a weekly report STALE. Mirrors the
# fixed-table approach in panel_d (deterministic, host-clock-injectable).
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

# CFTC Legacy futures-only: positions are as of Tuesday, released the following
# Friday at ~15:30 ET. We treat the whole Friday as the release boundary (a
# read on/after that Friday sees the new report).
_COT_AS_OF_WEEKDAY = 1  # Tuesday (Mon=0)
_COT_RELEASE_WEEKDAY = 4  # Friday


# --- COT index (pure) -----------------------------------------------------

def cot_index(net_spec_today: float, history: list[float]) -> Optional[float]:
    """3-year COT index = 100 × (today − min) / (max − min) over ``history``
    (the net-spec values within the lookback window, today included).

    Returns NULL (caller renders the accruing/``—`` state) when the window is
    degenerate — ``max == min`` (flat history / single distinct value) — so the
    division never produces ``NaN``/``±inf``. ``history`` is assumed already
    clamped to ``COT_INDEX_LOOKBACK_WEEKS`` and non-empty.
    """
    if not history:
        return None
    lo = min(history)
    hi = max(history)
    if hi == lo:
        return None
    return 100.0 * (net_spec_today - lo) / (hi - lo)


def classify_crowding(index_value: Optional[float]) -> str:
    """Crowding class from the COT index: ≥80 LONG, ≤20 SHORT, else neutral.
    A NULL index (accruing / degenerate) is never crowded."""
    if index_value is None:
        return CROWD_NONE
    if index_value >= COT_CROWDED_LONG_THRESHOLD:
        return CROWD_LONG
    if index_value <= COT_CROWDED_SHORT_THRESHOLD:
        return CROWD_SHORT
    return CROWD_NONE


def crowding_inference(crowding: str) -> str:
    """The directional read shown in-panel (not just a code comment): a COT
    extreme flags the side of the tail risk = the option you should NOT be
    short."""
    if crowding == CROWD_LONG:
        return "specs crowded LONG — tail risk DOWN (long-liquidation); don't be short puts / lean calls"
    if crowding == CROWD_SHORT:
        return "specs crowded SHORT — tail risk UP (short-squeeze); don't be short calls / lean puts"
    return ""


def cot_index_display(
    index_value: Optional[float],
    history_weeks: int,
    min_weeks: int = COT_MIN_HISTORY_WEEKS,
    lookback_weeks: int = COT_INDEX_LOOKBACK_WEEKS,
) -> str:
    """Render the headline COT index, or the cold-start/degenerate label.

    * A real index → ``"NN"`` (no decimals — it is a 0–100 percentile).
    * Too little history (``history_weeks < min_weeks``) → ``"— (accruing M/156)"``
      where ``M`` is the stored weeks and ``156`` is the lookback constant.
    * Enough history but a degenerate (flat) window → ``"—"``.
    """
    if history_weeks < min_weeks:
        return f"— (accruing {history_weeks}/{lookback_weeks})"
    if index_value is None:
        return "—"
    return f"{index_value:,.0f}"


def cot_sort_key(index_value: Optional[float]):
    """Sort key for ``ABS(cot_index − 50) DESC`` with NULL/accruing LAST: real
    indices sort by distance from the neutral 50 (both extremes surface), NULL
    indices sink below every real one regardless of distance."""
    if index_value is None:
        return (0, 0.0)
    return (1, abs(index_value - 50.0))


# --- Weekly COT staleness (pure, clock-injectable) ------------------------

def expected_cot_report_date(today: dt.date) -> dt.date:
    """The most-recent **expected** COT ``report_date`` (a Tuesday) whose
    following-Friday ~15:30 ET release has already passed as of ``today``.

    The Tuesday positioning is released the next Friday; so the freshest report
    we can expect to have is the latest Tuesday whose release-Friday is on or
    before ``today``. Clock-injectable — ``today`` is passed, never read.
    """
    # Walk back from today to the most recent (or current) Friday release that
    # has occurred, then map that Friday back to its Tuesday positioning date.
    release_friday = today
    while release_friday.weekday() != _COT_RELEASE_WEEKDAY:
        release_friday -= dt.timedelta(days=1)
    # The Tuesday three days before that Friday.
    return release_friday - dt.timedelta(days=(_COT_RELEASE_WEEKDAY - _COT_AS_OF_WEEKDAY))


def _had_holiday_grace_window(expected: dt.date) -> bool:
    """Whether the release for ``expected`` (the Tuesday) plausibly slipped a
    week because a federal holiday fell in its Tue→Fri release window. Used to
    grant a one-release grace before flagging STALE."""
    # The release week runs from the as-of Tuesday through its release Friday.
    release_friday = expected + dt.timedelta(days=(_COT_RELEASE_WEEKDAY - _COT_AS_OF_WEEKDAY))
    day = expected
    while day <= release_friday:
        if day in _US_FEDERAL_HOLIDAYS:
            return True
        day += dt.timedelta(days=1)
    return False


def is_cot_stale(report_date: Optional[dt.date], today: dt.date) -> bool:
    """A COT row is stale when its stored ``report_date`` is older than the
    expected Tuesday report — i.e. the Friday ETL didn't run. A NULL date is
    never flagged. A one-release **holiday grace** tolerates the normal
    holiday-delayed week before flagging STALE.
    """
    if report_date is None:
        return False
    expected = expected_cot_report_date(today)
    if report_date >= expected:
        return False
    # Older than expected: allow a single missed release if a federal holiday
    # fell in the expected report's release window (CFTC routinely slips).
    if _had_holiday_grace_window(expected):
        prior_expected = expected - dt.timedelta(days=7)
        return report_date < prior_expected
    return True


# --- Curve NULL-vs-flat labelling (pure) ----------------------------------

CURVE_BACKWARDATION = "backwardation"
CURVE_CONTANGO = "contango"
CURVE_FLAT = "flat"
CURVE_NONE = "no-curve"


def curve_structure_class(structure: Optional[str]) -> str:
    """Map the stored ``structure`` text to a template CSS class. NULL (absence
    of a clean curve) is a distinct class from ``'flat'`` (a real reading inside
    the deadband) — they must look different and a NULL leg is never relabelled
    flat."""
    if structure is None:
        return CURVE_NONE
    if structure == CURVE_BACKWARDATION:
        return CURVE_BACKWARDATION
    if structure == CURVE_CONTANGO:
        return CURVE_CONTANGO
    return CURVE_FLAT


def curve_structure_label(structure: Optional[str]) -> str:
    """Human label for the curve structure. NULL → ``"— (no curve)"``, visually
    and textually distinct from ``"flat"``."""
    if structure is None:
        return "— (no curve)"
    return structure


# --- Formatting (CLAUDE.md conventions) -----------------------------------

def format_int(value: Optional[int]) -> str:
    """An integer count with thousands separators (signed if negative), or an em
    dash for NULL."""
    if value is None:
        return "—"
    return f"{value:,d}"


def format_pct(value: Optional[float]) -> str:
    """A [0,1]-ish fraction → a percentage with one decimal, or an em dash for
    NULL. ``slope_pct`` and ``net_spec_pct_oi`` are stored/derived as decimals
    (0.30 == 30%)."""
    if value is None:
        return "—"
    return f"{value * 100:,.1f}%"


def format_price(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def format_date(value: Optional[dt.date]) -> str:
    if value is None:
        return "—"
    return value.isoformat()


# --- View-model rows ------------------------------------------------------

@dataclass
class CotRow:
    symbol: str
    name: str
    report_date: Optional[dt.date]
    noncomm_long: Optional[int]
    noncomm_short: Optional[int]
    net_spec: Optional[int]
    net_spec_pct_oi: Optional[float]
    open_interest: Optional[int]
    index_value: Optional[float]
    index_label: str
    crowding: str
    inference: str
    history_weeks: int
    structure_echo: Optional[str]
    stale: bool


@dataclass
class CurveCard:
    symbol: str
    structure: Optional[str]
    structure_class: str
    structure_label: str
    slope_pct: Optional[float]
    front_price: Optional[float]
    back_price: Optional[float]
    spread: Optional[float]
    date: Optional[dt.date]


@dataclass
class PanelCView:
    cot_rows: list[CotRow]
    curve_cards: list[CurveCard]
    expected_report_date: dt.date
    error: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.cot_rows and not self.curve_cards


# --- Read-only queries ----------------------------------------------------

# Latest COT row per symbol (the (symbol, report_date DESC) index from 0002
# supports the DISTINCT ON).
_LATEST_COT_SQL = text(
    """
    SELECT DISTINCT ON (symbol)
        symbol, report_date, noncomm_long, noncomm_short, open_interest
    FROM cot
    WHERE symbol = ANY(:symbols)
    ORDER BY symbol, report_date DESC
    """
)

# Per-symbol net-spec history within the lookback window, newest first. Read at
# request time to compute the index + the accruing M count without assuming
# gap-free weeks. noncomm_long/short NULL rows are excluded — they cannot form a
# net.
_COT_HISTORY_SQL = text(
    """
    SELECT symbol, (noncomm_long - noncomm_short) AS net_spec
    FROM cot
    WHERE symbol = ANY(:symbols)
      AND report_date >= :since
      AND noncomm_long IS NOT NULL
      AND noncomm_short IS NOT NULL
    ORDER BY symbol, report_date DESC
    """
)

# Latest curve row per energy symbol.
_LATEST_CURVE_SQL = text(
    """
    SELECT DISTINCT ON (symbol)
        symbol, date, front_price, back_price, spread, slope_pct, structure
    FROM curve_shape
    WHERE symbol = ANY(:symbols)
    ORDER BY symbol, date DESC
    """
)


def _cot_market_map() -> dict[str, str]:
    """CFTC symbol → display name, from config/cftc_markets.yaml (the canonical
    ~28-contract universe; never hardcoded here)."""
    config = load_cftc_markets()
    return {m["symbol"]: m.get("name", m["symbol"]) for m in config.get("markets", [])}


def _curve_symbols() -> list[str]:
    """Energy curve symbols, from the config/symbols.yaml ``curve`` block."""
    curve = load_curve_config()
    return [u["symbol"] for u in curve.get("underlyings", [])]


def _as_int(value) -> Optional[int]:
    return None if value is None else int(value)


def build_view(engine: Engine, today: Optional[dt.date] = None) -> PanelCView:
    """Assemble the Panel C view model with a single read-only pass over ``cot``
    and ``curve_shape``. ``today`` is injectable so the weekly staleness is
    testable without the wall clock."""
    today = today or dt.date.today()
    market_map = _cot_market_map()
    curve_symbols = _curve_symbols()
    cot_symbols = list(market_map.keys())
    expected = expected_cot_report_date(today)
    since = (today - dt.timedelta(weeks=COT_INDEX_LOOKBACK_WEEKS)).isoformat()

    latest_cot: dict[str, dict] = {}
    history: dict[str, list[int]] = {}
    latest_curve: dict[str, dict] = {}
    try:
        with engine.connect() as conn:
            if cot_symbols:
                for row in conn.execute(_LATEST_COT_SQL, {"symbols": cot_symbols}):
                    latest_cot[row.symbol] = row._mapping
                for row in conn.execute(
                    _COT_HISTORY_SQL, {"symbols": cot_symbols, "since": since}
                ):
                    history.setdefault(row.symbol, []).append(int(row.net_spec))
            if curve_symbols:
                for row in conn.execute(_LATEST_CURVE_SQL, {"symbols": curve_symbols}):
                    latest_curve[row.symbol] = row._mapping
    except (OperationalError, ProgrammingError):
        # DB unreachable (OperationalError) or a pre-migration DB without the
        # cot/curve_shape tables (ProgrammingError): one failing condition must
        # not 500 the dashboard (CLAUDE.md §4). Render the honest error state.
        # Static message — never log the DSN/credentials (mirrors /health).
        logger.exception("Panel C read failed; rendering data-unavailable state")
        return PanelCView(
            cot_rows=[], curve_cards=[], expected_report_date=expected, error=True
        )

    cot_rows = _build_cot_rows(market_map, latest_cot, history, latest_curve, today)
    curve_cards = _build_curve_cards(curve_symbols, latest_curve)
    return PanelCView(
        cot_rows=cot_rows, curve_cards=curve_cards, expected_report_date=expected
    )


def _build_cot_rows(
    market_map: dict[str, str],
    latest_cot: dict[str, dict],
    history: dict[str, list[int]],
    latest_curve: dict[str, dict],
    today: dt.date,
) -> list[CotRow]:
    rows: list[CotRow] = []
    for symbol, name in market_map.items():
        data = latest_cot.get(symbol)
        if data is None:
            continue  # no COT report yet for this contract — do not fabricate a row.
        nc_long = _as_int(data["noncomm_long"])
        nc_short = _as_int(data["noncomm_short"])
        oi = _as_int(data["open_interest"])
        report_date = data["report_date"]

        net_spec = None
        if nc_long is not None and nc_short is not None:
            net_spec = nc_long - nc_short

        net_pct_oi = None
        if net_spec is not None and oi:
            net_pct_oi = net_spec / oi

        net_history = history.get(symbol, [])
        history_weeks = len(net_history)
        index_value = None
        if net_spec is not None and history_weeks >= COT_MIN_HISTORY_WEEKS:
            index_value = cot_index(float(net_spec), [float(x) for x in net_history])
        index_label = cot_index_display(index_value, history_weeks)
        crowding = classify_crowding(index_value)

        curve = latest_curve.get(symbol)
        structure_echo = curve["structure"] if curve is not None else None

        rows.append(
            CotRow(
                symbol=symbol,
                name=name,
                report_date=report_date,
                noncomm_long=nc_long,
                noncomm_short=nc_short,
                net_spec=net_spec,
                net_spec_pct_oi=net_pct_oi,
                open_interest=oi,
                index_value=index_value,
                index_label=index_label,
                crowding=crowding,
                inference=crowding_inference(crowding),
                history_weeks=history_weeks,
                structure_echo=structure_echo,
                stale=is_cot_stale(report_date, today),
            )
        )
    rows.sort(key=lambda r: cot_sort_key(r.index_value), reverse=True)
    return rows


def _build_curve_cards(
    curve_symbols: list[str], latest_curve: dict[str, dict]
) -> list[CurveCard]:
    cards: list[CurveCard] = []
    for symbol in curve_symbols:
        data = latest_curve.get(symbol)
        if data is None:
            continue  # no curve snapshot yet for this energy symbol.
        structure = data["structure"]
        cards.append(
            CurveCard(
                symbol=symbol,
                structure=structure,
                structure_class=curve_structure_class(structure),
                structure_label=curve_structure_label(structure),
                slope_pct=_as_float(data["slope_pct"]),
                front_price=_as_float(data["front_price"]),
                back_price=_as_float(data["back_price"]),
                spread=_as_float(data["spread"]),
                date=data["date"],
            )
        )
    cards.sort(key=lambda c: c.symbol)
    return cards


def _as_float(value) -> Optional[float]:
    return None if value is None else float(value)
