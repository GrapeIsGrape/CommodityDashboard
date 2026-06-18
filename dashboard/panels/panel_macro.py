"""Macro-context sub-panel — read-only view model for the dashboard.

A compact cross-asset risk-regime strip over the macro-context trio (TLT / VTI /
QQQ) written to the ``prices`` table by ``etl/sources/prices.py`` (#20). This is
**context, NOT commodities, NOT where decisions live** (Panel D owns decisions;
Panel A owns the macro backdrop) — it is explicitly subordinate to Panel A, a
3-row strip + ONE composite regime line, never a second macro panel.

This module is **read-only** — SELECT only, never a write. It holds the
per-symbol latest-row + bounded-history queries and the *pure* presentation
logic — ~1m/~3m total-return % change off ``adj_close`` (the raw ``close`` is
shown only as a secondary "last / tape" broker-checkable level, never mixed into
the headline), the trailing-high drawdown fear-gauge, the single neutral
risk-on/risk-off composite (sign of equity ~1m vs long-bond ~1m, deadband-gated),
and NYSE-trading-day-aware staleness. The pure functions are network-free so they
unit-test without a live DB, mirroring ``panel_a.py``.

Deliberate constraints (from the Trader consult, folded into #21 ACs):

* Total-return headline off ``adj_close`` (raw ``close`` would show TLT "falling"
  purely from coupons); the raw level is a secondary tape check only and is never
  mixed into an adj-close-derived figure.
* TLT/VTI/QQQ overlap heavily with Panel A — so this strip is honest about its
  marginal value: a single risk-regime read + an equity drawdown fear-gauge,
  both purely *descriptive of the cross-asset tape* with **no commodity-action
  gloss** (unlike Panel A's USD/real-rate clauses) and **no option-action /
  rich-cheap / IV-rank / percentile language anywhere**.
* The ETL re-touches a trailing ~400-day ``adj_close`` window every run (it shifts
  after each ex-div), so returns/drawdown are recomputed at read time from the
  freshest stored rows — never cached/precomputed.

Reuses Panel A's pure helpers (``last_expected_session``/``is_trading_session``,
``nearest_prior``, ``pct_change``, ``direction_arrow``, ``format_pct_change``,
``format_date`` and the deadband pattern). Importing panel_a is a
dashboard-to-dashboard import — both ship in the dashboard image; the forbidden
coupling is dashboard → ``etl`` (#17), which this module does not do.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import load_macro_context
from dashboard.panels.panel_a import (
    direction_arrow,
    format_date,
    format_pct_change,
    is_trading_session,
    last_expected_session,
    nearest_prior,
    pct_change,
)

logger = logging.getLogger("dashboard.panel_macro")

# Symbol roles (config-driven names; these constants are only the *role* the
# regime composite reads — which instrument is the equity leg vs the long-bond
# leg). The names themselves come from config/symbols.yaml `macro_context`.
_EQUITY_SYMBOL = "VTI"
_TECH_EQUITY_SYMBOL = "QQQ"
_LONG_BOND_SYMBOL = "TLT"

# Per-symbol one-line subordination labels (#21 AC7). TLT must not read as a
# second rates signal; QQQ is a higher-beta/tech read whose gap to VTI is NOT a
# signal. Keyed by symbol; absent → no extra label.
_SYMBOL_NOTES: dict[str, str] = {
    "TLT": "duration proxy; see Panel A for the rate itself",
    "VTI": "broad US equity",
    "QQQ": "higher-beta / tech (QQQ ⊂ VTI) — the QQQ-vs-VTI gap is not a signal",
}

# Daily change windows in *trading days* (~21/session-month), mapped to a target
# calendar date; we then take the nearest stored row at/just-before it. Same
# convention as Panel A's daily path.
_ONE_MONTH_TRADING_DAYS = 21
_THREE_MONTH_TRADING_DAYS = 63

# Trailing-high drawdown window (calendar days). ~1 trading year. If the stored
# history is shallower than this (cold-start / fresh deploy) the actual window
# used is reported instead of implying a full 1y look-back (#21 AC8.ii).
_DRAWDOWN_WINDOW_DAYS = 365

# Regime composite deadband (mirrors Panel A's `_USD_TREND_DEADBAND`): a leg's
# ~1m total-return change must survive the headline's rounding (format_pct_change
# is 1-decimal percent → |fraction| < 0.0005 renders "+0.0%") before it counts as
# directional. A sub-rounding drift renders the neutral "~flat / mixed" regime,
# never a confident risk-on/off label.
_REGIME_DEADBAND = 0.0005

# Equity-in-drawdown threshold that feeds the "correlated de-risking" emphasis in
# the regime read — a meaningful pullback, not noise. Descriptive only.
_DRAWDOWN_MATERIAL = 0.05

# Regime vocabulary — descriptive of the cross-asset tape ONLY. No
# commodity-action gloss, no option-action language (asserted by the
# banned-phrase test over rendered output).
REGIME_RISK_ON = "Risk-on: equities firm, long bonds soft"
REGIME_RISK_OFF = "Risk-off: equities soft, bonds bid"
REGIME_DERISK = "Both equities and long bonds soft — correlated de-risking"
REGIME_FLAT = "~flat / mixed"
REGIME_UNKNOWN = "Insufficient data for a cross-asset regime read"


# --- Total-return change calcs (pure) -------------------------------------

def _sign_within_deadband(change: Optional[float]) -> int:
    """+1 / -1 for a move that survives the regime deadband, else 0 (flat or
    NULL). The deadband matches the headline rounding so a sub-rounding drift
    never reads as directional."""
    if change is None:
        return 0
    if change > _REGIME_DEADBAND:
        return 1
    if change < -_REGIME_DEADBAND:
        return -1
    return 0


def classify_regime(
    equity_1m: Optional[float],
    bond_1m: Optional[float],
    equity_in_drawdown: bool = False,
) -> str:
    """The single neutral cross-asset regime read from the sign of the equity
    (VTI) ~1m total return vs the long-bond (TLT) ~1m total return.

    Four descriptive cases, directional labels gated behind the deadband:

    * equity up, bonds down → risk-on (classic);
    * equity down, bonds up → risk-off (flight to duration);
    * equity down AND bonds down → correlated de-risking (the fattest
      commodity-vol-tail regime; the drawdown input reinforces this read);
    * anything flat/mixed (incl. a sub-rounding drift) → "~flat / mixed".

    Returns the unknown label only when a leg has no comparable prior at all
    (both NULL). Purely descriptive of the tape — no commodity-action gloss.
    """
    if equity_1m is None and bond_1m is None:
        return REGIME_UNKNOWN
    eq = _sign_within_deadband(equity_1m)
    bd = _sign_within_deadband(bond_1m)
    if eq < 0 and bd < 0:
        return REGIME_DERISK
    if eq > 0 and bd < 0:
        return REGIME_RISK_ON
    if eq < 0 and bd > 0:
        return REGIME_RISK_OFF
    # Equity soft but bonds flat still reads as de-risking when equity is in a
    # material drawdown — the fear-gauge input (AC8.iii) sharpens the call.
    if eq < 0 and equity_in_drawdown:
        return REGIME_DERISK
    return REGIME_FLAT


# --- Trailing-high drawdown (pure) ----------------------------------------

@dataclass
class Drawdown:
    """Total-return drawdown vs a trailing high, with the ACTUAL look-back
    window used (so a shallow/cold-start history never implies a full 1y look)."""
    pct: Optional[float]  # fractional, <= 0 (0 == at the high); None when unknown.
    window_days: Optional[int]  # span between the oldest and newest row used.
    obs: int  # number of non-NULL rows considered.


def trailing_drawdown(
    history: list[tuple[dt.date, Optional[float]]],
    latest: Optional[float],
) -> Drawdown:
    """Drawdown of the latest ``adj_close`` from its trailing-window high.

    ``history`` is (date, adj_close) newest-first, already bounded to the
    drawdown window by the query. Computed off ``adj_close`` (total return),
    never mixed with the raw close. NULL bars are skipped (never carried
    forward). Returns the actual window span + observation count so the render
    can show the real look-back rather than a misleading full 1y. A NULL latest
    or no usable history yields an unknown drawdown."""
    points = [(d, float(v)) for d, v in history if v is not None]
    if latest is None or not points:
        return Drawdown(pct=None, window_days=None, obs=len(points))
    high = max(v for _, v in points)
    oldest = min(d for d, _ in points)
    newest = max(d for d, _ in points)
    window_days = (newest - oldest).days
    if high <= 0:
        return Drawdown(pct=None, window_days=window_days, obs=len(points))
    pct = (latest - high) / high
    # Clamp tiny positive noise (latest is a new high) to 0 — never a positive
    # "drawdown".
    if pct > 0:
        pct = 0.0
    return Drawdown(pct=pct, window_days=window_days, obs=len(points))


# --- Formatting (CLAUDE.md conventions) -----------------------------------

def format_usd(value: Optional[float]) -> str:
    """A raw-close price level in USD with thousands separators and two
    decimals. NULL → em dash (distinct from a real 0.00)."""
    if value is None:
        return "—"
    return f"${value:,.2f}"


def format_drawdown(dd: Drawdown) -> str:
    """The drawdown as a signed percent with its honest window, e.g.
    "-7.2% (off ~1y high)" or, on a thin window, "-7.2% (off 142-day high)".
    Unknown → em dash."""
    if dd.pct is None:
        return "—"
    pct = f"{dd.pct * 100:+,.1f}%"
    if dd.window_days is not None and dd.window_days >= _DRAWDOWN_WINDOW_DAYS - 10:
        window = "off ~1y high"
    elif dd.window_days is not None:
        window = f"off {dd.window_days}-day high"
    else:
        window = "off trailing high"
    return f"{pct} ({window})"


# --- View-model rows ------------------------------------------------------

@dataclass
class MacroContextRow:
    symbol: str
    name: str
    note: str
    date: Optional[dt.date]
    # Raw tape close (secondary, broker-checkable) — never mixed into the
    # headline % which is adj_close-derived.
    close: Optional[float]
    adj_close: Optional[float]
    stale: bool

    # Pre-resolved display strings (template stays declarative).
    close_label: str = "—"
    one_month_label: str = "— (no prior)"
    one_month_arrow: str = ""
    three_month_label: str = "— (no prior)"
    three_month_arrow: str = ""
    drawdown_label: str = "—"
    # Raw ~1m total-return change carried for the regime composite (None when no
    # comparable prior); not rendered directly.
    one_month_change: Optional[float] = None
    # Whether this row's equity is in a material drawdown (feeds the regime read).
    in_drawdown: bool = False


@dataclass
class PanelMacroView:
    rows: list[MacroContextRow]
    last_session: dt.date
    regime: str = REGIME_UNKNOWN
    error: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.rows


# --- Read-only queries ----------------------------------------------------

# Latest stored bar per symbol (freshest real adj_close + raw close + date).
_LATEST_BY_SYMBOL_SQL = text(
    """
    SELECT DISTINCT ON (symbol)
        symbol, date, close, adj_close
    FROM prices
    WHERE symbol = ANY(:symbols)
    ORDER BY symbol, date DESC
    """
)

# Bounded history per symbol (newest-first) for the change windows + the
# trailing-high drawdown. The window covers the ~1y drawdown look-back with
# slack; the (symbol, date DESC) index from 0002 serves it.
_HISTORY_SQL = text(
    """
    SELECT symbol, date, adj_close
    FROM prices
    WHERE symbol = ANY(:symbols)
      AND date >= :since
    ORDER BY symbol, date DESC
    """
)


def build_view(engine: Engine, today: Optional[dt.date] = None) -> PanelMacroView:
    """Assemble the macro-context sub-panel view model with a single read-only
    pass over ``prices``. ``today`` is injectable so the change windows and
    NYSE daily staleness are testable without the wall clock."""
    today = today or dt.date.today()
    instruments = load_macro_context()
    symbols = [entry["symbol"] for entry in instruments]
    last_session = last_expected_session(today)
    # Reach back the drawdown window + slack so the ~3m change and the trailing
    # high are both in range.
    since = (today - dt.timedelta(days=_DRAWDOWN_WINDOW_DAYS + 35)).isoformat()

    latest: dict[str, dict] = {}
    history: dict[str, list[tuple[dt.date, Optional[float]]]] = {}
    if symbols:
        try:
            with engine.connect() as conn:
                for row in conn.execute(_LATEST_BY_SYMBOL_SQL, {"symbols": symbols}):
                    latest[row.symbol] = row._mapping
                for row in conn.execute(_HISTORY_SQL, {"symbols": symbols, "since": since}):
                    value = None if row.adj_close is None else float(row.adj_close)
                    history.setdefault(row.symbol, []).append((row.date, value))
        except (OperationalError, ProgrammingError):
            # DB unreachable (OperationalError) or a pre-migration DB without the
            # prices table (ProgrammingError): one failing condition must not 500
            # the dashboard (CLAUDE.md §4) — matters most on a stressed-equity
            # day. Render the honest error state; never log the DSN/credentials.
            logger.exception("Macro-context read failed; rendering data-unavailable state")
            return PanelMacroView(rows=[], last_session=last_session, error=True)

    rows = [_build_row(entry, latest, history, today) for entry in instruments]
    regime = _compose_regime(rows)
    return PanelMacroView(rows=rows, last_session=last_session, regime=regime)


def _build_row(
    entry: dict,
    latest: dict[str, dict],
    history: dict[str, list[tuple[dt.date, Optional[float]]]],
    today: dt.date,
) -> MacroContextRow:
    symbol = entry["symbol"]
    data = latest.get(symbol)
    date = data["date"] if data is not None else None
    close = None
    adj_close = None
    if data is not None:
        if data["close"] is not None:
            close = float(data["close"])
        if data["adj_close"] is not None:
            adj_close = float(data["adj_close"])

    hist = history.get(symbol, [])

    row = MacroContextRow(
        symbol=symbol,
        name=entry.get("name", symbol),
        note=_SYMBOL_NOTES.get(symbol, ""),
        date=date,
        close=close,
        adj_close=adj_close,
        stale=_is_stale(date, today),
        close_label=format_usd(close),
    )

    # ~1m / ~3m total-return change off adj_close via nearest-prior (Panel A
    # windows). Anchored on the latest bar's date.
    anchor = date if date is not None else (hist[0][0] if hist else None)
    if anchor is not None:
        one_m_target = anchor - dt.timedelta(days=_ONE_MONTH_TRADING_DAYS * 7 // 5)
        three_m_target = anchor - dt.timedelta(days=_THREE_MONTH_TRADING_DAYS * 7 // 5)
        one_prior = nearest_prior(hist, one_m_target)
        three_prior = nearest_prior(hist, three_m_target)
        one_pct = pct_change(adj_close, one_prior)
        three_pct = pct_change(adj_close, three_prior)
        row.one_month_change = one_pct
        row.one_month_label = format_pct_change(one_pct)
        row.one_month_arrow = direction_arrow(one_pct)
        row.three_month_label = format_pct_change(three_pct)
        row.three_month_arrow = direction_arrow(three_pct)

    # Trailing-high total-return drawdown (equities only — it is an equity
    # fear-gauge; a long-bond "drawdown" is just rate level and lives in Panel A).
    if symbol in (_EQUITY_SYMBOL, _TECH_EQUITY_SYMBOL):
        dd = trailing_drawdown(hist, adj_close)
        row.drawdown_label = format_drawdown(dd)
        row.in_drawdown = dd.pct is not None and dd.pct <= -_DRAWDOWN_MATERIAL

    return row


def _compose_regime(rows: list[MacroContextRow]) -> str:
    by_symbol = {row.symbol: row for row in rows}
    equity = by_symbol.get(_EQUITY_SYMBOL)
    bond = by_symbol.get(_LONG_BOND_SYMBOL)
    if equity is None or bond is None:
        return REGIME_UNKNOWN
    return classify_regime(
        equity.one_month_change,
        bond.one_month_change,
        equity_in_drawdown=equity.in_drawdown,
    )


# --- NYSE daily staleness (reuse Panel A's trading-session model) ----------

def _is_stale(date: Optional[dt.date], today: dt.date) -> bool:
    """TLT/VTI/QQQ are exchange-traded ETFs — NYSE-trading-day-aware staleness
    (NOT the FRED reference-period publication-lag model). STALE only if the
    latest stored bar predates the last expected trading session. A NULL date is
    never flagged (honest unknown, not stale)."""
    if date is None:
        return False
    return date < last_expected_session(today)


# Re-export so a caller/test can reuse the trading-session predicate without
# reaching into panel_a (it is Panel A's, shared here intentionally).
__all__ = [
    "build_view",
    "classify_regime",
    "trailing_drawdown",
    "format_usd",
    "format_drawdown",
    "format_pct_change",
    "format_date",
    "is_trading_session",
    "last_expected_session",
    "MacroContextRow",
    "PanelMacroView",
    "Drawdown",
]
