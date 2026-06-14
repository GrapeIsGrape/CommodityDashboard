"""Shared configuration: database connection and symbol universe.

All environment-specific values come from env vars so the identical image
runs on local Compose, Railway, and Synology with no code changes.
"""

import os
from pathlib import Path

import yaml
from sqlalchemy.engine import URL

_DEFAULT_SYMBOLS_PATH = Path(__file__).resolve().parent.parent / "config" / "symbols.yaml"


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
