"""CFTC Commitments-of-Traders ETL → cot (Panel C, positioning).

Pulls the weekly CFTC **Legacy futures-only** Commitments-of-Traders report from
the free CFTC Socrata Open Data API and upserts each market's weekly row into
``cot`` on the natural key ``(symbol, report_date)``.

Fourth Phase 2 ETL source — follows the reusable pattern established by FRED,
EIA, and USDA:

* **Config-driven** — the symbol → CFTC ``cftc_contract_market_code`` map lives
  in ``config/cftc_markets.yaml``, never here.
* **Idempotent / append-only** — ``INSERT ... ON CONFLICT (symbol, report_date)
  DO UPDATE`` so a same-week re-run upserts in place, never duplicates. CFTC
  restatements overwrite the row for that date (the desired behaviour).
* **Incremental + revision lookback** — each market refetches from
  ``max(stored report_date) - revision_lookback_days`` (falling back to the
  configured ``observation_start`` when the market has no rows yet), so the
  first run backfills history and later runs are cheap while catching
  restatements.
* **Per-source isolation** — a failure on one market is caught, logged with the
  offending symbol, and never aborts the remaining markets or the container.

Socrata is open (no API key). An optional ``CFTC_APP_TOKEN`` (env only) is sent
as the ``X-App-Token`` header when set — it only raises rate limits. The API
paginates with ``$limit``/``$offset``; :func:`_fetch_rows` follows the pages so
long backfills aren't silently truncated.

Run manually: ``python -m etl.sources.cftc``. No scheduler is wired yet
(CLAUDE.md §2); cadence is a later ticket.
"""

import datetime as dt
import logging
import os
from typing import Optional

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url, load_cftc_markets

logger = logging.getLogger("etl.cftc")

_SOCRATA_BASE = "https://publicreporting.cftc.gov/resource/"
_SOURCE = "CFTC"
_PAGE_LENGTH = 5000
_REQUEST_TIMEOUT = 60

# cot column -> Socrata Legacy futures-only field.
_FIELD_MAP = {
    "noncomm_long": "noncomm_positions_long_all",
    "noncomm_short": "noncomm_positions_short_all",
    "comm_long": "comm_positions_long_all",
    "comm_short": "comm_positions_short_all",
    "open_interest": "open_interest_all",
}

_UPSERT_SQL = text(
    """
    INSERT INTO cot (
        symbol, report_date, noncomm_long, noncomm_short,
        comm_long, comm_short, open_interest, source
    )
    VALUES (
        :symbol, :report_date, :noncomm_long, :noncomm_short,
        :comm_long, :comm_short, :open_interest, :source
    )
    ON CONFLICT (symbol, report_date)
    DO UPDATE SET
        noncomm_long = EXCLUDED.noncomm_long,
        noncomm_short = EXCLUDED.noncomm_short,
        comm_long = EXCLUDED.comm_long,
        comm_short = EXCLUDED.comm_short,
        open_interest = EXCLUDED.open_interest,
        source = EXCLUDED.source
    """
)


def _app_token_headers() -> dict:
    """Optional Socrata app token (env only) — raises rate limits, not required."""
    token = os.environ.get("CFTC_APP_TOKEN", "").strip()
    return {"X-App-Token": token} if token else {}


def _to_int(raw: Optional[str]) -> Optional[int]:
    """Parse a Socrata numeric string to int. Blank/missing → NULL, never 0."""
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if cleaned == "":
        return None
    return int(float(cleaned))


def _latest_stored_date(engine: Engine, symbol: str) -> Optional[dt.date]:
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT max(report_date) FROM cot WHERE symbol = :symbol"),
            {"symbol": symbol},
        )
        return result.scalar()


def _start_date(
    engine: Engine,
    symbol: str,
    observation_start: str,
    revision_lookback_days: int,
) -> str:
    """First run: backfill from observation_start. Later runs: refetch from a
    lookback window before the latest stored report_date so revisions overwrite."""
    latest = _latest_stored_date(engine, symbol)
    if latest is None:
        return observation_start
    incremental = latest - dt.timedelta(days=revision_lookback_days)
    return incremental.isoformat()


def _fetch_rows(dataset: str, code: str, start_date: str, headers: dict) -> list[dict]:
    """Page through the Socrata dataset for one contract-market code from
    ``start_date`` onward, following $limit/$offset so long backfills aren't
    truncated. No secret is in the URL — Socrata needs no key and the optional
    token travels in the header."""
    # Defensive guard: codes come from our committed config and are always
    # 6-char alphanumeric. Validate before embedding in the SoQL $where so a
    # malformed code fails loudly here (and is contained by per-market
    # isolation) instead of producing a broken/injected filter.
    if not code.isalnum():
        raise ValueError(f"CFTC contract market code is not alphanumeric: {code!r}")
    url = f"{_SOCRATA_BASE}{dataset}.json"
    rows: list[dict] = []
    offset = 0
    while True:
        params = {
            "$where": (
                f"cftc_contract_market_code='{code}' "
                f"and report_date_as_yyyy_mm_dd >= '{start_date}'"
            ),
            "$order": "report_date_as_yyyy_mm_dd asc",
            "$limit": _PAGE_LENGTH,
            "$offset": offset,
        }
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"CFTC request for {code} failed: {exc}") from None
        page = response.json()
        rows.extend(page)
        if len(page) < _PAGE_LENGTH:
            break
        offset += _PAGE_LENGTH
    return rows


def _to_rows(symbol: str, records: list[dict]) -> list[dict]:
    """Map Socrata Legacy futures-only records to cot upsert rows."""
    rows = []
    for record in records:
        report_date = record["report_date_as_yyyy_mm_dd"][:10]
        row = {
            "symbol": symbol,
            "report_date": report_date,
            "source": _SOURCE,
        }
        for column, field in _FIELD_MAP.items():
            row[column] = _to_int(record.get(field))
        rows.append(row)
    return rows


def _upsert(engine: Engine, rows: list[dict]) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, rows)


def ingest_market(engine: Engine, entry: dict, defaults: dict, headers: dict) -> int:
    """Fetch and upsert one market. Returns the number of weekly rows written."""
    symbol = entry["symbol"]
    start_date = _start_date(
        engine,
        symbol,
        entry.get("observation_start", defaults.get("observation_start", "2010-01-01")),
        int(defaults.get("revision_lookback_days", 30)),
    )
    records = _fetch_rows(defaults["dataset"], entry["code"], start_date, headers)
    rows = _to_rows(symbol, records)
    _upsert(engine, rows)
    logger.info("CFTC %s: upserted %d rows from %s", symbol, len(rows), start_date)
    return len(rows)


def run() -> None:
    config = load_cftc_markets()
    defaults = config.get("defaults", {})
    markets = config.get("markets", [])
    headers = _app_token_headers()

    engine = create_engine(get_database_url())
    try:
        succeeded = 0
        for entry in markets:
            symbol = entry["symbol"]
            try:
                ingest_market(engine, entry, defaults, headers)
                succeeded += 1
            except Exception:
                logger.exception("CFTC market %s failed; continuing with the rest.", symbol)
        logger.info("CFTC ETL complete: %d/%d markets succeeded.", succeeded, len(markets))
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
