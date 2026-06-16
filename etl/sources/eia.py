"""EIA energy-inventory ETL → inventories (Panel B).

Pulls a config-driven set of EIA series (weekly crude/products stocks incl.
Cushing, weekly natural-gas working storage, and production/demand proxies)
from the free EIA Open Data v2 API and upserts each observation into
``inventories`` on the natural key ``(source, series_id, date)``.

Second Phase 2 ETL source — follows the reusable pattern established by FRED
(etl/sources/fred.py):

* **Config-driven** — series ids live in ``config/eia_series.yaml``, never here.
* **Idempotent / append-only** — ``INSERT ... ON CONFLICT (source, series_id,
  date) DO UPDATE`` so a same-date re-run upserts in place, never duplicates.
  EIA revisions overwrite the value for that date (the desired behaviour).
* **Incremental + revision lookback** — each series refetches from the *year*
  of ``max(stored date) - revision_lookback_days`` (falling back to the
  configured ``observation_start`` when the series has no rows yet). Sending the
  year as the API lower bound is safe across EIA's weekly/monthly/annual period
  formats, so the first run backfills history and later runs refetch only the
  current year (catching restatements) instead of the full series.
* **Per-source isolation** — a failure on one series is caught, logged with the
  offending ``series_id``, and never aborts the remaining series or the container.

The EIA v2 ``seriesid`` endpoint paginates with ``offset``/``length`` (max 5000
rows); :func:`_fetch_observations` follows the pages so long backfills are never
silently truncated.

Run manually: ``python -m etl.sources.eia``. No scheduler is wired yet
(CLAUDE.md §2); cadence is a later ticket.
"""

import datetime as dt
import logging
import os
from typing import Optional

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url, load_eia_series

logger = logging.getLogger("etl.eia")

_EIA_SERIESID_URL = "https://api.eia.gov/v2/seriesid/"
_SOURCE = "EIA"
_PAGE_LENGTH = 5000
_REQUEST_TIMEOUT = 30

_UPSERT_SQL = text(
    """
    INSERT INTO inventories (source, series_id, date, value, unit)
    VALUES (:source, :series_id, :date, :value, :unit)
    ON CONFLICT (source, series_id, date)
    DO UPDATE SET value = EXCLUDED.value, unit = EXCLUDED.unit
    """
)


def _require_api_key() -> str:
    key = os.environ.get("EIA_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "EIA_API_KEY is missing or blank. Set it in the environment "
            "(see .env.example) before running the EIA ETL."
        )
    return key


def _redact(message: str, api_key: str) -> str:
    """Strip the API key out of an error string. EIA takes the key as a query
    param, so request exceptions (HTTP status, connection, timeout) embed the
    full URL — keep it out of the logs."""
    return message.replace(api_key, "***") if api_key else message


def _period_to_date(period: str) -> dt.date:
    """Map an EIA period to a calendar date. Periods are ``YYYY`` (annual),
    ``YYYY-MM`` (monthly), or ``YYYY-MM-DD`` (weekly/daily); the shorter forms
    anchor to the first day of the year/month."""
    parts = period.split("-")
    if len(parts) == 1:
        return dt.date(int(parts[0]), 1, 1)
    if len(parts) == 2:
        return dt.date(int(parts[0]), int(parts[1]), 1)
    return dt.date.fromisoformat(period)


def _latest_stored_date(engine: Engine, series_id: str) -> Optional[dt.date]:
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT max(date) FROM inventories "
                "WHERE source = :source AND series_id = :series_id"
            ),
            {"source": _SOURCE, "series_id": series_id},
        )
        return result.scalar()


def _start_year(
    engine: Engine,
    series_id: str,
    observation_start: str,
    revision_lookback_days: int,
) -> str:
    """First run: backfill from observation_start's year. Later runs: refetch
    from the year of a lookback window before the latest stored date so EIA
    revisions overwrite in place."""
    latest = _latest_stored_date(engine, series_id)
    if latest is None:
        return observation_start[:4]
    incremental = latest - dt.timedelta(days=revision_lookback_days)
    return str(incremental.year)


def _fetch_observations(series_id: str, api_key: str, start_year: str) -> list[dict]:
    """Page through the EIA v2 seriesid endpoint from ``start_year`` onward,
    following offset/length pagination so long backfills aren't truncated."""
    observations: list[dict] = []
    offset = 0
    while True:
        params = {
            "api_key": api_key,
            "start": start_year,
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "offset": offset,
            "length": _PAGE_LENGTH,
        }
        try:
            response = requests.get(
                _EIA_SERIESID_URL + series_id, params=params, timeout=_REQUEST_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"EIA request for {series_id} failed: {_redact(str(exc), api_key)}"
            ) from None
        page = response.json().get("response", {}).get("data", [])
        observations.extend(page)
        if len(page) < _PAGE_LENGTH:
            break
        offset += _PAGE_LENGTH
    return observations


def _to_rows(series_id: str, observations: list[dict], unit: Optional[str]) -> list[dict]:
    """Map EIA observations to upsert rows. Missing/withheld values (JSON null
    or blank) become NULL — never coerced to 0. The configured unit wins; the
    API's reported unit is the fallback."""
    rows = []
    for obs in observations:
        raw = obs.get("value")
        value = None if raw is None or raw == "" else raw
        rows.append(
            {
                "source": _SOURCE,
                "series_id": series_id,
                "date": _period_to_date(obs["period"]).isoformat(),
                "value": value,
                "unit": unit or obs.get("units"),
            }
        )
    return rows


def _upsert(engine: Engine, rows: list[dict]) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, rows)


def ingest_series(engine: Engine, entry: dict, api_key: str, defaults: dict) -> int:
    """Fetch and upsert one series. Returns the number of observations written."""
    series_id = entry["id"]
    observation_start = entry.get(
        "observation_start", defaults.get("observation_start", "2005-01-01")
    )
    start_year = _start_year(
        engine,
        series_id,
        observation_start,
        int(defaults.get("revision_lookback_days", 14)),
    )
    observations = _fetch_observations(series_id, api_key, start_year)
    rows = _to_rows(series_id, observations, entry.get("unit"))
    _upsert(engine, rows)
    logger.info(
        "EIA %s: upserted %d observations from %s", series_id, len(rows), start_year
    )
    return len(rows)


def run() -> None:
    api_key = _require_api_key()
    config = load_eia_series()
    defaults = config.get("defaults", {})
    series = config.get("series", [])

    engine = create_engine(get_database_url())
    try:
        succeeded = 0
        for entry in series:
            series_id = entry["id"]
            try:
                ingest_series(engine, entry, api_key, defaults)
                succeeded += 1
            except Exception:
                logger.exception("EIA series %s failed; continuing with the rest.", series_id)
        logger.info("EIA ETL complete: %d/%d series succeeded.", succeeded, len(series))
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
