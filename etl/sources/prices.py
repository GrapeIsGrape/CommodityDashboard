"""Daily-price ETL → prices (macro-context sub-panel: TLT / VTI / QQQ).

Ingests daily OHLCV **plus** total-return-adjusted close for the three
macro-context ETFs into the existing ``prices`` table on the natural key
``(symbol, date)``:

* **close** — the raw *tape* close (stable once printed; cross-checkable against
  a broker quote).
* **adj_close** — yfinance's dividend-/split-back-adjusted, dividend-reinvested
  **TOTAL-RETURN** close. This is the clean trend series the sub-panel renders
  (raw close would show TLT "falling" ~0.3%/mo purely from coupons — a lie). For
  a non-dividend bar (no separate adjusted value, or adjusted == raw) we set
  ``adj_close = close`` so a consumer can *always* read ``adj_close``.
* **open / high / low / volume** — raw OHLCV alongside.

Both values come from **ONE** yfinance call with ``auto_adjust=False`` (the
yfinance default is now ``True``, which silently returns *adjusted* in the
``Close`` column with no separate ``Adj Close`` — the trap this module avoids).

Append-only / idempotent: ``INSERT ... ON CONFLICT (symbol, date) DO UPDATE``
(constraint ``uq_prices_symbol_date`` from migration ``0002``) updates
``adj_close`` + OHLCV (+ ``source``) in place, so a re-run never duplicates.
``adj_close`` *legitimately* changes after every ex-dividend (that is the point
of the trailing re-fetch); raw ``close`` is expected stable — a material raw
change is logged as a real Yahoo correction.

Corporate-action lookback: the **first** run backfills ~5 years (config
``prices.defaults.backfill_days``) so the trend chart has context immediately;
**later** runs are incremental but start at ``max(stored date) - refetch_days``
(config ``prices.defaults.refetch_days``, ~400d), NOT just ``max(date)`` — so the
lookback re-touches recent rows and keeps the back-adjusted series internally
consistent after each distribution / split.

Honest NULL, not interpolation: an empty / NaN / ``<= 0`` bar (incl. a 0-volume
US-market-day stale fetch) stores **no row** — never forward-filled, never a
NULL-close placeholder (a flat line reads as "closed unchanged" = a lie).
Weekend/holiday → yfinance returns no bar → no row.

The price fetch sits behind a **swappable** ``_PROVIDER`` (CLAUDE.md §4): a
:class:`PriceProvider` Protocol returning ordered daily bars, swappable via
:func:`set_provider` (e.g. IBKR later) without touching the ETL. yfinance is
imported only inside :class:`YFinancePriceProvider`.

Daily granularity only (``interval='1d'``); intraday is out of scope.

Run manually: ``python -m etl.sources.prices``. No scheduler is wired yet
(CLAUDE.md §2); cadence is a later ticket — run evening ET (after close +
settlement, when Yahoo's final adjusted bar has settled).
"""

import datetime as dt
import logging
import math
from typing import Optional, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url, load_macro_context, load_prices_config

logger = logging.getLogger("etl.prices")

_SOURCE = "yfinance"
_DEFAULT_BACKFILL_DAYS = 1825  # ~5y; overridden by config prices.defaults.
_DEFAULT_REFETCH_DAYS = 400    # ~13mo trailing re-fetch; overridden by config.
# A re-fetched raw close differing from the stored value by more than this
# fraction is logged as a likely real Yahoo correction (not a silent overwrite).
_RAW_CLOSE_CHANGE_TOL = 0.001  # 0.1%

# A daily bar as returned by a provider:
#   (date, open, high, low, close, adj_close, volume)
DailyBar = tuple


_UPSERT_SQL = text(
    """
    INSERT INTO prices (
        symbol, date, open, high, low, close, adj_close, volume, source
    )
    VALUES (
        :symbol, :date, :open, :high, :low, :close, :adj_close, :volume, :source
    )
    ON CONFLICT ON CONSTRAINT uq_prices_symbol_date
    DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        adj_close = EXCLUDED.adj_close,
        volume = EXCLUDED.volume,
        source = EXCLUDED.source
    """
)


