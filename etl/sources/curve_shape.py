"""Futures curve-shape ETL → curve_shape (Panel C — Positioning & Flow).

Takes a daily front-vs-ONE-deferred-contract basis snapshot per energy
underlying and upserts it into ``curve_shape`` on the natural key
``(symbol, date)``. For a premium seller this surfaces the term-structure
regime — **contango vs backwardation** — and its magnitude as an annualized
roll-yield, the third Panel C signal alongside COT positioning.

Per-row content, keyed by our future ``symbol`` (e.g. ``CL`` / ``NG``), never
the yfinance ticker:

* **front_price** — front-month close in USD (continuous front ticker, e.g.
  ``CL=F``).
* **back_price** — the configured deferred contract's close in USD, fetched via
  an explicit month-coded ticker (e.g. ``CLN26.NYM``) anchored ``months_out``
  delivery months past the **realized front delivery month** (not the calendar
  month — #12, so near a roll the leg and its scaling stay aligned); **NULL**
  when there is no clean deferred leg (missing / NaN / stale / holiday). Never
  carried forward, never ``0``.
* **spread** = ``back_price - front_price`` (signed, diagnostic); NULL when
  ``back_price`` is NULL.
* **slope_pct** = ``((back - front) / front) / (months_between / 12)`` — the
  annualized % carry, the headline decision number. Raw $ spread is meaningless
  without the price level and the months between the legs, so it is annualized.
* **structure** — regime flag derived from ``slope_pct`` with a non-zero
  deadband (below).
* **source** = ``'yfinance'``.

**Sign convention (pinned — ETL and dashboard must agree):** ``slope_pct > 0``
⇒ contango, ``slope_pct < 0`` ⇒ backwardation. ``structure`` is ``'contango'``
above ``+eps``, ``'backwardation'`` below ``-eps``, ``'flat'`` within the
deadband ``[-eps, +eps]`` (a small non-zero annualized %, so the regime doesn't
flip-flop day to day on noise), and **NULL** when ``slope_pct`` is NULL.

**Negative/zero front guard:** ``slope_pct`` divides by ``front_price``, so when
``front_price <= 0`` (the April-2020 WTI case) ``slope_pct`` and ``structure``
are NULL — never ``±inf``/NaN.

v1 is deliberately **front vs ONE deferred contract** — not a full multi-point
curve; the ``curve_shape`` schema matches this exactly. Daily snapshots accrue
history so the panel can compute a tightening/loosening trend later for free; no
trend column is added here.

The yfinance fetch sits behind a **swappable** :class:`CurveProvider` /
``_PROVIDER`` (CLAUDE.md §4) — the only place yfinance is imported — so IBKR can
later supply real multi-expiry curves without touching the ETL. Per-underlying
error isolation: a failing / empty leg or symbol is logged and skipped, never
fatal to the run.

Idempotent / append-only: ``INSERT ... ON CONFLICT (symbol, date) DO UPDATE`` so
a same-date re-run upserts in place, never duplicates. The snapshot is
forward-accruing (today's date); a deep historical backfill of deferred legs is
not required for v1.

**Scope / honest-NULL boundary.** ETF-roll proxies (USO/UNG vs spot) are
*rejected* as a curve source — they conflate fund fees + roll methodology with
the real basis. Base-metals curves (ALI/ZNC/NICKEL, LME) and deferred
grains/softs curves have no free term structure — flagged-not-faked, out of
scope. Only the five energy underlyings (CL, BZ, NG, RB, HO) ship in the config.

Run manually: ``python -m etl.sources.curve_shape``. No scheduler is wired yet
(CLAUDE.md §2); cadence is a later ticket.
"""

import datetime as dt
import logging
import math
from typing import Optional, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url, load_curve_config

logger = logging.getLogger("etl.curve_shape")

_SOURCE = "yfinance"
_DEFAULT_FLAT_EPS = 0.005  # annualized-slope deadband; overridden by config curve.defaults.
# yfinance month-coded futures: <ROOT><MONTHCODE><YY><SUFFIX>, e.g. CLN26.NYM.
# Codes F G H J K M N Q U V X Z map to Jan..Dec.
_MONTH_CODES = "FGHJKMNQUVXZ"

_UPSERT_SQL = text(
    """
    INSERT INTO curve_shape (
        symbol, date, front_price, back_price, spread, slope_pct, structure, source
    )
    VALUES (
        :symbol, :date, :front_price, :back_price, :spread, :slope_pct, :structure, :source
    )
    ON CONFLICT (symbol, date)
    DO UPDATE SET
        front_price = EXCLUDED.front_price,
        back_price = EXCLUDED.back_price,
        spread = EXCLUDED.spread,
        slope_pct = EXCLUDED.slope_pct,
        structure = EXCLUDED.structure,
        source = EXCLUDED.source
    """
)


