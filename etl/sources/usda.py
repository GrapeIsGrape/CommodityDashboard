"""USDA NASS QuickStats ETL → inventories (Panel B, grains).

Pulls a config-driven set of national grain/oilseed series (production and grain
stocks) from the free USDA NASS QuickStats API and upserts each record into
``inventories`` with ``source = 'USDA'`` on the natural key
``(source, series_id, date)``.

Third Phase 2 ETL source — follows the reusable pattern established by FRED
(etl/sources/fred.py) and EIA (etl/sources/eia.py):

* **Config-driven** — query params live in ``config/usda_series.yaml``, never
  here. Each entry carries a synthetic ``id`` (our series_id) plus a QuickStats
  ``query`` dict; the api key / format / ``year__GE`` are added at request time.
* **Idempotent / append-only** — ``INSERT ... ON CONFLICT (source, series_id,
  date) DO UPDATE`` so a same-date re-run upserts in place, never duplicates.
  NASS revisions overwrite the value for that date (the desired behaviour).
* **Incremental + revision lookback** — each series refetches from the *year* of
  ``max(stored date) - revision_lookback_days`` via ``year__GE`` (falling back
  to the configured ``observation_start`` year when the series has no rows yet).
  The first run backfills history; later runs refetch only recent years (so NASS
  restatements overwrite) instead of the whole series.
* **Per-source isolation** — a failure on one series is caught, logged with the
  offending ``id``, and never aborts the remaining series or the container.
* **Secret-safe** — the api key is read from the env only and ``_redact``-ed out
  of any request-failure message before it can reach the logs.

The QuickStats API caps a single response at 50,000 records and errors past that;
the config queries are pinned to national level + a single ``short_desc`` (tiny
result sets) and the year-incremental start keeps later runs small, so no
pagination is needed.

Run manually: ``python -m etl.sources.usda``. No scheduler is wired yet
(CLAUDE.md §2); cadence is a later ticket.
"""

import datetime as dt
import logging
import os
from typing import Optional

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url, load_usda_series

logger = logging.getLogger("etl.usda")

_USDA_QUICKSTATS_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
_SOURCE = "USDA"
_REQUEST_TIMEOUT = 60

# NASS withheld/suppressed/not-applicable sentinels — never coerce to a number.
_NULL_SENTINELS = {"(D)", "(NA)", "(X)", "(Z)", "(S)", "(L)", "(NR)", ""}

# reference_period_desc -> (month, day) within the record's year. Annual periods
# anchor to Jan 1; quarterly grain-stocks periods anchor to the first of the
# stated month. Unmapped periods fall back to Jan 1 (logged at debug).
_MONTH_BY_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_UPSERT_SQL = text(
    """
    INSERT INTO inventories (source, series_id, date, value, unit)
    VALUES (:source, :series_id, :date, :value, :unit)
    ON CONFLICT (source, series_id, date)
    DO UPDATE SET value = EXCLUDED.value, unit = EXCLUDED.unit
    """
)


def _require_api_key() -> str:
    key = os.environ.get("USDA_NASS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "USDA_NASS_API_KEY is missing or blank. Set it in the environment "
            "(see .env.example) before running the USDA ETL."
        )
    return key


def _redact(message: str, api_key: str) -> str:
    """Strip the API key out of an error string. NASS takes the key as a query
    param, so request exceptions (HTTP status, connection, timeout) embed the
    full URL — keep it out of the logs."""
    return message.replace(api_key, "***") if api_key else message


def _record_date(record: dict) -> dt.date:
    """Map a QuickStats record to a calendar date. Prefer an explicit
    ``week_ending`` ISO date when present (crop-progress style); otherwise derive
    from ``year`` + ``reference_period_desc`` (YEAR / MARKETING YEAR -> Jan 1,
    ``FIRST OF <MON>`` -> first of that month). Unmapped periods fall back to
    Jan 1 of the record's year."""
    week_ending = (record.get("week_ending") or "").strip()
    if week_ending:
        return dt.date.fromisoformat(week_ending)

    year = int(record["year"])
    period = (record.get("reference_period_desc") or "").strip().upper()
    # Match a month abbreviation as a whole word (e.g. "FIRST OF DEC"), not a
    # substring — "MARKETING YEAR" must not match "MAR".
    words = period.replace(",", " ").split()
    for word in words:
        if word in _MONTH_BY_ABBR:
            return dt.date(year, _MONTH_BY_ABBR[word], 1)
    if period not in ("", "YEAR", "MARKETING YEAR"):
        logger.debug(
            "USDA: unmapped reference_period_desc %r for year %s; using Jan 1",
            period, year,
        )
    return dt.date(year, 1, 1)


def _parse_value(raw: Optional[str]) -> Optional[str]:
    """NASS reports values as strings with thousands separators. Strip the commas
    and surface withheld/suppressed sentinels (and blanks) as NULL — never 0."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if cleaned in _NULL_SENTINELS:
        return None
    return cleaned.replace(",", "")


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
    from the year of a lookback window before the latest stored date so NASS
    revisions overwrite in place."""
    latest = _latest_stored_date(engine, series_id)
    if latest is None:
        return observation_start[:4]
    incremental = latest - dt.timedelta(days=revision_lookback_days)
    return str(incremental.year)


def _fetch_records(query: dict, api_key: str, start_year: str) -> list[dict]:
    """Fetch QuickStats records for one query from ``start_year`` onward. The
    config query selects the series (short_desc + national level); we add the
    key, JSON format, and the year lower bound here."""
    params = {
        **query,
        "key": api_key,
        "year__GE": start_year,
        "format": "JSON",
    }
    try:
        response = requests.get(
            _USDA_QUICKSTATS_URL, params=params, timeout=_REQUEST_TIMEOUT
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"USDA request failed: {_redact(str(exc), api_key)}"
        ) from None
    return response.json().get("data", [])


def _to_rows(series_id: str, records: list[dict], unit: Optional[str]) -> list[dict]:
    """Map QuickStats records to upsert rows. The configured unit wins; the API's
    reported ``unit_desc`` is the fallback."""
    rows = []
    for record in records:
        rows.append(
            {
                "source": _SOURCE,
                "series_id": series_id,
                "date": _record_date(record).isoformat(),
                "value": _parse_value(record.get("Value")),
                "unit": unit or record.get("unit_desc"),
            }
        )
    return rows


def _upsert(engine: Engine, rows: list[dict]) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, rows)


def ingest_series(engine: Engine, entry: dict, api_key: str, defaults: dict) -> int:
    """Fetch and upsert one series. Returns the number of records written."""
    series_id = entry["id"]
    observation_start = entry.get(
        "observation_start", defaults.get("observation_start", "2005-01-01")
    )
    start_year = _start_year(
        engine,
        series_id,
        observation_start,
        int(defaults.get("revision_lookback_days", 30)),
    )
    records = _fetch_records(entry["query"], api_key, start_year)
    rows = _to_rows(series_id, records, entry.get("unit"))
    _upsert(engine, rows)
    logger.info(
        "USDA %s: upserted %d records from %s", series_id, len(rows), start_year
    )
    return len(rows)


def run() -> None:
    api_key = _require_api_key()
    config = load_usda_series()
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
                logger.exception("USDA series %s failed; continuing with the rest.", series_id)
        logger.info("USDA ETL complete: %d/%d series succeeded.", succeeded, len(series))
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
