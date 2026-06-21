"""Shared configuration: database connection and symbol universe.

All environment-specific values come from env vars so the identical image
runs on local Compose, Railway, and Synology with no code changes.
"""

import os
from pathlib import Path

import yaml
from sqlalchemy.engine import URL

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_DEFAULT_SYMBOLS_PATH = _CONFIG_DIR / "symbols.yaml"
_DEFAULT_FRED_SERIES_PATH = _CONFIG_DIR / "fred_series.yaml"
_DEFAULT_EIA_SERIES_PATH = _CONFIG_DIR / "eia_series.yaml"
_DEFAULT_USDA_SERIES_PATH = _CONFIG_DIR / "usda_series.yaml"
_DEFAULT_CFTC_MARKETS_PATH = _CONFIG_DIR / "cftc_markets.yaml"
_DEFAULT_SCHEDULER_PATH = _CONFIG_DIR / "scheduler.yaml"


def get_database_url() -> URL:
    """Build the SQLAlchemy URL from POSTGRES_* env vars.

    Uses URL.create so credentials with special characters are escaped
    correctly rather than interpolated into a string.
    """
    return URL.create(
        "postgresql+psycopg2",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ["POSTGRES_DB"],
    )


def load_symbols(path: str | os.PathLike | None = None) -> dict:
    """Load the symbol universe from config/symbols.yaml.

    Override the location with the SYMBOLS_CONFIG env var or the ``path`` arg.
    """
    resolved = Path(path or os.environ.get("SYMBOLS_CONFIG") or _DEFAULT_SYMBOLS_PATH)
    with open(resolved, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_curve_config(path: str | os.PathLike | None = None) -> dict:
    """Load the futures curve-shape config (the ``curve`` block of
    config/symbols.yaml): per-underlying front/deferred ticker specs + the
    annualized-slope deadband default. Returns ``{}`` if the block is absent.

    Reuses the symbols config (no separate file), overridable via SYMBOLS_CONFIG
    or the ``path`` arg, consistent with :func:`load_symbols`.
    """
    return load_symbols(path).get("curve", {})


def load_macro_context(path: str | os.PathLike | None = None) -> list:
    """Load the macro-context tickers (TLT/VTI/QQQ) from the ``macro_context``
    block of config/symbols.yaml — each entry is ``{symbol, name}``. Returns
    ``[]`` if the block is absent.

    Reuses the symbols config (no separate file), overridable via SYMBOLS_CONFIG
    or the ``path`` arg, consistent with :func:`load_symbols`.
    """
    return load_symbols(path).get("macro_context", [])


def load_prices_config(path: str | os.PathLike | None = None) -> dict:
    """Load the daily prices-ETL config (the ``prices`` block of
    config/symbols.yaml): backfill / re-fetch-window defaults. Returns ``{}`` if
    the block is absent.

    Reuses the symbols config (no separate file), overridable via SYMBOLS_CONFIG
    or the ``path`` arg, consistent with :func:`load_symbols`.
    """
    return load_symbols(path).get("prices", {})


def load_fred_series(path: str | os.PathLike | None = None) -> dict:
    """Load the FRED macro series config from config/fred_series.yaml.

    Override the location with the FRED_SERIES_CONFIG env var or the ``path`` arg.
    """
    resolved = Path(path or os.environ.get("FRED_SERIES_CONFIG") or _DEFAULT_FRED_SERIES_PATH)
    with open(resolved, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_eia_series(path: str | os.PathLike | None = None) -> dict:
    """Load the EIA energy-inventory series config from config/eia_series.yaml.

    Override the location with the EIA_SERIES_CONFIG env var or the ``path`` arg.
    """
    resolved = Path(path or os.environ.get("EIA_SERIES_CONFIG") or _DEFAULT_EIA_SERIES_PATH)
    with open(resolved, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_usda_series(path: str | os.PathLike | None = None) -> dict:
    """Load the USDA NASS QuickStats series config from config/usda_series.yaml.

    Override the location with the USDA_SERIES_CONFIG env var or the ``path`` arg.
    """
    resolved = Path(path or os.environ.get("USDA_SERIES_CONFIG") or _DEFAULT_USDA_SERIES_PATH)
    with open(resolved, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_cftc_markets(path: str | os.PathLike | None = None) -> dict:
    """Load the CFTC COT market map from config/cftc_markets.yaml.

    Override the location with the CFTC_MARKETS_CONFIG env var or the ``path`` arg.
    """
    resolved = Path(path or os.environ.get("CFTC_MARKETS_CONFIG") or _DEFAULT_CFTC_MARKETS_PATH)
    with open(resolved, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_scheduler_config(path: str | os.PathLike | None = None) -> dict:
    """Load the ETL scheduler config (slots, session window, timezone) from
    config/scheduler.yaml.

    Override the location with the SCHEDULER_CONFIG env var or the ``path`` arg.
    The timezone may additionally be overridden by the ETL_TZ env var, applied
    by the scheduler itself so the file stays the single source of slot timing.
    """
    resolved = Path(path or os.environ.get("SCHEDULER_CONFIG") or _DEFAULT_SCHEDULER_PATH)
    with open(resolved, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