# --- Pure transforms (network-free, unit-tested) -------------------------

def _clean_price(price) -> Optional[float]:
    """A raw close → a usable price, or None for missing/NaN/non-positive.

    Honest NULL: a missing/stale/holiday leg, or a non-positive print, becomes
    None rather than a forward-filled or ``0`` value. (front_price itself may be
    <= 0 — the April-2020 WTI case — and is guarded separately in :func:`slope`.)
    """
    if price is None:
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    return value


def realized_front_month(snapshot_date: dt.date, front_lead_months: int, roll_day: int) -> tuple[int, int]:
    """The active front contract's *realized delivery* ``(year, month)`` on
    ``snapshot_date`` — what the continuous ``=F`` ticker actually represents,
    which near a roll is **not** the calendar month (#12).

    Energy front-months deliver ``front_lead_months`` ahead of the calendar
    month (1 for CL/NG/RB/HO: a contract delivering month M trades through ~M-1).
    On or after ``roll_day`` the front contract has expired and the active front
    has rolled forward one further delivery month — so the late-June WTI front is
    August (June + lead 1 + roll 1), not July. Pure / deterministic: it reads
    only the passed snapshot date, never the host clock or timezone.

    ``front_lead_months=0`` and a ``roll_day`` past month-end reduce to the
    calendar month — the pre-#12 behaviour — so an in-month underlying is
    unchanged.
    """
    offset = front_lead_months + (1 if snapshot_date.day >= roll_day else 0)
    target = (snapshot_date.month - 1) + offset  # 0-based month index from Jan of snapshot year
    year = snapshot_date.year + target // 12
    month = target % 12 + 1
    return year, month


def deferred_month_code(front_month: int, months_out: int) -> tuple[str, int]:
    """The (yfinance month code, year-offset) for a contract ``months_out``
    delivery months past ``front_month`` (1=Jan..12=Dec).

    Energy futures (CL/BZ/NG/RB/HO) list every consecutive month, so the target
    is simply ``front_month + months_out`` rolled into the month-code alphabet,
    with the year advanced for each 12-month wrap. Returns the code letter and
    how many years past the front year the deferred contract falls in.
    """
    target = front_month + months_out  # 1-based, may exceed 12
    year_offset = (target - 1) // 12
    month = (target - 1) % 12 + 1
    return _MONTH_CODES[month - 1], year_offset


def build_deferred_ticker(
    deferred_root: str,
    suffix: str,
    today: dt.date,
    months_out: int,
    front_lead_months: int = 0,
    roll_day: int = 99,
) -> str:
    """Construct the yfinance month-coded deferred ticker for the contract
    ``months_out`` delivery months past the **realized front delivery month**
    (not the calendar month — #12).

    The front month is resolved via :func:`realized_front_month`, so in late
    June WTI (``front_lead_months=1``, ``roll_day=20``) the deferred is N months
    past **August**: ``CL`` + ``.NYM``, months_out=6 → ``CLG27.NYM`` (Feb 2027),
    not ``CLZ26.NYM``. The defaults (``front_lead_months=0``, ``roll_day=99``)
    reduce to the calendar month — the pre-#12 behaviour.

    Symbology is fragile, so the actual fetch is defensive and per-underlying
    error-isolated; an unrecognised/illiquid ticker just yields a NULL back leg.
    """
    front_year, front_month = realized_front_month(today, front_lead_months, roll_day)
    code, year_offset = deferred_month_code(front_month, months_out)
    year = front_year + year_offset
    yy = f"{year % 100:02d}"
    return f"{deferred_root}{code}{yy}{suffix}"


def slope(front: Optional[float], back: Optional[float], months_between: int) -> Optional[float]:
    """Annualized % carry: ``((back - front) / front) / (months_between / 12)``.

    ``months_between`` is the realized number of delivery months separating the
    two fetched legs (front delivery month → deferred delivery month). It is the
    same ``months_out`` the deferred ticker is offset by — both anchor to the
    realized front month so they can never disagree (#12).

    None when either leg is None, or when ``front <= 0`` (the negative/zero-front
    guard — never ``±inf``/NaN), or when ``months_between <= 0`` (misconfig).
    Sign: > 0 contango, < 0 backwardation.
    """
    if front is None or back is None:
        return None
    if front <= 0 or months_between <= 0:
        return None
    return ((back - front) / front) / (months_between / 12.0)


def classify(slope_pct: Optional[float], eps: float = _DEFAULT_FLAT_EPS) -> Optional[str]:
    """Term-structure regime from the annualized slope with a non-zero deadband:
    ``'contango'`` above ``+eps``, ``'backwardation'`` below ``-eps``, ``'flat'``
    within ``[-eps, +eps]``, and None when ``slope_pct`` is None."""
    if slope_pct is None:
        return None
    if slope_pct > eps:
        return "contango"
    if slope_pct < -eps:
        return "backwardation"
    return "flat"


