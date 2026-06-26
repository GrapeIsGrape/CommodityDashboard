"""Volatility-indices ETL → iv_metrics (Panel D, alongside per-underlying IV).

Ingests the published CBOE commodity volatility indices as their own rows in
``iv_metrics`` on the natural key ``(symbol, snapshot_date)``:

* **GVZ** — CBOE Gold ETF Volatility Index (gold / GLD implied-vol index).
* **OVX** — CBOE Crude Oil ETF Volatility Index (WTI / CL implied-vol index).

Each ingested entry stores the published index *level* in ``atm_iv``
(``source = 'yfinance'``), with ``rv_30`` / ``iv_rv_spread`` forced **NULL** —
these are vol indices, not an underlying with a price series we compute RV on.
``iv_rank`` / ``iv_percentile`` reuse #9's accrual math (:func:`iv._iv_rank` /
:func:`iv._iv_percentile`), computed against the index's own stored/backfilled
history.

**``^VIX`` is deliberately excluded** — VIX is sourced from FRED ``VIXCLS`` →
``macro_metrics`` (#3); re-pulling it here would duplicate it with worse lineage.
The exclusion is config-driven (``ingest: false`` in ``config/symbols.yaml``).

**Unlike #9's home-grown ATM IV** (Yahoo gives no IV history, so #9 accrues rank
forward over 20+ daily snapshots), GVZ/OVX have real Yahoo daily history. So on
the first run we **backfill** ~3 years (config-driven ``backfill_days``) via
``yfinance.Ticker(ticker).history()`` — one row per trading day, ``atm_iv`` =
that day's daily **close** — making ``iv_rank`` meaningful immediately. Later
runs are **incremental** from ``max(stored snapshot_date)`` forward, never
re-pulling the full window.

Honest NULL, not interpolation: a holiday / missing / NaN day stores
``atm_iv = NULL`` — never carried forward, never 0.

The index-level fetch sits behind a **swappable** ``_PROVIDER`` (CLAUDE.md §4) —
a distinct path from #9's option-chain ``get_iv()`` seam — so IBKR/another feed
can replace yfinance later without touching the ETL. yfinance is imported only
inside that provider.

Idempotent / append-only: reuses #9's ``INSERT ... ON CONFLICT (symbol,
snapshot_date) DO UPDATE``; both the one-shot backfill and the daily incremental
upsert flow through the same path.

Run manually: ``python -m etl.sources.vol_indices``. No scheduler is wired yet
(CLAUDE.md §2); cadence is a later ticket.
"""

import datetime as dt
import logging
import math
from typing import Optional, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url, load_symbols
from etl.sources.iv import (
    _RANK_WINDOW_DAYS,
    _SOURCE,
    _UPSERT_SQL,
    _iv_percentile,
    _iv_rank,
)

logger = logging.getLogger("etl.vol_indices")

_DEFAULT_BACKFILL_DAYS = 1095  # ~3y; overridden by config volatility_indices.defaults.


# --- Pure transforms (network-free, unit-tested) -------------------------

def _clean_level(level) -> Optional[float]:
    """A published index close → a stored decimal fraction, or None for a
    missing/NaN/<=0 bar.  Honest NULL: we never carry forward or substitute 0.

    CBOE vol indices quote in percentage-point units (e.g. GVZ=29.58 means
    29.58 % annualised vol).  We divide by 100 so ``atm_iv`` is a consistent
    decimal fraction across all rows in ``iv_metrics`` (same convention as the
    per-underlying option-chain IV stored by ``etl/sources/iv.py``).  The
    migration 0005_normalize_gvz_ovx rescaled all historical rows already
    stored in the old raw-level convention."""
    if level is None:
        return None
    try:
        value = float(level)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or value <= 0:
        return None
    return value / 100.0


def build_index_rows(symbol: str, closes_by_date: list[tuple]) -> list[dict]:
    """Build iv_metrics rows for one index from its ordered (date, close) bars.

    ``rv_30`` / ``iv_rv_spread`` are always NULL (it is a vol index, not an
    underlying with a price series). ``iv_rank`` / ``iv_percentile`` accrue
    against the prior non-null levels *within this series* (reusing #9's math),
    so a backfill makes the latest row's rank meaningful immediately. A
    missing/NaN bar stores ``atm_iv = NULL`` and is excluded from the rank
    history (so it neither fakes a level nor pollutes later ranks).

    The rank/percentile lookback is bounded to the same trailing window the
    per-underlying IV path uses (:data:`iv._RANK_WINDOW_DAYS`): only prior
    cleaned levels whose bar date is within that window of the current bar feed
    the rank, so GVZ/OVX rank means the same thing as #9's per-underlying rank.
    The 3-year backfill of stored rows is unchanged — only the lookback is
    bounded."""
    rows: list[dict] = []
    history: list[tuple[dt.date, float]] = []
    for bar_date, raw in closes_by_date:
        level = _clean_level(raw)
        rank = pct = None
        if level is not None:
            floor = bar_date - dt.timedelta(days=_RANK_WINDOW_DAYS)
            window = [lvl for d, lvl in history if d >= floor]
            rank = _iv_rank(window, level)
            pct = _iv_percentile(window, level)
        rows.append(
            {
                "symbol": symbol,
                "snapshot_date": bar_date.isoformat(),
                "atm_iv": level,
                "iv_rank": rank,
                "iv_percentile": pct,
                "rv_30": None,
                "iv_rv_spread": None,
                "source": _SOURCE,
            }
        )
        if level is not None:
            history.append((bar_date, level))
    return rows


