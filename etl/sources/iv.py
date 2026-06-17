"""Implied-volatility ETL → iv_metrics (Panel D, where the decisions live).

Takes a daily volatility snapshot per commodity underlying and upserts it into
``iv_metrics`` on the natural key ``(symbol, snapshot_date)``:

* **atm_iv** — at-the-money implied vol from the underlying's *optionable ETF
  proxy* option chain. Per the Phase 0 spike, IV comes via proxies (GLD/SLV/
  USO/UNG…), never futures symbols; the proxy map lives in
  ``config/symbols.yaml`` (`iv_proxy`, null where no liquid proxy exists — those
  underlyings are skipped). Only contracts with a live two-sided market
  (``bid > 0``) are trusted, so a run outside market hours — when Yahoo zeroes
  bid/ask and emits degenerate IV sentinels — records ``atm_iv = NULL`` rather
  than fake IV. Schedule the snapshot during/after the option session.
* **rv_30** — 30-day annualized realized vol from the proxy's price history.
* **iv_rv_spread** — ``atm_iv - rv_30`` (null if either is null).
* **iv_rank / iv_percentile** — accrued from *our own* stored ``atm_iv`` history
  (Yahoo gives no IV history, per Phase 0), over a trailing window; **null until
  enough snapshots exist** (``_MIN_HISTORY_OBS``).

Rows are keyed by our commodity *future* symbol (e.g. ``GC``) — Panel D is
per-underlying; the proxy is an implementation detail.

The vol source sits behind a **swappable** :func:`get_iv` interface and a
``_PROVIDER`` object (CLAUDE.md §4), so IBKR can replace yfinance later via
:func:`set_provider` without touching the ETL or dashboard. The yfinance
provider is the only place that imports yfinance.

Idempotent / append-only: ``INSERT ... ON CONFLICT (symbol, snapshot_date) DO
UPDATE`` so a same-day re-run upserts in place, never duplicates. The snapshot
is forward-accruing (today's date) — not backfillable, since Yahoo exposes no
IV history.

Run manually: ``python -m etl.sources.iv``. No scheduler is wired yet
(CLAUDE.md §2); cadence is a later ticket.
"""

import datetime as dt
import logging
import math
from typing import Optional, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url, load_symbols
from common.constants import _MIN_HISTORY_OBS

logger = logging.getLogger("etl.iv")

_SOURCE = "yfinance"
_RV_WINDOW = 30
_TRADING_DAYS = 252
_RANK_WINDOW_DAYS = 365  # ~one trading year of trailing snapshots for rank/pct.
# _MIN_HISTORY_OBS (= 20): below this, iv_rank / iv_percentile stay NULL. It is
# defined once in common.constants (imported above) and re-exported via that
# module-level binding so both this ETL writer and the Panel D reader — built as
# separate images — share a single source of truth.
_MIN_DTE = 7             # skip about-to-expire contracts; pick the first expiry >= this.
# Yahoo reports a degenerate near-zero IV (~1e-5) for zero-bid/stale contracts —
# common when the market is closed. No real ETF ATM option is below ~1% IV, so
# treat anything under this floor as unusable and store NULL rather than fake data.
_MIN_PLAUSIBLE_IV = 0.01
_ATM_STRIKES = 6         # consider only the strikes nearest spot when reading ATM IV.

_UPSERT_SQL = text(
    """
    INSERT INTO iv_metrics (
        symbol, snapshot_date, atm_iv, iv_rank, iv_percentile,
        rv_30, iv_rv_spread, source
    )
    VALUES (
        :symbol, :snapshot_date, :atm_iv, :iv_rank, :iv_percentile,
        :rv_30, :iv_rv_spread, :source
    )
    ON CONFLICT (symbol, snapshot_date)
    DO UPDATE SET
        atm_iv = EXCLUDED.atm_iv,
        iv_rank = EXCLUDED.iv_rank,
        iv_percentile = EXCLUDED.iv_percentile,
        rv_30 = EXCLUDED.rv_30,
        iv_rv_spread = EXCLUDED.iv_rv_spread,
        source = EXCLUDED.source
    """
)


# --- Pure vol math (network-free, unit-tested) ---------------------------