def build_row(
    symbol: str,
    snapshot_date: dt.date,
    front_raw,
    back_raw,
    months_between: int,
    eps: float = _DEFAULT_FLAT_EPS,
) -> dict:
    """Assemble one curve_shape row from the two fetched closes. Pure given its
    inputs — the network lives in the provider. ``months_between`` is the
    realized delivery-month gap between the two legs (the same offset the
    deferred ticker is built with — both anchor to the realized front month,
    #12). A missing deferred leg yields ``front_price`` only with
    back_price/spread/slope_pct/structure NULL."""
    front = _clean_price(front_raw)
    back = _clean_price(back_raw)
    spread = (back - front) if (front is not None and back is not None) else None
    slope_pct = slope(front, back, months_between)
    return {
        "symbol": symbol,
        "date": snapshot_date.isoformat(),
        "front_price": front,
        "back_price": back,
        "spread": spread,
        "slope_pct": slope_pct,
        "structure": classify(slope_pct, eps),
        "source": _SOURCE,
    }


# --- Swappable curve provider (the only place yfinance is imported) ------

class CurveProvider(Protocol):
    """Front/deferred close provider for the curve snapshot. Swap the
    implementation (e.g. IBKR with real multi-expiry curves) via
    :func:`set_provider` without touching the ETL (CLAUDE.md §4)."""

    def latest_close(self, ticker: str) -> Optional[float]: ...


class YFinanceCurveProvider:
    """yfinance-backed provider — no API key, no auth (Phase 0 verdict).

    Fetches the most recent daily close for a (continuous front or month-coded
    deferred) futures ticker. Defensive: an empty/None frame or a fragile/
    unrecognised month-coded symbol returns None (→ a NULL leg), never raises
    through to abort the run."""

    def latest_close(self, ticker: str) -> Optional[float]:
        import yfinance as yf

        try:
            hist = yf.Ticker(ticker).history(period="5d", interval="1d")
        except Exception:
            logger.exception("curve: history fetch failed for %s", ticker)
            return None
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        closes = [c for c in hist["Close"].tolist() if c is not None and not math.isnan(c)]
        if not closes:
            return None
        return float(closes[-1])


_PROVIDER: CurveProvider = YFinanceCurveProvider()


def set_provider(provider: CurveProvider) -> None:
    """Swap the curve provider (e.g. inject an IBKR or fake provider)."""
    global _PROVIDER
    _PROVIDER = provider


def get_curve(front_ticker: str, deferred_ticker: str) -> tuple[Optional[float], Optional[float]]:
    """Swappable curve entrypoint (CLAUDE.md §4): the (front close, deferred
    close) pair for a front + deferred ticker. Either may be None (missing leg)."""
    return _PROVIDER.latest_close(front_ticker), _PROVIDER.latest_close(deferred_ticker)


# --- DB + ETL ------------------------------------------------------------

def _upsert(engine: Engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, row)


def ingest_underlying(engine: Engine, spec: dict, snapshot_date: dt.date, eps: float) -> dict:
    """Fetch front + deferred closes for one underlying and upsert the row."""
    deferred_ticker = build_deferred_ticker(
        spec["deferred_root"],
        spec["suffix"],
        snapshot_date,
        spec["months_out"],
        spec.get("front_lead_months", 0),
        spec.get("roll_day", 99),
    )
    front_raw, back_raw = get_curve(spec["front_ticker"], deferred_ticker)
    row = build_row(spec["symbol"], snapshot_date, front_raw, back_raw, spec["months_out"], eps)
    _upsert(engine, row)
    logger.info(
        "curve %s: front(%s)=%s back(%s)=%s slope_pct=%s structure=%s",
        spec["symbol"], spec["front_ticker"], row["front_price"],
        deferred_ticker, row["back_price"], row["slope_pct"], row["structure"],
    )
    return row


def _underlyings(curve_cfg: dict) -> list[dict]:
    return list(curve_cfg.get("underlyings", []))


def _flat_eps(curve_cfg: dict) -> float:
    return float(curve_cfg.get("defaults", {}).get("flat_eps", _DEFAULT_FLAT_EPS))


def run() -> None:
    curve_cfg = load_curve_config()
    specs = _underlyings(curve_cfg)
    eps = _flat_eps(curve_cfg)
    snapshot_date = dt.date.today()

    engine = create_engine(get_database_url())
    try:
        succeeded = 0
        for spec in specs:
            try:
                ingest_underlying(engine, spec, snapshot_date, eps)
                succeeded += 1
            except Exception:
                logger.exception(
                    "curve %s failed; continuing with the rest.", spec.get("symbol")
                )
        logger.info("curve-shape ETL complete: %d/%d underlyings snapshotted.", succeeded, len(specs))
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