# --- Pure transforms (network-free, unit-tested) -------------------------

def _clean_price(value) -> Optional[float]:
    """A raw price field → a stored value, or None for a missing/NaN/<=0 field.
    Honest NULL: we never carry forward a prior value or substitute 0."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or out <= 0:
        return None
    return out


def _clean_volume(value) -> Optional[int]:
    """Volume → a non-negative int, or None. NaN/negative → None; 0 is kept (a
    legitimate-but-rare value) — a 0 on a real session is caught upstream by the
    close being absent, not by faking volume."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or out < 0:
        return None
    return int(out)


def build_row(bar: DailyBar) -> Optional[dict]:
    """Build one prices row from a raw daily bar, or None to skip the bar.

    A bar with no usable raw ``close`` (missing / NaN / <= 0) is **skipped**
    entirely — we never insert a NULL-close placeholder (a flat line reads as
    "closed unchanged"). When the bar has no separate adjusted close (or it is
    unusable), ``adj_close`` falls back to the raw ``close`` so a consumer can
    always read ``adj_close`` (uniform non-dividend convention).
    """
    bar_date, o, h, low, close, adj_close, volume = bar
    clean_close = _clean_price(close)
    if clean_close is None:
        return None
    clean_adj = _clean_price(adj_close)
    return {
        "symbol": None,  # filled by the caller
        "date": bar_date.isoformat(),
        "open": _clean_price(o),
        "high": _clean_price(h),
        "low": _clean_price(low),
        "close": clean_close,
        "adj_close": clean_adj if clean_adj is not None else clean_close,
        "volume": _clean_volume(volume),
        "source": _SOURCE,
    }


def build_rows(symbol: str, bars: list) -> list[dict]:
    """Build the ordered prices rows for one symbol, dropping unusable bars."""
    rows: list[dict] = []
    for bar in bars:
        row = build_row(bar)
        if row is None:
            continue
        row["symbol"] = symbol
        rows.append(row)
    return rows


# --- Swappable price provider (only place yfinance is imported) ----------

class PriceProvider(Protocol):
    """Daily OHLCV + adjusted-close history for a ticker. Swap the
    implementation (e.g. IBKR) via :func:`set_provider` without touching the ETL
    (CLAUDE.md §4). Returns ordered daily bars
    ``(date, open, high, low, close, adj_close, volume)``; any field may be
    None/NaN for a missing bar (cleaned downstream)."""

    def daily_bars(self, ticker: str, start: dt.date) -> list: ...


class YFinancePriceProvider:
    """yfinance-backed price provider — no API key (Phase 0 verdict).

    Fetches with ``auto_adjust=False`` so a single call returns BOTH the raw
    ``Close`` and the back-adjusted ``Adj Close`` (the yfinance default of
    ``True`` would drop ``Adj Close`` and put adjusted values in ``Close``).
    """

    def daily_bars(self, ticker: str, start: dt.date) -> list:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(
            start=start.isoformat(), interval="1d", auto_adjust=False
        )
        if hist is None or hist.empty or "Close" not in hist.columns:
            return []
        cols = hist.columns
        has_adj = "Adj Close" in cols
        bars: list = []
        for ts, row in hist.iterrows():
            adj = row["Adj Close"] if has_adj else None
            bars.append(
                (
                    ts.date(),
                    row.get("Open"),
                    row.get("High"),
                    row.get("Low"),
                    row.get("Close"),
                    adj,
                    row.get("Volume"),
                )
            )
        return bars


_PROVIDER: PriceProvider = YFinancePriceProvider()


def set_provider(provider: PriceProvider) -> None:
    """Swap the price provider (e.g. inject an IBKR or fake provider)."""
    global _PROVIDER
    _PROVIDER = provider