# --- Swappable index-history provider (only place yfinance is imported) --

class IndexHistoryProvider(Protocol):
    """Daily-close history for a vol index. Swap the implementation (e.g.
    IBKR) without touching the ETL (CLAUDE.md §4). Returns ordered
    ``(date, close)`` bars; ``close`` may be None/NaN for a missing bar."""

    def daily_closes(self, ticker: str, start: dt.date) -> list[tuple]: ...


class YFinanceIndexProvider:
    """yfinance-backed index-history provider — no API key (Phase 0 verdict)."""

    def daily_closes(self, ticker: str, start: dt.date) -> list[tuple]:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(start=start.isoformat(), interval="1d")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return []
        bars: list[tuple] = []
        for ts, close in hist["Close"].items():
            bars.append((ts.date(), close))
        return bars


_PROVIDER: IndexHistoryProvider = YFinanceIndexProvider()


def set_provider(provider: IndexHistoryProvider) -> None:
    """Swap the index-history provider (e.g. inject an IBKR or fake provider)."""
    global _PROVIDER
    _PROVIDER = provider


# --- DB + ETL ------------------------------------------------------------

def _max_snapshot_date(engine: Engine, symbol: str) -> Optional[dt.date]:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT max(snapshot_date) FROM iv_metrics WHERE symbol = :symbol"),
            {"symbol": symbol},
        ).scalar()


def _start_date(engine: Engine, symbol: str, backfill_days: int, today: dt.date) -> dt.date:
    """First run (no stored rows) → backfill from ``today - backfill_days``.
    Later runs → from the latest stored snapshot_date (re-fetched so the most
    recent close is refreshed) — never the full window again."""
    latest = _max_snapshot_date(engine, symbol)
    if latest is None:
        return today - dt.timedelta(days=backfill_days)
    return latest


def ingest_index(engine: Engine, ticker: str, symbol: str,
                 backfill_days: int, today: dt.date) -> int:
    """Fetch and upsert one index's daily levels. Returns the row count upserted."""
    start = _start_date(engine, symbol, backfill_days, today)
    bars = _PROVIDER.daily_closes(ticker, start)
    rows = build_index_rows(symbol, bars)
    with engine.begin() as conn:
        for row in rows:
            conn.execute(_UPSERT_SQL, row)
    latest = rows[-1] if rows else None
    logger.info(
        "vol index %s (%s): upserted %d rows from %s; latest atm_iv=%s rank=%s",
        symbol, ticker, len(rows), start.isoformat(),
        latest["atm_iv"] if latest else None,
        latest["iv_rank"] if latest else None,
    )
    return len(rows)


def _ingest_entries(symbols: dict) -> list[tuple]:
    """(ticker, symbol) for each volatility index flagged ``ingest: true``
    (VIX is excluded — sourced from FRED VIXCLS → macro_metrics)."""
    section = symbols.get("volatility_indices", {})
    entries = []
    for entry in section.get("indices", []):
        if not entry.get("ingest"):
            continue
        entries.append((entry["ticker"], entry["symbol"]))
    return entries


def _backfill_days(symbols: dict) -> int:
    defaults = symbols.get("volatility_indices", {}).get("defaults", {})
    return int(defaults.get("backfill_days", _DEFAULT_BACKFILL_DAYS))


def run() -> None:
    symbols = load_symbols()
    entries = _ingest_entries(symbols)
    backfill_days = _backfill_days(symbols)
    today = dt.date.today()

    engine = create_engine(get_database_url())
    try:
        succeeded = 0
        for ticker, symbol in entries:
            try:
                ingest_index(engine, ticker, symbol, backfill_days, today)
                succeeded += 1
            except Exception:
                logger.exception("vol index %s failed; continuing with the rest.", symbol)
        logger.info("vol-indices ETL complete: %d/%d indices ingested.", succeeded, len(entries))
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
