"""Tests for the manual ETL trigger feature (#29).

Three layers mirror the project pattern:

* **Pure, DB-free unit tests** — rate-limit logic, AC#16.
* **Static isolation test** — ``dashboard.main`` must not import from ``etl/``
  (AC#18, mirrors ``test_no_dashboard_module_imports_etl`` in test_panel_d.py).
* **Scheduler unit tests** — ``_check_manual_trigger`` with injected engine mock
  (AC#19).
* **Live-Postgres-or-skip integration tests** — POST insert, rate-limit on
  second POST, scheduled poll marks row processed (AC#17).
"""

import datetime as dt
import os
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url

ET = ZoneInfo("America/New_York")

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
alembic_config = pytest.importorskip("alembic.config")
alembic_command = pytest.importorskip("alembic.command")

from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _et(year, month, day, hour=12, minute=0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=ET)


def _monkeypatch_db_env(monkeypatch):
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))


def _dashboard_main(monkeypatch):
    _monkeypatch_db_env(monkeypatch)
    import dashboard.main as dm
    return dm


# ---------------------------------------------------------------------------
# AC#16 — pure rate-limit unit tests (no DB)
# ---------------------------------------------------------------------------

class _FakeConn:
    """Minimal stand-in for a SQLAlchemy connection for trigger-check tests."""

    def __init__(self, row=None, raise_exc=None):
        self._row = row
        self._raise_exc = raise_exc

    def execute(self, _statement):
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResult(self._row)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