def _atm_iv(
    spot: Optional[float],
    strike_iv_pairs: list[tuple],
    min_iv: float = _MIN_PLAUSIBLE_IV,
    n_strikes: int = _ATM_STRIKES,
) -> Optional[float]:
    """ATM implied vol: among the ``n_strikes`` strikes nearest spot, the IV of
    the closest one with a usable reading. Restricting to the near-ATM strikes
    first means a degenerate ATM (Yahoo's zero-bid/stale sentinel, ~1e-5, common
    when the market is closed) yields None rather than silently reaching out to a
    far-OTM wing. Returns None when there is no spot or no usable near-ATM IV —
    an honest NULL beats a fake reading polluting the rank history."""
    if spot is None:
        return None
    nearest = sorted(strike_iv_pairs, key=lambda pair: abs(pair[0] - spot))[:n_strikes]
    usable = [
        iv
        for _, iv in nearest
        if iv is not None and not math.isnan(iv) and iv > min_iv
    ]
    return float(usable[0]) if usable else None


def _realized_vol(
    closes: list[float], window: int = _RV_WINDOW, trading_days: int = _TRADING_DAYS
) -> Optional[float]:
    """Annualized realized vol = stdev(daily log returns over `window`) *
    sqrt(trading_days). Needs window+1 closes for `window` returns; returns None
    if there isn't enough history."""
    prices = [c for c in closes if c is not None and c > 0]
    if len(prices) < window + 1:
        return None
    recent = prices[-(window + 1):]
    rets = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(variance) * math.sqrt(trading_days)


def _iv_rank(
    history: list[float], current: float, min_obs: int = _MIN_HISTORY_OBS
) -> Optional[float]:
    """IV rank over the trailing window incl. today: (current - min)/(max - min),
    in [0, 1]. None until `min_obs` observations exist, or when the window is
    flat (max == min)."""
    series = list(history) + [current]
    if len(series) < min_obs:
        return None
    lo, hi = min(series), max(series)
    if hi == lo:
        return None
    return (current - lo) / (hi - lo)


def _iv_percentile(
    history: list[float], current: float, min_obs: int = _MIN_HISTORY_OBS
) -> Optional[float]:
    """IV percentile over the trailing window incl. today: fraction of
    observations <= current, in [0, 1]. None until `min_obs` observations
    exist."""
    series = list(history) + [current]
    if len(series) < min_obs:
        return None
    at_or_below = sum(1 for v in series if v <= current)
    return at_or_below / len(series)


def _iv_rv_spread(atm_iv: Optional[float], rv_30: Optional[float]) -> Optional[float]:
    if atm_iv is None or rv_30 is None:
        return None
    return atm_iv - rv_30


# --- Swappable vol provider (the only place yfinance is imported) --------

class IVProvider(Protocol):
    """Market-data provider for the vol snapshot. Swap the implementation
    (e.g. IBKR) via :func:`set_provider` without touching the ETL."""

    def atm_iv(self, ticker: str) -> Optional[float]: ...
    def daily_closes(self, ticker: str, lookback_days: int) -> list[float]: ...


class YFinanceProvider:
    """yfinance-backed provider — no API key, no auth (Phase 0 verdict)."""

    def _ticker(self, ticker: str):
        import yfinance as yf
        return yf.Ticker(ticker)

    def _spot(self, tkr) -> Optional[float]:
        try:
            return float(tkr.fast_info["last_price"])
        except Exception:
            try:
                return float(tkr.history(period="1d")["Close"].iloc[-1])
            except Exception:
                return None

    def _pick_expiry(self, expirations) -> Optional[str]:
        """First expiry at least _MIN_DTE days out (avoids the about-to-expire
        contracts whose IV is noisy); falls back to the furthest available."""
        today = dt.date.today()
        for exp in expirations:
            try:
                if (dt.date.fromisoformat(exp) - today).days >= _MIN_DTE:
                    return exp
            except ValueError:
                continue
        return expirations[-1] if expirations else None

    def atm_iv(self, ticker: str) -> Optional[float]:
        tkr = self._ticker(ticker)
        expirations = tkr.options
        if not expirations:
            return None
        exp = self._pick_expiry(expirations)
        calls = tkr.option_chain(exp).calls
        if "impliedVolatility" not in calls.columns:
            return None
        # Only trust contracts with a live two-sided market (bid > 0). When the
        # market is closed Yahoo zeroes bid/ask and emits degenerate IV
        # sentinels; dropping zero-bid rows makes that resolve to NULL, not fake
        # IV. The pure _atm_iv floor is a secondary guard.
        strikes = calls["strike"].tolist()
        ivs = calls["impliedVolatility"].tolist()
        bids = calls["bid"].tolist() if "bid" in calls.columns else [None] * len(strikes)
        pairs = [
            (s, iv)
            for s, iv, b in zip(strikes, ivs, bids)
            if b is not None and not math.isnan(b) and b > 0
        ]
        return _atm_iv(self._spot(tkr), pairs)

    def daily_closes(self, ticker: str, lookback_days: int) -> list[float]:
        # Fetch a generous window so >= _RV_WINDOW+1 trading days are available.
        period = f"{max(lookback_days, _RV_WINDOW * 3)}d"
        hist = self._ticker(ticker).history(period=period)
        if hist is None or hist.empty:
            return []
        return [float(c) for c in hist["Close"].tolist()]