# --- DB + ETL ------------------------------------------------------------

def _max_date(engine: Engine, symbol: str) -> Optional[dt.date]:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT max(date) FROM prices WHERE symbol = :symbol"),
            {"symbol": symbol},
        ).scalar()


def start_date(
    latest: Optional[dt.date],
    backfill_days: int,
    refetch_days: int,
    today: dt.date,
) -> dt.date:
    """First run (no stored rows) → backfill from ``today - backfill_days``.
    Later runs → from ``max(stored date) - refetch_days`` (NOT just max(date)),
    so the trailing re-fetch re-touches recent rows and keeps the back-adjusted
    series internally consistent after each distribution / split."""
    if latest is None:
        return today - dt.timedelta(days=backfill_days)
    return latest - dt.timedelta(days=refetch_days)


def _stored_closes(engine: Engine, symbol: str, start: dt.date) -> dict:
    """{date: raw close} for the rows the re-fetch will touch, so a material raw
    change can be flagged as a real Yahoo correction (AC#15)."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT date, close FROM prices "
                "WHERE symbol = :symbol AND date >= :start AND close IS NOT NULL"
            ),
            {"symbol": symbol, "start": start.isoformat()},
        )
        return {row[0].isoformat(): float(row[1]) for row in result}


def _log_raw_close_changes(symbol: str, rows: list[dict], prior: dict) -> None:
    for row in rows:
        old = prior.get(row["date"])
        if old is None or old == 0:
            continue
        new = row["close"]
        if abs(new - old) / old > _RAW_CLOSE_CHANGE_TOL:
            logger.warning(
                "prices %s %s: raw close changed %.6g -> %.6g on re-fetch "
                "(likely a Yahoo correction); upserting.",
                symbol, row["date"], old, new,
            )


def ingest_symbol(
    engine: Engine,
    symbol: str,
    backfill_days: int,
    refetch_days: int,
    today: dt.date,
) -> int:
    """Fetch and upsert one symbol's daily bars. Returns the row count upserted."""
    latest = _max_date(engine, symbol)
    start = start_date(latest, backfill_days, refetch_days, today)
    bars = _PROVIDER.daily_bars(symbol, start)
    rows = build_rows(symbol, bars)

    prior = _stored_closes(engine, symbol, start) if latest is not None else {}
    _log_raw_close_changes(symbol, rows, prior)

    with engine.begin() as conn:
        for row in rows:
            conn.execute(_UPSERT_SQL, row)

    latest_row = rows[-1] if rows else None
    logger.info(
        "prices %s: upserted %d rows from %s; latest close=%s adj_close=%s",
        symbol, len(rows), start.isoformat(),
        latest_row["close"] if latest_row else None,
        latest_row["adj_close"] if latest_row else None,
    )
    return len(rows)


def _symbols() -> list[str]:
    return [entry["symbol"] for entry in load_macro_context()]


def _backfill_days(prices_config: dict) -> int:
    defaults = prices_config.get("defaults", {})
    return int(defaults.get("backfill_days", _DEFAULT_BACKFILL_DAYS))


def _refetch_days(prices_config: dict) -> int:
    defaults = prices_config.get("defaults", {})
    return int(defaults.get("refetch_days", _DEFAULT_REFETCH_DAYS))


def run() -> None:
    symbols = _symbols()
    prices_config = load_prices_config()
    backfill_days = _backfill_days(prices_config)
    refetch_days = _refetch_days(prices_config)
    today = dt.date.today()

    engine = create_engine(get_database_url())
    try:
        succeeded = 0
        for symbol in symbols:
            try:
                ingest_symbol(engine, symbol, backfill_days, refetch_days, today)
                succeeded += 1
            except Exception:
                logger.exception("prices %s failed; continuing with the rest.", symbol)
        logger.info("prices ETL complete: %d/%d symbols ingested.", succeeded, len(symbols))
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