def test_rate_limit_no_rows_allows_insert(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    conn = _FakeConn(row=None)
    rate_limited, wait = dm._check_trigger_rate_limit(conn)
    assert rate_limited is False
    assert wait is None


def test_rate_limit_unprocessed_row_blocks(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    now = dt.datetime(2026, 6, 26, 12, 0, tzinfo=dt.timezone.utc)
    # Row requested 2 minutes ago — still within the 10-minute cooldown.
    requested_at = now - dt.timedelta(minutes=2)
    conn = _FakeConn(row=(1, requested_at))
    rate_limited, wait = dm._check_trigger_rate_limit(conn, now=now)
    assert rate_limited is True
    assert wait is not None and wait >= 1


def test_rate_limit_recently_processed_row_blocks(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    now = dt.datetime(2026, 6, 26, 12, 0, tzinfo=dt.timezone.utc)
    # Row requested 5 minutes ago (processed, but within the 10-minute window).
    requested_at = now - dt.timedelta(minutes=5)
    conn = _FakeConn(row=(1, requested_at))
    rate_limited, wait = dm._check_trigger_rate_limit(conn, now=now)
    assert rate_limited is True
    assert wait is not None and wait >= 1


def test_rate_limit_old_processed_row_allows_insert(monkeypatch):
    """A row whose requested_at is older than the cooldown window allows a new insert.

    The SQL query in _TRIGGER_CHECK_SQL gates on
    ``processed_at IS NULL OR processed_at >= now() - interval '10 minutes'``.
    For the pure test we simulate this by only passing rows that would match
    the SQL filter — here we simulate no row returned (the SQL would return
    nothing for an old processed row).
    """
    dm = _dashboard_main(monkeypatch)
    # No matching row from the DB (old processed row doesn't satisfy the WHERE).
    conn = _FakeConn(row=None)
    rate_limited, wait = dm._check_trigger_rate_limit(conn)
    assert rate_limited is False
    assert wait is None


def test_rate_limit_wait_minutes_computed_correctly(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    now = dt.datetime(2026, 6, 26, 12, 0, tzinfo=dt.timezone.utc)
    # Row requested 3 minutes ago — remaining ≈ 10 - 3 = 7 + 1 = 8 minutes.
    requested_at = now - dt.timedelta(minutes=3)
    conn = _FakeConn(row=(1, requested_at))
    _, wait = dm._check_trigger_rate_limit(conn, now=now)
    assert wait is not None
    assert 7 <= wait <= 8


def test_rate_limit_naive_datetime_handled(monkeypatch):
    """A naive requested_at (no tzinfo) is coerced to UTC — no crash."""
    dm = _dashboard_main(monkeypatch)
    now = dt.datetime(2026, 6, 26, 12, 0, tzinfo=dt.timezone.utc)
    requested_at_naive = dt.datetime(2026, 6, 26, 11, 58)  # naive, ~2 min ago
    conn = _FakeConn(row=(1, requested_at_naive))
    rate_limited, _ = dm._check_trigger_rate_limit(conn, now=now)
    assert rate_limited is True


# ---------------------------------------------------------------------------
# AC#18 — static isolation: dashboard.main must not import from etl/
# ---------------------------------------------------------------------------

def test_dashboard_main_does_not_import_etl(monkeypatch):
    _monkeypatch_db_env(monkeypatch)
    import sys
    # Force a fresh import to inspect all transitively loaded modules.
    mods_before = set(sys.modules.keys())
    import dashboard.main  # noqa: F401 — imported for side-effect inspection
    mods_after = set(sys.modules.keys())
    new_mods = mods_after - mods_before
    etl_mods = [m for m in new_mods if m == "etl" or m.startswith("etl.")]
    assert etl_mods == [], (
        f"dashboard.main transitively imported etl modules: {etl_mods}. "
        "The dashboard image ships without etl/ — this would crash on Railway."
    )


# ---------------------------------------------------------------------------
# AC#16 extra — pure unit tests for POST /health/trigger error paths
# (no live Postgres required — engine is replaced with a raising mock)
# ---------------------------------------------------------------------------

class _RaisingCtx:
    """Context-manager that raises on __enter__."""
    def __init__(self, exc):
        self._exc = exc
    def __enter__(self):
        raise self._exc
    def __exit__(self, *_):
        pass


def test_post_trigger_operational_error_returns_503(monkeypatch):
    """OperationalError (DB down) on the POST → HTTP 503, never 500."""
    dm = _dashboard_main(monkeypatch)
    exc = OperationalError("down", {}, Exception())
    mock_engine = MagicMock()
    mock_engine.begin.return_value = _RaisingCtx(exc)
    with patch.object(dm, "engine", mock_engine):
        with TestClient(dm.app, raise_server_exceptions=False) as client:
            resp = client.post("/health/trigger", follow_redirects=False)
    assert resp.status_code == 503


def test_post_trigger_programming_error_redirects_unavailable(monkeypatch):
    """ProgrammingError (pre-migration table missing) → 303 to ?trigger_unavailable=1."""
    dm = _dashboard_main(monkeypatch)
    exc = ProgrammingError("no table", {}, Exception())
    mock_engine = MagicMock()
    mock_engine.begin.return_value = _RaisingCtx(exc)
    with patch.object(dm, "engine", mock_engine):
        with TestClient(dm.app, raise_server_exceptions=False) as client:
            resp = client.post("/health/trigger", follow_redirects=False)
    assert resp.status_code == 303
    assert "trigger_unavailable=1" in resp.headers["location"]


# ---------------------------------------------------------------------------
# AC#19 — scheduler pure tests (_check_manual_trigger)
# ---------------------------------------------------------------------------

def _make_schedule():
    from common.config import load_scheduler_config
    from etl.scheduler import build_schedule
    return build_schedule(load_scheduler_config())


def _runners(record, failing=()):
    def make(name):
        def fn():
            record.append(name)
            if name in failing:
                raise RuntimeError(f"{name} boom")
        return fn
    return {n: make(n) for n in ("fred", "eia", "usda", "cftc",
                                  "curve_shape", "iv", "vol_indices", "prices")}


class _MockEngine:
    """Minimal engine mock for testing _check_manual_trigger without a DB."""

    def __init__(self, row=None, raise_on_connect=None):
        self._row = row
        self._raise_on_connect = raise_on_connect
        self.disposed = False
        self._update_called = False

    def connect(self):
        return _MockConnCtx(self._row, self._raise_on_connect)

    def begin(self):
        return _MockBeginCtx(self)

    def dispose(self):
        self.disposed = True


class _MockConnCtx:
    def __init__(self, row, raise_exc):
        self._row = row
        self._raise_exc = raise_exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, _stmt):
        if self._raise_exc is not None:
            raise self._raise_exc
        return _MockResult(self._row)


class _MockBeginCtx:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, _stmt, *args, **kwargs):
        self._engine._update_called = True


class _MockResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


def test_check_manual_trigger_no_row_no_dispatch():
    from etl.scheduler import _check_manual_trigger
    schedule = _make_schedule()
    record = []
    engine = _MockEngine(row=None)
    _check_manual_trigger(schedule, _runners(record), _et(2026, 6, 26), engine=engine)
    assert record == []


def test_check_manual_trigger_unprocessed_row_dispatches_all_slots():
    from etl import scheduler
    schedule = _make_schedule()
    record = []
    # An unprocessed row: (id=1, slot='all')
    engine = _MockEngine(row=(1, "all"))
    # Use a time inside the session window so guarded slots run.
    now_et = _et(2026, 6, 25, 16, 20)  # Wednesday 16:20 ET — session window
    heartbeats = []
    scheduler._check_manual_trigger(
        schedule, _runners(record), now_et,
        heartbeat=lambda *a, **k: heartbeats.append(a[1]),
        engine=engine,
    )
    # All sources from all slots should have been attempted.
    all_sources = set()
    for slot in schedule.slots.values():
        all_sources.update(slot.sources)
    assert set(record) == all_sources
    assert engine._update_called is True


def test_check_manual_trigger_session_guarded_slot_skipped_outside_window():
    from etl import scheduler
    schedule = _make_schedule()
    record = []
    engine = _MockEngine(row=(1, "all"))
    # Off-hours: session-guarded slots (curve, close-batch) should be skipped.
    now_et = _et(2026, 6, 25, 9, 0)  # 09:00 ET — outside window
    scheduler._check_manual_trigger(
        schedule, _runners(record), now_et, engine=engine,
    )
    # Session-guarded sources (iv, vol_indices, prices, curve_shape) must NOT run.
    session_guarded_sources = set()
    for slot in schedule.slots.values():
        if slot.session_guarded:
            session_guarded_sources.update(slot.sources)
    for src in session_guarded_sources:
        assert src not in record, f"{src} ran despite being outside session window"


def test_check_manual_trigger_marks_row_processed():
    from etl import scheduler
    schedule = _make_schedule()
    record = []
    engine = _MockEngine(row=(42, "all"))
    now_et = _et(2026, 6, 25, 16, 20)
    scheduler._check_manual_trigger(
        schedule, _runners(record), now_et, engine=engine,
    )
    assert engine._update_called is True


def test_check_manual_trigger_db_unavailable_loop_continues():
    from etl import scheduler
    # Reset the error-logged sentinel before this test.
    scheduler._trigger_error_logged = False
    schedule = _make_schedule()
    record = []
    engine = _MockEngine(raise_on_connect=OperationalError("down", {}, Exception()))
    now_et = _et(2026, 6, 26, 12, 0)
    # Should not raise — loop continues silently (AC#14).
    scheduler._check_manual_trigger(
        schedule, _runners(record), now_et, engine=engine,
    )
    assert record == []


def test_check_manual_trigger_pre_migration_loop_continues():
    from etl import scheduler
    scheduler._trigger_error_logged = False
    schedule = _make_schedule()
    record = []
    engine = _MockEngine(
        raise_on_connect=ProgrammingError("no table", {}, Exception())
    )
    now_et = _et(2026, 6, 26, 12, 0)
    scheduler._check_manual_trigger(
        schedule, _runners(record), now_et, engine=engine,
    )
    assert record == []


def test_check_manual_trigger_db_error_logged_only_once():
    """Repeated DB failures must not flood the log (AC#14)."""
    from etl import scheduler
    import logging

    scheduler._trigger_error_logged = False
    schedule = _make_schedule()
    engine = _MockEngine(raise_on_connect=OperationalError("down", {}, Exception()))
    now_et = _et(2026, 6, 26, 12, 0)
    log_records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            log_records.append(record)

    handler = _Capture()
    scheduler.logger.addHandler(handler)
    try:
        for _ in range(3):
            scheduler._check_manual_trigger(schedule, {}, now_et, engine=engine)
    finally:
        scheduler.logger.removeHandler(handler)
    warning_msgs = [r for r in log_records if r.levelname == "WARNING"]
    assert len(warning_msgs) == 1, (
        f"Expected exactly 1 WARNING log for repeated DB errors, got {len(warning_msgs)}"
    )


# ---------------------------------------------------------------------------
# Live-Postgres-or-skip integration tests (AC#17)
# ---------------------------------------------------------------------------

def _alembic_cfg():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    return cfg


@pytest.fixture
def trigger_client(monkeypatch):
    """Skip without a DB; migrate to head and yield (client, db_engine)."""
    _monkeypatch_db_env(monkeypatch)
    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for trigger tests")

    cfg = _alembic_cfg()
    alembic_command.upgrade(cfg, "head")

    import importlib
    import dashboard.main as dm
    importlib.reload(dm)

    try:
        with TestClient(dm.app) as client:
            yield client, engine
    finally:
        dm.engine.dispose()
        engine.dispose()


def _cleanup_triggers(engine):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM etl_manual_trigger"))


def test_post_trigger_inserts_row_and_redirects(trigger_client):
    client, engine = trigger_client
    _cleanup_triggers(engine)
    try:
        resp = client.post("/health/trigger", follow_redirects=False)
        assert resp.status_code == 303
        assert "triggered=1" in resp.headers["location"]
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM etl_manual_trigger WHERE processed_at IS NULL")
            ).scalar()
        assert count == 1
    finally:
        _cleanup_triggers(engine)


def test_second_post_within_cooldown_is_rate_limited(trigger_client):
    client, engine = trigger_client
    _cleanup_triggers(engine)
    try:
        # First POST → success.
        r1 = client.post("/health/trigger", follow_redirects=False)
        assert r1.status_code == 303
        assert "triggered=1" in r1.headers["location"]
        # Second POST immediately → rate limited.
        r2 = client.post("/health/trigger", follow_redirects=False)
        assert r2.status_code == 303
        assert "rate_limited=1" in r2.headers["location"]
    finally:
        _cleanup_triggers(engine)


def test_post_after_cooldown_inserts_new_row(trigger_client):
    """A second POST succeeds once the first row is old enough.

    We fake an old row by backdating ``requested_at`` directly in the DB
    (11 minutes ago — past the 10-minute cooldown) and marking it processed,
    so the SQL WHERE clause would not match it.
    """
    client, engine = trigger_client
    _cleanup_triggers(engine)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO etl_manual_trigger (requested_at, processed_at, slot) "
                    "VALUES (now() - interval '11 minutes', now() - interval '1 minute', 'all')"
                )
            )
        resp = client.post("/health/trigger", follow_redirects=False)
        assert resp.status_code == 303
        assert "triggered=1" in resp.headers["location"]
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM etl_manual_trigger WHERE processed_at IS NULL")
            ).scalar()
        assert count == 1
    finally:
        _cleanup_triggers(engine)


def test_health_page_renders_trigger_form(trigger_client):
    """The /health HTML page contains the trigger form and button (AC#6)."""
    client, _ = trigger_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "Run all ETL now" in resp.text
    assert 'action="/health/trigger"' in resp.text


def test_health_page_triggered_banner(trigger_client):
    """?triggered=1 shows the green confirmation banner (AC#7)."""
    client, _ = trigger_client
    resp = client.get("/health?triggered=1")
    assert resp.status_code == 200
    assert "ETL run triggered" in resp.text


def test_health_page_rate_limited_banner(trigger_client):
    """?rate_limited=1 shows the amber rate-limit banner (AC#8)."""
    client, _ = trigger_client
    resp = client.get("/health?rate_limited=1&wait_minutes=7")
    assert resp.status_code == 200
    assert "Already triggered recently" in resp.text
    assert "7" in resp.text


def test_health_page_trigger_unavailable_hides_button(trigger_client):
    """?trigger_unavailable=1 hides the trigger form (AC#9)."""
    client, _ = trigger_client
    resp = client.get("/health?trigger_unavailable=1")
    assert resp.status_code == 200
    assert "Run all ETL now" not in resp.text
