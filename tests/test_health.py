"""Tests for the dashboard /health endpoint.

The schema_version assertion runs against a live Postgres when one is reachable
via the POSTGRES_* env vars (the Compose / local DB); when none is reachable the
test is skipped so the suite still passes without a database. FastAPI / httpx
are optional deps here (the dashboard image has them, the bare test env may not),
so the module is skipped when they are absent.
"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")
alembic_script = pytest.importorskip("alembic.script")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import ProgrammingError  # noqa: E402


def _read_schema_version_fn(monkeypatch):
    """Import the handler helper with DB env present (engine creation is lazy)."""
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    import dashboard.main as dashboard_main

    return dashboard_main._read_schema_version


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeConn:
    """Minimal stand-in for a SQLAlchemy connection for the version query."""

    def __init__(self, row=None, raise_missing=False):
        self._row = row
        self._raise_missing = raise_missing

    def execute(self, _statement):
        if self._raise_missing:
            raise ProgrammingError(
                "SELECT version_num FROM alembic_version", {}, Exception("UndefinedTable")
            )
        return _FakeResult(self._row)


def test_read_schema_version_returns_revision(monkeypatch):
    fn = _read_schema_version_fn(monkeypatch)
    assert fn(_FakeConn(row=("0002_data_tables",))) == "0002_data_tables"


def test_read_schema_version_none_when_table_missing(monkeypatch):
    fn = _read_schema_version_fn(monkeypatch)
    assert fn(_FakeConn(raise_missing=True)) is None


def test_read_schema_version_none_when_table_empty(monkeypatch):
    fn = _read_schema_version_fn(monkeypatch)
    assert fn(_FakeConn(row=None)) is None

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


def _alembic_cfg():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    return cfg


def _expected_head(cfg) -> str:
    return alembic_script.ScriptDirectory.from_config(cfg).get_current_head()


@pytest.fixture
def health_client(monkeypatch):
    """Skip without a DB; migrate to head and yield (client, expected_head)."""
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))

    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for health tests")

    cfg = _alembic_cfg()
    alembic_command.upgrade(cfg, "head")

    # Import after env is set so the module-level engine binds to the test DB.
    import importlib

    import dashboard.main as dashboard_main

    importlib.reload(dashboard_main)

    try:
        with TestClient(dashboard_main.app) as client:
            yield client, _expected_head(cfg)
    finally:
        dashboard_main.engine.dispose()
        engine.dispose()


def test_health_reports_schema_version_at_head(health_client):
    client, expected_head = health_client

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "reachable"
    assert "schema_version" in body
    assert body["schema_version"] == expected_head
