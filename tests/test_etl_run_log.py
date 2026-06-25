"""Tests for the ETL run-log / heartbeat (etl/run_log.py + dispatch_slot — #24).

Two layers, mirroring the project pattern:

* **Pure, DB-free helpers** — status classification, the redacted error-summary
  builder (asserts a secret-shaped input is NOT persisted), and the dispatch
  heartbeat wiring (success / failure / skipped) exercised with an injected
  in-memory heartbeat so no clock or DB is touched. Per-source isolation is
  re-asserted: a heartbeat write raising does not abort the batch.
* **Live-Postgres-or-skip** — the real upsert idempotency (same
  ``(slot, source, run_date)`` re-dispatch updates one row) against a migrated DB.

The real source ``run()`` functions are never imported here — runners are
injected, as in test_scheduler.py.
"""
import datetime as dt
import os
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from common.config import get_database_url
from etl import run_log
from etl.run_log import (
    STATUS_FAILURE,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    classify_status,
    redact_detail,
    summarize_exception,
)
from etl.scheduler import Schedule, SessionWindow, Slot, build_schedule, dispatch_slot
from common.config import load_scheduler_config

ET = ZoneInfo("America/New_York")


def _et(year, month, day, hour, minute) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=ET)


@pytest.fixture
def schedule() -> Schedule:
    return build_schedule(load_scheduler_config())


# --- status classification (AC#11) ---------------------------------------


def test_classify_status_success_failure():
    assert classify_status(True) == STATUS_SUCCESS
    assert classify_status(False) == STATUS_FAILURE


# --- redaction (AC#6/#11) ------------------------------------------------


def test_redact_strips_api_key_query_param():
    msg = "FRED request failed: 401 for url ...?api_key=abcd1234SECRET&file_type=json"
    out = redact_detail(msg)
    assert "abcd1234SECRET" not in out
    assert "api_key=***" in out


def test_redact_strips_token_header_and_password():
    assert "tok-xyz" not in redact_detail("X-App-Token: tok-xyz rate limited")
    assert "hunter2" not in redact_detail('password="hunter2" auth failed')
    assert "topsecret" not in redact_detail("authorization: Bearer topsecret")


def test_redact_does_not_over_match_benign_prose():
    # The =/: separator is required, so a secret keyword followed by ordinary
    # prose (no key=value form) is left intact for debuggability.
    assert redact_detail("the secret sauce failed") == "the secret sauce failed"
    assert redact_detail("password is required") == "password is required"
    assert redact_detail("a token was rejected") == "a token was rejected"
    # genuine key=value / header forms are still redacted
    assert "abcd" not in redact_detail("api_key=abcd")
    assert "tok-xyz" not in redact_detail("X-App-Token: tok-xyz")


def test_redact_strips_dsn_password_userinfo():
    # The scheme://user:password@host DSN form must not leak POSTGRES_PASSWORD
    # into etl_run_log.detail even if a source surfaces a raw connection string.
    msg = "connection to server failed: postgresql://commodity:change_me@db:5432/commodity"
    out = redact_detail(msg)
    assert "change_me" not in out
    assert "commodity:***@db" in out  # scheme/user/host kept for debuggability
    # other URL schemes with userinfo are stripped too
    assert "s3kr3t" not in redact_detail("redis://user:s3kr3t@cache:6379/0")


def test_summarize_exception_is_bounded_and_redacted():
    secret = "supersecretkey9999"
    exc = RuntimeError(f"connect refused api_key={secret} host=db")
    summary = summarize_exception(exc)
    assert summary.startswith("RuntimeError:")
    assert secret not in summary
    assert len(summary) <= 500


def test_summarize_exception_truncates_long_message():
    exc = RuntimeError("x" * 2000)
    summary = summarize_exception(exc)
    assert len(summary) <= 500
    assert summary.endswith("…")


# --- dispatch wiring: heartbeat per source (AC#4/#5) ----------------------


class _RecordingHeartbeat:
    """In-memory heartbeat stand-in — captures rows, no DB."""

    def __init__(self, raise_on=None):
        self.rows = []
        self._raise_on = raise_on or set()

    def __call__(self, slot, source, run_date, status, **kwargs):
        if source in self._raise_on:
            raise RuntimeError("heartbeat DB hiccup")
        self.rows.append({"slot": slot, "source": source, "run_date": run_date,
                          "status": status, **kwargs})
        return True


def _runners(record, failing=()):
    def make(name):
        def fn():
            record.append(name)
            if name in failing:
                raise RuntimeError(f"{name} boom api_key=LEAKED1234")
        return fn

    return {n: make(n) for n in ("fred", "eia", "usda", "cftc", "curve_shape",
                                 "iv", "vol_indices", "prices")}


def test_dispatch_writes_success_heartbeat_per_source(schedule):
    record, hb = [], _RecordingHeartbeat()
    slot = schedule.slots["close-batch"]
    now = _et(2026, 6, 17, 16, 20)
    dispatch_slot(schedule, slot, now, _runners(record), heartbeat=hb)

    statuses = {(r["source"], r["status"]) for r in hb.rows}
    assert statuses == {("iv", STATUS_SUCCESS), ("vol_indices", STATUS_SUCCESS),
                        ("prices", STATUS_SUCCESS)}
    assert all(r["run_date"] == dt.date(2026, 6, 17) for r in hb.rows)
    for r in hb.rows:
        assert r["run_started_at"] is not None and r["run_finished_at"] is not None