_PROVIDER: IVProvider = YFinanceProvider()


def set_provider(provider: IVProvider) -> None:
    """Swap the vol provider (e.g. inject an IBKR or fake provider)."""
    global _PROVIDER
    _PROVIDER = provider


def get_iv(ticker: str) -> Optional[float]:
    """Swappable IV entrypoint (CLAUDE.md §4): ATM implied vol for a market-data
    ticker (the optionable proxy for yfinance; could be a futures symbol for a
    future IBKR provider)."""
    return _PROVIDER.atm_iv(ticker)


# --- DB + ETL ------------------------------------------------------------

def _history_ivs(engine: Engine, symbol: str, snapshot_date: dt.date) -> list[float]:
    """Prior non-null atm_iv values for `symbol` within the trailing rank window,
    excluding the snapshot date itself (so a same-day re-run isn't double-counted)."""
    floor = snapshot_date - dt.timedelta(days=_RANK_WINDOW_DAYS)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT atm_iv FROM iv_metrics "
                "WHERE symbol = :symbol AND atm_iv IS NOT NULL "
                "AND snapshot_date >= :floor AND snapshot_date < :today "
                "ORDER BY snapshot_date"
            ),
            {"symbol": symbol, "floor": floor, "today": snapshot_date},
        )
        return [float(row[0]) for row in result]


def build_row(symbol: str, snapshot_date: dt.date, atm_iv: Optional[float],
              closes: list[float], history_ivs: list[float]) -> dict:
    """Assemble one iv_metrics row from the fetched snapshot + prior IV history.
    Pure given its inputs — the network lives in the provider."""
    rv_30 = _realized_vol(closes)
    rank = pct = None
    if atm_iv is not None:
        rank = _iv_rank(history_ivs, atm_iv)
        pct = _iv_percentile(history_ivs, atm_iv)
    return {
        "symbol": symbol,
        "snapshot_date": snapshot_date.isoformat(),
        "atm_iv": atm_iv,
        "iv_rank": rank,
        "iv_percentile": pct,
        "rv_30": rv_30,
        "iv_rv_spread": _iv_rv_spread(atm_iv, rv_30),
        "source": _SOURCE,
    }


def _upsert(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, row)


def ingest_symbol(engine: Engine, symbol: str, proxy: str, snapshot_date: dt.date) -> dict:
    """Fetch the vol snapshot for one underlying (via its proxy) and upsert it."""
    atm_iv = get_iv(proxy)
    closes = _PROVIDER.daily_closes(proxy, _RV_WINDOW * 3)
    history_ivs = _history_ivs(engine, symbol, snapshot_date)
    row = build_row(symbol, snapshot_date, atm_iv, closes, history_ivs)
    _upsert(engine, row)
    logger.info(
        "IV %s (proxy %s): atm_iv=%s rv_30=%s rank=%s",
        symbol, proxy, row["atm_iv"], row["rv_30"], row["iv_rank"],
    )
    return row


def _proxy_pairs(symbols: dict) -> list[tuple]:
    """(future_symbol, iv_proxy) for every commodity with a non-null proxy."""
    pairs = []
    for group in symbols.get("commodities", {}).values():
        for entry in group:
            proxy = entry.get("iv_proxy")
            if proxy:
                pairs.append((entry["future"], proxy))
            else:
                logger.debug("IV: %s has no iv_proxy; skipping.", entry.get("future"))
    return pairs


def run() -> None:
    symbols = load_symbols()
    pairs = _proxy_pairs(symbols)
    snapshot_date = dt.date.today()

    engine = create_engine(get_database_url())
    try:
        succeeded = 0
        for symbol, proxy in pairs:
            try:
                ingest_symbol(engine, symbol, proxy, snapshot_date)
                succeeded += 1
            except Exception:
                logger.exception("IV %s failed; continuing with the rest.", symbol)
        logger.info("IV ETL complete: %d/%d underlyings snapshotted.", succeeded, len(pairs))
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
