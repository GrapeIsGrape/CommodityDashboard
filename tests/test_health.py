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
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    # now_et is a Wednesday 17:00 — 16:20 slot has fired, last weekday = Wednesday
    now_et = _dt.datetime(2026, 6, 24, 17, 0, tzinfo=ET)
    # run_date = today's date → not stale for a weekdays cadence
    cadence_map = {("close-batch", "iv"): ("weekdays", "16:20")}
    row = {
        "slot": "close-batch",
        "source": "iv",
        "run_date": _dt.date(2026, 6, 24),
        "run_finished_at": _dt.datetime(2026, 6, 22, 20, 22, tzinfo=_dt.timezone.utc),
        "last_status": "failure",
        # last success pre-dates the failed last attempt
        "last_success_run_date": _dt.date(2026, 6, 21),
        "last_success_finished_at": _dt.datetime(2026, 6, 21, 20, 22, tzinfo=_dt.timezone.utc),
    }
    shaped = dm._shape_etl_row(row, cadence_map, now_et)
    assert shaped["slot"] == "close-batch"
    assert shaped["source"] == "iv"
    assert shaped["last_status"] == "failure"
    assert shaped["run_date"] == "2026-06-24"
    assert shaped["run_finished_at"] == "2026-06-22T20:22:00+00:00"
    # last success is older than the last (failed) attempt — surfaced distinctly.
    assert shaped["last_success_run_date"] == "2026-06-21"
    # stale field is present and typed correctly
    assert "stale" in shaped
    assert shaped["stale"] is False  # run_date == last_expected_weekday


def test_shape_etl_row_passes_through_nulls(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now_et = _dt.datetime(2026, 6, 24, 17, 0, tzinfo=ET)
    cadence_map = {("close-batch", "iv"): ("weekdays", "16:20")}
    row = {
        "slot": "close-batch",
        "source": "iv",
        "run_date": _dt.date(2026, 6, 22),
        "run_finished_at": None,
        "last_status": "skipped",
        "last_success_run_date": None,   # never succeeded
        "last_success_finished_at": None,
    }
    shaped = dm._shape_etl_row(row, cadence_map, now_et)
    assert shaped["run_finished_at"] is None
    assert shaped["last_success_run_date"] is None
    assert shaped["last_success_finished_at"] is None
    assert shaped["last_status"] == "skipped"
    assert "stale" in shaped


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


# --- #25: is_etl_source_stale pure clock-injectable tests -------------------

def _et_now(year, month, day, hour=12, minute=0):
    from zoneinfo import ZoneInfo
    return _dt.datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))


def test_stale_weekday_friday_run_saturday_now(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # Friday run, Saturday now — last expected weekday = Friday → not stale.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 19),   # Friday
        "weekdays",
        _et_now(2026, 6, 20),    # Saturday
    ) is False


def test_stale_weekday_friday_run_sunday_now(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # Friday run, Sunday now — last expected weekday = Friday → not stale.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 19),   # Friday
        "weekdays",
        _et_now(2026, 6, 21),    # Sunday
    ) is False


def test_stale_weekday_friday_run_monday_morning_not_stale(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # Friday run, Monday 08:00 ET — 16:20 slot has NOT fired yet.
    # Expected datum = last_expected_session(Monday) = Friday → not stale.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 19),   # Friday
        "weekdays",
        _et_now(2026, 6, 22, 8, 0),   # Monday 08:00 ET
        slot_time_str="16:20",
    ) is False


def test_stale_weekday_friday_run_monday_evening_stale(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # Friday run, Monday 17:00 ET — 16:20 slot has fired.
    # Expected datum = _last_trading_session(Monday) = Monday → stale.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 19),   # Friday
        "weekdays",
        _et_now(2026, 6, 22, 17, 0),  # Monday 17:00 ET
        slot_time_str="16:20",
    ) is True