def test_dispatch_writes_failure_heartbeat_with_redacted_detail(schedule):
    record, hb = [], _RecordingHeartbeat()
    slot = schedule.slots["close-batch"]
    dispatch_slot(schedule, slot, _et(2026, 6, 17, 16, 20),
                  _runners(record, failing={"vol_indices"}), heartbeat=hb)

    by_source = {r["source"]: r for r in hb.rows}
    assert by_source["iv"]["status"] == STATUS_SUCCESS
    assert by_source["prices"]["status"] == STATUS_SUCCESS
    fail = by_source["vol_indices"]
    assert fail["status"] == STATUS_FAILURE
    # The redacted summary must not leak the secret embedded in the error.
    assert "LEAKED1234" not in fail["detail"]
    assert "RuntimeError" in fail["detail"]


def test_dispatch_writes_skipped_heartbeat_for_every_guarded_source(schedule):
    # close-batch fired at a UTC-drifted ET evening: nothing runs, every source
    # is positively recorded as skipped (AC#4) — distinct from "never fired".
    record, hb = [], _RecordingHeartbeat()
    slot = schedule.slots["close-batch"]
    succeeded = dispatch_slot(schedule, slot, _et(2026, 6, 17, 19, 30),
                              _runners(record), heartbeat=hb)
    assert succeeded == []
    assert record == []
    assert {r["source"] for r in hb.rows} == {"iv", "vol_indices", "prices"}
    assert all(r["status"] == STATUS_SKIPPED for r in hb.rows)
    assert all(r["detail"] for r in hb.rows)  # the skip reason is recorded


def test_heartbeat_write_failure_does_not_abort_batch(schedule):
    # A heartbeat raising for one source must not stop the rest of the batch
    # (AC#5) and must not change which sources run (#23 AC#6 preserved).
    record = []
    hb = _RecordingHeartbeat(raise_on={"iv"})
    slot = schedule.slots["close-batch"]
    succeeded = dispatch_slot(schedule, slot, _et(2026, 6, 17, 16, 20),
                              _runners(record), heartbeat=hb)
    assert record == ["iv", "vol_indices", "prices"]   # all attempted
    assert succeeded == ["iv", "vol_indices", "prices"]  # iv's run() succeeded
    # iv's heartbeat raised (not recorded); the others still wrote.
    assert {r["source"] for r in hb.rows} == {"vol_indices", "prices"}


def test_default_heartbeat_is_best_effort_writer(monkeypatch, schedule):
    # With no heartbeat injected, dispatch uses run_log.write_run_log — and a DB
    # failure inside it must not abort the batch (the writer swallows it).
    calls = []

    def fake_writer(slot, source, run_date, status, **kwargs):
        calls.append(source)
        return False  # simulate a swallowed write failure

    monkeypatch.setattr(run_log, "write_run_log", fake_writer)
    record = []
    slot = schedule.slots["fred"]
    succeeded = dispatch_slot(schedule, slot, _et(2026, 6, 20, 8, 30), _runners(record))
    assert succeeded == ["fred"]
    assert calls == ["fred"]


# --- write_run_log best-effort tolerance (AC#5) --------------------------


def test_write_run_log_tolerates_missing_table(monkeypatch):
    # A pre-migration DB (or any write error) must degrade to a logged no-op
    # returning False, never raising.
    class _BoomEngine:
        def begin(self):
            raise RuntimeError("relation etl_run_log does not exist")

        def dispose(self):
            pass

    ok = run_log.write_run_log(
        "fred", "fred", dt.date(2026, 6, 22), STATUS_SUCCESS, engine=_BoomEngine()
    )
    assert ok is False


# --- live-Postgres-or-skip: real upsert idempotency (AC#7/#11) ------------


_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


@pytest.fixture
def live_engine(monkeypatch):
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    alembic_config = pytest.importorskip("alembic.config")
    alembic_command = pytest.importorskip("alembic.command")
    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for run-log tests")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    alembic_command.upgrade(cfg, "head")
    try:
        yield engine
    finally:
        engine.dispose()


def _count(engine, slot, source, run_date) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM etl_run_log "
                "WHERE slot=:s AND source=:src AND run_date=:d"
            ),
            {"s": slot, "src": source, "d": run_date},
        ).scalar_one()


def test_live_upsert_is_idempotent_on_natural_key(live_engine):
    run_date = dt.date(2026, 6, 22)
    slot, source = "test-slot", "test-source"
    with live_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM etl_run_log WHERE slot=:s AND source=:src"),
            {"s": slot, "src": source},
        )

    assert run_log.write_run_log(slot, source, run_date, STATUS_FAILURE,
                                 detail="first attempt", engine=live_engine)
    assert _count(live_engine, slot, source, run_date) == 1

    # Re-dispatch the same day: overwrites in place, no duplicate heartbeat.
    assert run_log.write_run_log(slot, source, run_date, STATUS_SUCCESS,
                                 detail="retry ok", engine=live_engine)
    assert _count(live_engine, slot, source, run_date) == 1

    with live_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, detail FROM etl_run_log "
                 "WHERE slot=:s AND source=:src AND run_date=:d"),
            {"s": slot, "src": source, "d": run_date},
        ).first()
    assert row[0] == STATUS_SUCCESS  # latest attempt wins
    assert row[1] == "retry ok"

    # A different day appends a new row (never overwrites another day).
    other = dt.date(2026, 6, 23)
    run_log.write_run_log(slot, source, other, STATUS_SUCCESS, engine=live_engine)
    assert _count(live_engine, slot, source, other) == 1

    with live_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM etl_run_log WHERE slot=:s AND source=:src"),
            {"s": slot, "src": source},
        )
