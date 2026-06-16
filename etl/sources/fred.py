"""FRED macro ETL → macro_metrics (Panel A).

Pulls a config-driven set of FRED series (rates, real yields, breakevens,
CPI/PCE/PPI, employment, GDP, VIX, dollar proxy) and upserts each observation
into ``macro_metrics`` on the natural key ``(series_id, date)``.

This is the first Phase 2 ETL source and establishes the reusable pattern the
EIA/USDA/CFTC sources follow:

* **Config-driven** — series ids live in ``config/fred_series.yaml``, never here.
* **Idempotent / append-only** — ``INSERT ... ON CONFLICT (series_id, date)
  DO UPDATE`` so a same-date re-run upserts in place, never duplicates. FRED
  revisions overwrite the value for that date (the desired behaviour).
* **Incremental + revision lookback** — each series refetches from
  ``max(stored date) - revision_lookback_days`` (falling back to the configured
  ``observation_start`` when the series has no rows yet), so the first run
  backfills history and later runs are cheap while still catching restatements.
* **Per-source isolation** — a failure on one series is caught, logged with the
  offending ``series_id``, and never aborts the remaining series or the container.

Run manually: ``python -m etl.sources.fred``. No scheduler is wired yet
(CLAUDE.md §2); cadence is a later ticket.
"""

import datetime as dt
import logging
import os
from typing import Optional

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url, load_fred_series

logger = logging.getLogger("etl.fred")

_FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
_SOURCE = "FRED"
_MISSING_SENTINEL = "."
_REQUEST_TIMEOUT = 30

_UPSERT_SQL = text(
    """
    INSERT INTO macro_metrics (series_id, date, value, source)
    VALUES (:series_id, :date, :value, :source)
    ON CONFLICT (series_id, date)
    DO UPDATE SET value = EXCLUDED.value, source = EXCLUDED.source
    """
)


def _require_api_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FRED_API_KEY is missing or blank. Set it in the environment "
            "(see .env.example) before running the FRED ETL."
        )
    return key


def _redact(message: str, api_key: str) -> str:
    """Strip the API key out of an error string. FRED takes the key as a query
    param, so request exceptions (HTTP status, connection, timeout) embed the
    full URL — keep it out of the logs."""
    return message.replace(api_key, "***") if api_key else message


def _latest_stored_date(engine: Engine, series_id: str) -> Optional[dt.date]:
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT max(date) FROM macro_metrics WHERE series_id = :series_id"),
            {"series_id": series_id},
        )
        return result.scalar()


def _start_date(
    engine: Engine,
    series_id: str,
    observation_start: str,
    revision_lookback_days: int,
) -> str:
    """First run: backfill from observation_start. Later runs: refetch from a
    lookback window before the latest stored date so revisions overwrite."""
    latest = _latest_stored_date(engine, series_id)
    if latest is None:
        return observation_start
    incremental = latest - dt.timedelta(days=revision_lookback_days)
    return incremental.isoformat()


def _fetch_observations(series_id: str, api_key: str, observation_start: str) -> list[dict]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": observation_start,
    }
    try:
        response = requests.get(
            _FRED_OBSERVATIONS_URL, params=params, timeout=_REQUEST_TIMEOUT
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"FRED request for {series_id} failed: {_redact(str(exc), api_key)}"
        ) from None
    return response.json().get("observations", [])


def _to_rows(series_id: str, observations: list[dict]) -> list[dict]:
    """Map FRED observations to upsert rows. The "." sentinel becomes NULL —
    never coerced to a number."""
    rows = []
    for obs in observations:
        raw = obs.get("value")
        value = None if raw is None or raw == _MISSING_SENTINEL else raw
        rows.append(
            {
                "series_id": series_id,
                "date": obs["date"],
                "value": value,
                "source": _SOURCE,
            }
        )
    return rows


def _upsert(engine: Engine, rows: list[dict]) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, rows)


def ingest_series(engine: Engine, series_id: str, api_key: str, defaults: dict) -> int:
    """Fetch and upsert one series. Returns the number of observations written."""
    observation_start = _start_date(
        engine,
        series_id,
        defaults.get("observation_start", "2005-01-01"),
        int(defaults.get("revision_lookback_days", 14)),
    )
    observations = _fetch_observations(series_id, api_key, observation_start)
    rows = _to_rows(series_id, observations)
    _upsert(engine, rows)
    logger.info("FRED %s: upserted %d observations from %s", series_id, len(rows), observation_start)
    return len(rows)


def run() -> None:
    api_key = _require_api_key()
    config = load_fred_series()
    defaults = config.get("defaults", {})
    series = config.get("series", [])

    engine = create_engine(get_database_url())
    try:
        succeeded = 0
        for entry in series:
            series_id = entry["id"]
            try:
                ingest_series(engine, series_id, api_key, defaults)
                succeeded += 1
            except Exception:
                logger.exception("FRED series %s failed; continuing with the rest.", series_id)
        logger.info("FRED ETL complete: %d/%d series succeeded.", succeeded, len(series))
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
