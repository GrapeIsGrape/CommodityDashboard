"""Panel D (Volatility) — read-only view model for the FastAPI dashboard.

Panel D is "where the decisions live": it surfaces, per commodity underlying,
the latest stored ATM-IV snapshot from ``iv_metrics`` (written by
``etl/sources/iv.py`` via the optionable-ETF proxy) plus the GVZ/OVX vol-index
context strip (written by ``etl/sources/vol_indices.py``).

This module is **read-only** — SELECT only, never a write. It holds:

* the per-underlying / index latest-snapshot queries (``DISTINCT ON``),
* the prior-snapshot count used for the cold-start ``N/20`` accruing state,
* and the pure presentation logic (highlight classification, last-expected-
  session staleness, NULL-meaning labelling, formatting) — pulled out as
  network-free functions so they unit-test without a live DB, mirroring the
  pure-math style of ``etl/sources/iv.py``.

The ``20`` accrual threshold is **not** redefined here — it is imported from
``common.constants._MIN_HISTORY_OBS`` (the single shared definition consumed by
both the ETL writer and this reader) so the UI label stays in lockstep with the
ETL that populates ``iv_rank`` / ``iv_percentile``. The dashboard image does not
ship the ``etl`` package, so this constant must come from ``common/``.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import load_symbols
from common.constants import _MIN_HISTORY_OBS

logger = logging.getLogger("dashboard.panel_d")

# US market holidays are the federal-ish NYSE closures; a fixed table keeps the
# last-expected-session calc deterministic and host-clock-injectable for tests
# rather than depending on a holiday library. Covers the in-scope window
# (current + recent years); extend as needed — an unknown future holiday only
# risks a single false "stale" badge, never bad data.
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

# Highlight classes — the template maps these to row treatments. Conjunctive by
# design (see classify_highlight): iv_rank alone never trips "sell candidate".
HIGHLIGHT_SELL = "sell-candidate"
HIGHLIGHT_RICH_RV = "rich-rv-catching-up"
HIGHLIGHT_NONE = "default"

_RICH_RANK_THRESHOLD = 0.70  # iv_rank is stored as a [0,1] fraction.


# --- Last expected trading session (pure, clock-injectable) --------------

def is_trading_session(day: dt.date) -> bool:
    """True if ``day`` is a weekday that is not a US market holiday."""
    return day.weekday() < 5 and day not in _US_MARKET_HOLIDAYS


def last_expected_session(today: dt.date) -> dt.date:
    """The last expected US trading session that should already have data —
    the most recent trading session **strictly before** ``today``.

    Weekend- and holiday-aware, and deliberately *before* today rather than
    on-or-after: a snapshot is considered current as long as it is no older than
    the prior session, so a Friday snapshot read on Monday is fresh (no session
    closed in between) and an intraday read on a session day does not flag that
    same day's not-yet-run snapshot. Measuring against expected sessions — not
    literal calendar days — is what keeps weekends/holidays from false-flagging.
    """
    day = today - dt.timedelta(days=1)
    while not is_trading_session(day):
        day -= dt.timedelta(days=1)
    return day


def is_stale(snapshot_date: Optional[dt.date], today: dt.date) -> bool:
    """A row is stale when its snapshot predates the last expected session
    (the prior trading session); a NULL date is never flagged stale."""
    if snapshot_date is None:
        return False
    return snapshot_date < last_expected_session(today)


# --- Rich / sell-candidate highlight (CONJUNCTIVE) -----------------------

def classify_highlight(
    iv_rank: Optional[float], iv_rv_spread: Optional[float]
) -> str:
    """Classify a row's highlight state.

    * ``HIGHLIGHT_SELL`` — ``iv_rank >= 0.70 AND iv_rv_spread > 0`` (rich vol the
      realized vol does *not* yet justify: a sell candidate).
    * ``HIGHLIGHT_RICH_RV`` — ``iv_rank >= 0.70 AND iv_rv_spread <= 0`` (rich,
      but RV is catching up — high for a reason; do not get walked into it).
    * ``HIGHLIGHT_NONE`` — anything else, including a NULL rank or NULL spread.

    Conjunctive on purpose: ``iv_rank`` alone never fires the sell highlight.
    """
    if iv_rank is None or iv_rank < _RICH_RANK_THRESHOLD:
        return HIGHLIGHT_NONE
    if iv_rv_spread is None:
        return HIGHLIGHT_NONE
    if iv_rv_spread > 0:
        return HIGHLIGHT_SELL
    return HIGHLIGHT_RICH_RV


# --- NULL-meaning + cold-start labelling ---------------------------------

def rank_display(
    iv_rank: Optional[float],
    atm_iv: Optional[float],
    snapshot_count: int,
    min_obs: int = _MIN_HISTORY_OBS,
) -> str:
    """How to render ``iv_rank`` / ``iv_percentile`` given the cold-start vs
    off-hours distinction (same NULL, opposite meaning).

    * A real value → the formatted percentage.
    * NULL because **still accruing** (fewer than ``min_obs`` stored non-null
      ``atm_iv`` snapshots) → ``"— (N/min_obs)"``.
    * NULL because **off-hours / bad chain today** (today's ``atm_iv`` itself is
      NULL but enough history exists) → ``"— (no chain)"``.
    * NULL with a flat window despite enough history → ``"—"``.
    """
    if iv_rank is not None:
        return format_pct(iv_rank)
    if snapshot_count < min_obs:
        return f"— ({snapshot_count}/{min_obs})"
    if atm_iv is None:
        return "— (no chain)"
    return "—"


# --- Formatting (CLAUDE.md conventions) ----------------------------------

def format_pct(value: Optional[float]) -> str:
    """A [0,1]-ish fraction → a percentage with one decimal, or an em dash for
    NULL. ETL stores IV/RV/rank as decimals (0.30 == 30%)."""
    if value is None:
        return "—"
    return f"{value * 100:,.1f}%"


def format_date(value: Optional[dt.date]) -> str:
    if value is None:
        return "—"
    return value.isoformat()


# --- View-model rows ------------------------------------------------------

@dataclass
class UnderlyingRow:
    symbol: str
    iv_proxy: str
    atm_iv: Optional[float]
    iv_rank: Optional[float]
    iv_percentile: Optional[float]
    rv_30: Optional[float]
    iv_rv_spread: Optional[float]
    snapshot_date: Optional[dt.date]
    snapshot_count: int
    highlight: str
    stale: bool
    rank_label: str
    percentile_label: str


@dataclass
class IndexRow:
    symbol: str
    name: str
    atm_iv: Optional[float]
    iv_rank: Optional[float]
    iv_percentile: Optional[float]
    snapshot_date: Optional[dt.date]
    stale: bool


@dataclass
class PanelDView:
    underlyings: list[UnderlyingRow]
    indices: list[IndexRow]
    last_session: dt.date
    error: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.underlyings and not self.indices


# --- Read-only queries ----------------------------------------------------

# Latest snapshot per symbol (the (symbol, snapshot_date DESC) index from 0002
# supports the DISTINCT ON). Filtered to the supplied symbol list so index rows
# (GVZ/OVX) and any stray symbols never leak into the per-underlying table.
_LATEST_BY_SYMBOL_SQL = text(
    """
    SELECT DISTINCT ON (symbol)
        symbol, snapshot_date, atm_iv, iv_rank, iv_percentile,
        rv_30, iv_rv_spread
    FROM iv_metrics
    WHERE symbol = ANY(:symbols)
    ORDER BY symbol, snapshot_date DESC
    """
)

# Count of stored non-null atm_iv snapshots per symbol — mirrors what iv.py
# accrues rank from, for the cold-start N/20 label.
_SNAPSHOT_COUNTS_SQL = text(
    """
    SELECT symbol, COUNT(*) AS n
    FROM iv_metrics
    WHERE symbol = ANY(:symbols) AND atm_iv IS NOT NULL
    GROUP BY symbol
    """
)


def _proxy_map(symbols: dict) -> dict[str, str]:
    """future symbol → iv_proxy, only for underlyings with a non-null proxy
    (null-proxy underlyings are skipped by the IV ETL and must not fabricate a
    Panel D row)."""
    out: dict[str, str] = {}
    for group in symbols.get("commodities", {}).values():
        for entry in group:
            proxy = entry.get("iv_proxy")
            if proxy:
                out[entry["future"]] = proxy
    return out


def _index_map(symbols: dict) -> dict[str, str]:
    """Ingested vol-index symbol → display name (GVZ/OVX; VIX excluded via
    ``ingest: false``)."""
    out: dict[str, str] = {}
    section = symbols.get("volatility_indices", {})
    for entry in section.get("indices", []):
        if entry.get("ingest"):
            out[entry["symbol"]] = entry.get("name", entry["symbol"])
    return out


def _as_float(value) -> Optional[float]:
    return None if value is None else float(value)


def build_view(engine: Engine, today: Optional[dt.date] = None) -> PanelDView:
    """Assemble the Panel D view model with a single read-only pass.

    ``today`` is injectable so staleness is testable without the wall clock.
    """
    today = today or dt.date.today()
    symbols = load_symbols()
    proxy_map = _proxy_map(symbols)
    index_map = _index_map(symbols)
    last_session = last_expected_session(today)

    underlying_symbols = list(proxy_map.keys())
    index_symbols = list(index_map.keys())
    all_symbols = underlying_symbols + index_symbols

    latest: dict[str, dict] = {}
    counts: dict[str, int] = {}
    if all_symbols:
        try:
            with engine.connect() as conn:
                for row in conn.execute(_LATEST_BY_SYMBOL_SQL, {"symbols": all_symbols}):
                    latest[row.symbol] = row._mapping
                for row in conn.execute(_SNAPSHOT_COUNTS_SQL, {"symbols": all_symbols}):
                    counts[row.symbol] = int(row.n)
        except (OperationalError, ProgrammingError):
            # DB unreachable (OperationalError) or pre-migration DB without the
            # iv_metrics table (ProgrammingError): one failing condition must not
            # 500 the dashboard (CLAUDE.md §4). Render the honest error state.
            # Static message — never log the DSN/credentials (mirrors /health).
            logger.exception("Panel D read failed; rendering data-unavailable state")
            return PanelDView(
                underlyings=[], indices=[], last_session=last_session, error=True
            )

    underlyings = _build_underlyings(proxy_map, latest, counts, today)
    indices = _build_indices(index_map, latest, today)
    return PanelDView(underlyings=underlyings, indices=indices, last_session=last_session)


def _build_underlyings(
    proxy_map: dict[str, str],
    latest: dict[str, dict],
    counts: dict[str, int],
    today: dt.date,
) -> list[UnderlyingRow]:
    rows: list[UnderlyingRow] = []
    for symbol, proxy in proxy_map.items():
        data = latest.get(symbol)
        if data is None:
            continue  # no snapshot yet for this underlying — do not fabricate a row.
        atm_iv = _as_float(data["atm_iv"])
        iv_rank = _as_float(data["iv_rank"])
        iv_pct = _as_float(data["iv_percentile"])
        spread = _as_float(data["iv_rv_spread"])
        snap = data["snapshot_date"]
        n = counts.get(symbol, 0)
        rows.append(
            UnderlyingRow(
                symbol=symbol,
                iv_proxy=proxy,
                atm_iv=atm_iv,
                iv_rank=iv_rank,
                iv_percentile=iv_pct,
                rv_30=_as_float(data["rv_30"]),
                iv_rv_spread=spread,
                snapshot_date=snap,
                snapshot_count=n,
                highlight=classify_highlight(iv_rank, spread),
                stale=is_stale(snap, today),
                rank_label=rank_display(iv_rank, atm_iv, n),
                percentile_label=rank_display(iv_pct, atm_iv, n),
            )
        )
    rows.sort(key=_rank_sort_key, reverse=True)
    return rows


def _rank_sort_key(row: UnderlyingRow):
    """Sort key for iv_rank DESC NULLS LAST: NULL ranks sink below any real
    rank regardless of direction."""
    return (row.iv_rank is not None, row.iv_rank if row.iv_rank is not None else 0.0)


def _build_indices(
    index_map: dict[str, str], latest: dict[str, dict], today: dt.date
) -> list[IndexRow]:
    rows: list[IndexRow] = []
    for symbol, name in index_map.items():
        data = latest.get(symbol)
        if data is None:
            continue
        snap = data["snapshot_date"]
        rows.append(
            IndexRow(
                symbol=symbol,
                name=name,
                atm_iv=_as_float(data["atm_iv"]),
                iv_rank=_as_float(data["iv_rank"]),
                iv_percentile=_as_float(data["iv_percentile"]),
                snapshot_date=snap,
                stale=is_stale(snap, today),
            )
        )
    rows.sort(key=lambda r: r.symbol)
    return rows