def test_stale_weekday_friday_run_monday_no_slot_time_old_behavior(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # No slot_time_str → graceful degradation to old model:
    # Monday is a trading session → expected = Monday → Friday < Monday → stale.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 19),   # Friday
        "weekdays",
        _et_now(2026, 6, 22, 8, 0),   # Monday 08:00 ET
        slot_time_str=None,
    ) is True


def test_stale_weekday_malformed_slot_time_degrades_gracefully(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # A malformed slot_time_str (not parseable) must not raise — falls back to
    # the old _last_trading_session model (slightly eager STALE on Mon morning).
    for bad in ("NOT_A_TIME", "25:99", 1620, "", "16:20:00"):
        result = dm.is_etl_source_stale(
            _dt.date(2026, 6, 19),  # Friday
            "weekdays",
            _et_now(2026, 6, 22, 8, 0),  # Monday 08:00 ET
            slot_time_str=bad,
        )
        assert isinstance(result, bool), f"expected bool, got {result!r} for {bad!r}"


def test_stale_weekday_missed_session(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # run_date is two weekdays ago — clearly stale.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 17),   # Wednesday
        "weekdays",
        _et_now(2026, 6, 19),    # Friday (expected = Friday, run_date < Friday)
    ) is True


def test_stale_daily_within_grace(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # age = 1 day — within grace (> 2 triggers stale).
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 23),
        "daily",
        _et_now(2026, 6, 24),
    ) is False


def test_stale_daily_outside_grace(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # age = 3 days — outside grace.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 21),
        "daily",
        _et_now(2026, 6, 24),
    ) is True


def test_stale_daily_grace_boundary_exactly_2_days(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # age == _DAILY_GRACE_DAYS (2) → not stale (condition is strict >).
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 22),
        "daily",
        _et_now(2026, 6, 24),
    ) is False


def test_stale_daily_grace_boundary_just_over(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # age == 3 (one past the grace) → stale.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 6, 21),
        "daily",
        _et_now(2026, 6, 24),
    ) is True


