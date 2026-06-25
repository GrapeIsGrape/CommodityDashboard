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


# --- /health etl summary (#24) -------------------------------------------


def _dashboard_main(monkeypatch):
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    import dashboard.main as dashboard_main

    return dashboard_main


import datetime as _dt  # noqa: E402


def test_shape_etl_row_iso_formats_and_keeps_last_success(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    row = {
        "slot": "close-batch",
        "source": "iv",
        "run_date": _dt.date(2026, 6, 22),
        "run_finished_at": _dt.datetime(2026, 6, 22, 20, 22, tzinfo=_dt.timezone.utc),
        "last_status": "failure",
        # last success pre-dates the failed last attempt
        "last_success_run_date": _dt.date(2026, 6, 21),
        "last_success_finished_at": _dt.datetime(2026, 6, 21, 20, 22, tzinfo=_dt.timezone.utc),
    }
    shaped = dm._shape_etl_row(row)
    assert shaped["slot"] == "close-batch"
    assert shaped["source"] == "iv"
    assert shaped["last_status"] == "failure"
    assert shaped["run_date"] == "2026-06-22"
    assert shaped["run_finished_at"] == "2026-06-22T20:22:00+00:00"
    # last success is older than the last (failed) attempt — surfaced distinctly.
    assert shaped["last_success_run_date"] == "2026-06-21"


def test_shape_etl_row_passes_through_nulls(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    row = {
        "slot": "close-batch",
        "source": "iv",
        "run_date": _dt.date(2026, 6, 22),
        "run_finished_at": None,
        "last_status": "skipped",
        "last_success_run_date": None,   # never succeeded
        "last_success_finished_at": None,
    }
    shaped = dm._shape_etl_row(row)
    assert shaped["run_finished_at"] is None
    assert shaped["last_success_run_date"] is None
    assert shaped["last_success_finished_at"] is None
    assert shaped["last_status"] == "skipped"


def test_read_etl_summary_none_when_table_missing(monkeypatch):
    dm = _dashboard_main(monkeypatch)

    class _MissingTableConn:
        def execute(self, _statement):
            raise ProgrammingError("SELECT ... etl_run_log", {}, Exception("UndefinedTable"))

    assert dm._read_etl_summary(_MissingTableConn()) is None


# --- AC#8a: reconcile against the configured slot/source set ---------------


def _present(slot, source, status):
    return {
        "slot": slot,
        "source": source,
        "run_date": "2026-06-22",
        "run_finished_at": "2026-06-22T20:22:00+00:00",
        "last_status": status,
        "last_success_run_date": "2026-06-22",
        "last_success_finished_at": "2026-06-22T20:22:00+00:00",
    }


def test_reconcile_fills_never_ran_for_configured_source_without_row(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    merged = dm._reconcile_etl_summary(
        present_rows=[],
        configured_pairs=[("fred", "fred"), ("close-batch", "iv")],
    )
    by_key = {(e["slot"], e["source"]): e for e in merged}
    assert by_key[("fred", "fred")]["last_status"] == "never_ran"
    assert by_key[("close-batch", "iv")]["last_status"] == "never_ran"
    # never_ran entries carry no fabricated timestamps.
    for entry in merged:
        assert entry["run_date"] is None
        assert entry["run_finished_at"] is None
        assert entry["last_success_run_date"] is None
        assert entry["last_success_finished_at"] is None


def test_reconcile_keeps_real_status_for_configured_source_with_row(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    merged = dm._reconcile_etl_summary(
        present_rows=[_present("fred", "fred", "success")],
        configured_pairs=[("fred", "fred"), ("eia", "eia")],
    )
    by_key = {(e["slot"], e["source"]): e for e in merged}
    assert by_key[("fred", "fred")]["last_status"] == "success"
    assert by_key[("fred", "fred")]["run_date"] == "2026-06-22"
    # the configured-but-unlogged source is the only one filled in.
    assert by_key[("eia", "eia")]["last_status"] == "never_ran"


def test_reconcile_preserves_unconfigured_logged_rows(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # A row present in the log but no longer in config (e.g. a renamed slot) is
    # still surfaced — never dropped — so historical heartbeats stay visible.
    merged = dm._reconcile_etl_summary(
        present_rows=[_present("retired-slot", "old", "success")],
        configured_pairs=[("fred", "fred")],
    )
    keys = {(e["slot"], e["source"]) for e in merged}
    assert ("retired-slot", "old") in keys
    assert ("fred", "fred") in keys


def test_reconcile_does_not_duplicate_configured_source_with_row(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    merged = dm._reconcile_etl_summary(
        present_rows=[_present("fred", "fred", "failure")],
        configured_pairs=[("fred", "fred"), ("fred", "fred")],
    )
    fred = [e for e in merged if (e["slot"], e["source"]) == ("fred", "fred")]
    assert len(fred) == 1
    assert fred[0]["last_status"] == "failure"


def test_reconcile_orders_problems_first_then_stable(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    merged = dm._reconcile_etl_summary(
        present_rows=[
            _present("z-slot", "ok", "success"),
            _present("a-slot", "skip", "skipped"),
            _present("m-slot", "bad", "failure"),
        ],
        configured_pairs=[("missing-slot", "gone")],  # -> never_ran
    )
    statuses = [e["last_status"] for e in merged]
    # failure first, then never_ran, then skipped, then success.
    assert statuses == ["failure", "never_ran", "skipped", "success"]


def test_configured_slot_sources_reads_yaml_without_etl(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    pairs = dm._configured_slot_sources()
    # Every slot in config/scheduler.yaml is represented, expanded per-source.
    assert ("fred", "fred") in pairs
    assert ("close-batch", "iv") in pairs
    assert ("close-batch", "vol_indices") in pairs
    assert ("close-batch", "prices") in pairs
    assert ("curve", "curve_shape") in pairs


def test_configured_slot_sources_degrades_on_bad_config(monkeypatch):
    dm = _dashboard_main(monkeypatch)

    def _boom(*a, **k):
        raise ValueError("malformed scheduler.yaml")

    monkeypatch.setattr(dm, "load_scheduler_config", _boom)
    # A bad config degrades to an empty configured set — never raises (no 500).
    assert dm._configured_slot_sources() == []

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
    # The etl run-log summary is present (a list — empty on a fresh DB), never
    # absent at head, and never trips a 500.
    assert "etl" in body
    assert isinstance(body["etl"], list)


def test_health_etl_summary_surfaces_latest_run_per_source(health_client):
    client, _ = health_client

    # Seed two attempts for one (slot, source): an older success then a newer
    # failure, so last_status=failure but last_success still points at the older.
    import datetime as dtime

    from common.config import get_database_url
    from sqlalchemy import create_engine, text

    engine = create_engine(get_database_url())
    slot, source = "uat-slot", "uat-source"
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM etl_run_log WHERE slot=:s AND source=:src"),
                         {"s": slot, "src": source})
            conn.execute(
                text(
                    "INSERT INTO etl_run_log (slot, source, run_date, "
                    "run_finished_at, status) VALUES "
                    "(:s, :src, :d1, :f1, 'success'), "
                    "(:s, :src, :d2, :f2, 'failure')"
                ),
                {
                    "s": slot, "src": source,
                    "d1": dtime.date(2026, 6, 21),
                    "f1": dtime.datetime(2026, 6, 21, 20, 22, tzinfo=dtime.timezone.utc),
                    "d2": dtime.date(2026, 6, 22),
                    "f2": dtime.datetime(2026, 6, 22, 20, 22, tzinfo=dtime.timezone.utc),
                },
            )

        body = client.get("/health").json()
        entry = next(e for e in body["etl"] if e["slot"] == slot and e["source"] == source)
        assert entry["last_status"] == "failure"        # newest attempt
        assert entry["run_date"] == "2026-06-22"
        assert entry["last_success_run_date"] == "2026-06-21"  # older success retained
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM etl_run_log WHERE slot=:s AND source=:src"),
                         {"s": slot, "src": source})
        engine.dispose()


def test_health_etl_summary_surfaces_never_ran_for_configured_source(health_client):
    client, _ = health_client

    from common.config import get_database_url, load_scheduler_config
    from sqlalchemy import create_engine, text

    # Pick a real configured (slot, source) and ensure it has NO logged row, so it
    # must surface as never_ran rather than vanishing from the summary (AC#8a).
    slots = (load_scheduler_config().get("slots") or {})
    slot, spec = next(iter(slots.items()))
    source = (spec.get("sources") or [None])[0]
    assert source is not None

    engine = create_engine(get_database_url())
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM etl_run_log WHERE slot=:s AND source=:src"),
                         {"s": slot, "src": source})

        body = client.get("/health").json()
        entry = next(e for e in body["etl"] if e["slot"] == slot and e["source"] == source)
        assert entry["last_status"] == "never_ran"
        assert entry["run_date"] is None
        assert entry["last_success_run_date"] is None
    finally:
        engine.dispose()