def test_stale_weekday_holiday_monday_friday_run_not_stale(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # 2026-01-19 is MLK Day (Monday, a US market holiday).
    # Friday run (2026-01-16) + holiday-Monday now → _last_trading_session rolls
    # back to Friday 2026-01-16 (the last real session).
    # run_date (Friday) < expected (Friday) → False (NOT stale).
    # The old _last_expected_weekday would have returned Monday 2026-01-19
    # (not holiday-aware), making this incorrectly stale.
    assert dm.is_etl_source_stale(
        _dt.date(2026, 1, 16),    # Friday before MLK Day
        "weekdays",
        _et_now(2026, 1, 19),     # MLK Day Monday (holiday)
    ) is False


def test_stale_never_ran_returns_none(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    # None run_date → None (never_ran takes precedence; must not read as stale=False).
    result = dm.is_etl_source_stale(None, "daily", _et_now(2026, 6, 24))
    assert result is None
    result2 = dm.is_etl_source_stale(None, "weekdays", _et_now(2026, 6, 24))
    assert result2 is None


def test_shape_etl_row_stale_flag_present_and_typed(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    # Wednesday 2026-06-24 17:00 — 16:20 slot has fired, last weekday = Wednesday
    now_et = _dt.datetime(2026, 6, 24, 17, 0, tzinfo=ET)
    cadence_map = {("close-batch", "iv"): ("weekdays", "16:20")}

    # Fresh run (same day) → stale=False
    row_fresh = {
        "slot": "close-batch", "source": "iv",
        "run_date": _dt.date(2026, 6, 24),
        "run_finished_at": None,
        "last_status": "success",
        "last_success_run_date": _dt.date(2026, 6, 24),
        "last_success_finished_at": None,
    }
    shaped = dm._shape_etl_row(row_fresh, cadence_map, now_et)
    assert "stale" in shaped
    assert shaped["stale"] is False

    # Stale run (Monday, now is Wednesday 17:00 — slot fired) → stale=True
    row_stale = {
        "slot": "close-batch", "source": "iv",
        "run_date": _dt.date(2026, 6, 22),
        "run_finished_at": None,
        "last_status": "success",
        "last_success_run_date": _dt.date(2026, 6, 22),
        "last_success_finished_at": None,
    }
    shaped_stale = dm._shape_etl_row(row_stale, cadence_map, now_et)
    assert shaped_stale["stale"] is True

    # Unknown cadence (slot/source not in map) → stale=None
    row_unknown = dict(row_fresh)
    row_unknown["slot"] = "unknown-slot"
    row_unknown["source"] = "unknown-source"
    shaped_unknown = dm._shape_etl_row(row_unknown, cadence_map, now_et)
    assert shaped_unknown["stale"] is None


def test_never_ran_row_stale_is_none(monkeypatch):
    dm = _dashboard_main(monkeypatch)
    row = dm._never_ran_row("fred", "fred")
    assert "stale" in row
    assert row["stale"] is None


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
    # /health now renders an HTML page (#29); verify key info is present.
    assert "text/html" in resp.headers.get("content-type", "")
    assert expected_head in resp.text
    # The DB-reachable state and ETL summary section are present.
    assert "reachable" in resp.text
    # The trigger form is present when the table is migrated (AC#6).
    assert "Run all ETL now" in resp.text


def test_health_etl_summary_surfaces_latest_run_per_source(health_client):
    client, _ = health_client

    # Seed two attempts for one (slot, source): an older success then a newer
    # failure, so last_status=failure but last_success still points at the older.
    # The HTML page renders both statuses in its ETL table.
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

        # The /health helper is the source of truth — test via _read_etl_summary
        # directly (which is already tested; this verifies seeded data round-trips).
        import dashboard.main as dashboard_main
        with engine.connect() as conn:
            rows = dashboard_main._read_etl_summary(conn)
        entry = next(e for e in rows if e["slot"] == slot and e["source"] == source)
        assert entry["last_status"] == "failure"        # newest attempt
        assert entry["run_date"] == "2026-06-22"
        assert entry["last_success_run_date"] == "2026-06-21"  # older success retained
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM etl_run_log WHERE slot=:s AND source=:src"),
                         {"s": slot, "src": source})
        engine.dispose()


def test_health_etl_summary_surfaces_never_ran_for_configured_source(health_client):
    _, _ = health_client

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

        import dashboard.main as dashboard_main
        with engine.connect() as conn:
            rows = dashboard_main._read_etl_summary(conn)
        entry = next(e for e in rows if e["slot"] == slot and e["source"] == source)
        assert entry["last_status"] == "never_ran"
        assert entry["run_date"] is None
        assert entry["last_success_run_date"] is None
        # #25: stale key is present; never_ran → None (not False, not True)
        assert "stale" in entry
        assert entry["stale"] is None
    finally:
        engine.dispose()


def test_health_etl_summary_stale_key_present_on_all_rows(health_client):
    """Every row in the /health etl summary carries a 'stale' key (#25 AC#5/10).

    The ``now_et`` clock is injected directly into ``_read_etl_summary`` via the
    live connection so the staleness verdict is deterministic regardless of when
    the test runs.
    """
    _, _ = health_client
    import datetime as dtime
    from zoneinfo import ZoneInfo

    from common.config import get_database_url
    from sqlalchemy import create_engine

    engine = create_engine(get_database_url())
    # A fixed recent weekday so freshly-inserted test rows are never stale.
    now_et = dtime.datetime(2026, 6, 24, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    import dashboard.main as dashboard_main

    try:
        with engine.connect() as conn:
            result = dashboard_main._read_etl_summary(conn, now_et=now_et)
        assert isinstance(result, list)
        for row in result:
            assert "stale" in row, f"'stale' missing from row {row}"
            assert row["stale"] is None or isinstance(row["stale"], bool), (
                f"stale must be None or bool, got {type(row['stale'])} in {row}"
            )
    finally:
        engine.dispose()
