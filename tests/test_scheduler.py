"""Tests for the ETL scheduling layer (etl/scheduler.py + etl/run.py — #23).

All pure scheduling logic is exercised WITHOUT a real clock or network
(clock-injectable, sources mocked), mirroring the panels' pure-logic pattern:

* slot selection — the right sources fire at each ET slot minute;
* weekday-only gating — the market-data batch is skipped Sat/Sun, release feeds
  still fire daily;
* the ET session-window guard's accept/skip decision (incl. the UTC-drift and
  holiday-tolerance paths);
* per-source isolation — one source's run() raising does not abort the others;
* migrations-on-boot is preserved (boot calls migrations before scheduling).

The real source run() functions are never imported here — runners are injected.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from common.config import load_scheduler_config
from etl import run as etl_run
from etl import scheduler
from etl.scheduler import (
    DAYS_DAILY,
    DAYS_WEEKDAYS,
    Schedule,
    SessionWindow,
    Slot,
    build_schedule,
    dispatch_slot,
    in_session_window,
    parse_hhmm,
    slots_due_at,
)

ET = ZoneInfo("America/New_York")


@pytest.fixture
def schedule() -> Schedule:
    """The real shipped config, parsed — the canonical AC#2 cadence."""
    return build_schedule(load_scheduler_config())


def _et(year, month, day, hour, minute) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=ET)


# --- config / parsing -----------------------------------------------------


def test_parse_hhmm():
    assert parse_hhmm("08:30") == dt.time(8, 30)
    assert parse_hhmm("16:20") == dt.time(16, 20)


def test_shipped_config_matches_canonical_cadence(schedule):
    expected = {
        "fred": (dt.time(8, 30), DAYS_DAILY, False, ("fred",)),
        "eia": (dt.time(11, 0), DAYS_DAILY, False, ("eia",)),
        "usda": (dt.time(12, 15), DAYS_DAILY, False, ("usda",)),
        "curve": (dt.time(15, 30), DAYS_WEEKDAYS, True, ("curve_shape",)),
        "cftc": (dt.time(16, 0), DAYS_DAILY, False, ("cftc",)),
        "close-batch": (dt.time(16, 20), DAYS_WEEKDAYS, True, ("iv", "vol_indices", "prices")),
    }
    assert set(schedule.slots) == set(expected)
    for name, (at, days, guarded, sources) in expected.items():
        slot = schedule.slots[name]
        assert slot.at == at
        assert slot.days == days
        assert slot.session_guarded is guarded
        assert slot.sources == sources


def test_timezone_is_et_not_a_utc_offset(schedule):
    assert schedule.timezone == "America/New_York"


def test_tz_override_applied():
    sched = build_schedule(load_scheduler_config(), tz_override="UTC")
    assert sched.timezone == "UTC"


# --- slot selection (AC#2/#11) -------------------------------------------


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (8, 30, {"fred"}),
        (11, 0, {"eia"}),
        (12, 15, {"usda"}),
        (15, 30, {"curve"}),
        (16, 0, {"cftc"}),
        (16, 20, {"close-batch"}),
        (9, 0, set()),  # no slot at this minute
    ],
)
def test_slots_due_at_picks_the_right_slot(schedule, hour, minute, expected):
    # A Wednesday (weekday) so weekday-gated slots are admitted.
    now = _et(2026, 6, 17, hour, minute)
    assert {s.name for s in slots_due_at(schedule, now)} == expected


# --- weekday gating (AC#5/#11) -------------------------------------------


def test_market_data_batch_skipped_on_weekend(schedule):
    saturday = _et(2026, 6, 20, 16, 20)
    sunday = _et(2026, 6, 21, 15, 30)
    assert slots_due_at(schedule, saturday) == []
    assert slots_due_at(schedule, sunday) == []


def test_release_feeds_fire_on_weekend(schedule):
    saturday = _et(2026, 6, 20, 8, 30)  # FRED slot, daily
    assert {s.name for s in slots_due_at(schedule, saturday)} == {"fred"}


def test_slot_fires_on_weekday_rule():
    weekday_only = Slot("x", dt.time(16, 20), DAYS_WEEKDAYS, True, ("iv",))
    daily = Slot("y", dt.time(8, 30), DAYS_DAILY, False, ("fred",))
    for wd in range(5):  # Mon-Fri
        assert weekday_only.fires_on_weekday(wd)
    for wd in (5, 6):  # Sat, Sun
        assert not weekday_only.fires_on_weekday(wd)
    for wd in range(7):
        assert daily.fires_on_weekday(wd)


# --- session-window guard (AC#4/#11) -------------------------------------


def test_session_window_accepts_inside_weekday_window(schedule):
    # Both session-guarded slot times must be inside the valid window.
    assert in_session_window(schedule, _et(2026, 6, 17, 15, 30))  # curve
    assert in_session_window(schedule, _et(2026, 6, 17, 16, 20))  # close batch


def test_session_window_rejects_utc_midnight_drift(schedule):
    # A 00:00 UTC cron lands ~19:00-20:00 ET — the #1 trap. The ET-localized
    # equivalent must be rejected so iv.py is never handed a dead chain.
    utc_midnight = dt.datetime(2026, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))
    now_et = utc_midnight.astimezone(ET)
    assert now_et.hour in (19, 20)
    assert not in_session_window(schedule, now_et)


def test_session_window_rejects_weekend(schedule):
    # Holiday-tolerant by the same mechanism: weekend (and, by extension, an
    # accidental holiday close-batch) is rejected without a calendar.
    assert not in_session_window(schedule, _et(2026, 6, 20, 16, 20))  # Saturday


def test_session_window_rejects_off_hours_weekday(schedule):
    assert not in_session_window(schedule, _et(2026, 6, 17, 9, 30))   # morning
    assert not in_session_window(schedule, _et(2026, 6, 17, 18, 5))   # post-Globex


# --- dispatch: guard + per-source isolation (AC#4/#6/#11) -----------------


def _runners(record, failing=()):
    def make(name):
        def fn():
            record.append(name)
            if name in failing:
                raise RuntimeError(f"{name} boom")
        return fn

    return {n: make(n) for n in ("fred", "eia", "usda", "cftc", "curve_shape",
                                 "iv", "vol_indices", "prices")}


def test_dispatch_runs_all_sources_in_slot(schedule):
    record = []
    slot = schedule.slots["close-batch"]
    succeeded = dispatch_slot(schedule, slot, _et(2026, 6, 17, 16, 20), _runners(record))
    assert record == ["iv", "vol_indices", "prices"]
    assert succeeded == ["iv", "vol_indices", "prices"]


def test_dispatch_per_source_isolation(schedule):
    # vol_indices raises; iv and prices still run, error swallowed (AC#6).
    record = []
    slot = schedule.slots["close-batch"]
    succeeded = dispatch_slot(
        schedule, slot, _et(2026, 6, 17, 16, 20), _runners(record, failing={"vol_indices"})
    )
    assert record == ["iv", "vol_indices", "prices"]  # all three attempted
    assert succeeded == ["iv", "prices"]               # the failure excluded


def test_dispatch_session_guard_skips_out_of_window(schedule):
    # close-batch invoked at a UTC-drifted ET evening time: nothing runs (AC#4).
    record = []
    slot = schedule.slots["close-batch"]
    evening = _et(2026, 6, 17, 19, 30)
    succeeded = dispatch_slot(schedule, slot, evening, _runners(record))
    assert record == []
    assert succeeded == []


def test_dispatch_unguarded_slot_runs_off_window(schedule):
    # A release-driven (not session-guarded) slot runs regardless of the window.
    record = []
    slot = schedule.slots["fred"]
    succeeded = dispatch_slot(schedule, slot, _et(2026, 6, 20, 8, 30), _runners(record))
    assert record == ["fred"]
    assert succeeded == ["fred"]


def test_dispatch_unknown_source_is_skipped_not_fatal(schedule):
    slot = Slot("bad", dt.time(8, 30), DAYS_DAILY, False, ("nope", "fred"))
    record = []
    succeeded = dispatch_slot(schedule, slot, _et(2026, 6, 17, 8, 30), _runners(record))
    assert record == ["fred"]
    assert succeeded == ["fred"]


# --- run_slot one-shot CLI (AC#9) ----------------------------------------


def test_run_slot_fires_named_slot(schedule):
    record = []
    succeeded = scheduler.run_slot(
        "fred",
        runners=_runners(record),
        schedule=schedule,
        now_et=_et(2026, 6, 17, 8, 30),
    )
    assert record == ["fred"]
    assert succeeded == ["fred"]


def test_run_slot_rejects_unknown_slot(schedule):
    with pytest.raises(SystemExit):
        scheduler.run_slot("does-not-exist", runners={}, schedule=schedule,
                           now_et=_et(2026, 6, 17, 8, 30))


def test_run_slot_session_guard_skips_evening_invocation(schedule):
    # An external cron that fires close-batch at a drifted ET evening: the guard
    # skips in one-shot CLI mode too (now_et passed explicitly, as run_slot's
    # default real-clock path would in production).
    record = []
    succeeded = scheduler.run_slot(
        "close-batch",
        runners=_runners(record),
        schedule=schedule,
        now_et=_et(2026, 6, 17, 19, 30),
    )
    assert record == []
    assert succeeded == []


# --- in-process loop (AC#1) ----------------------------------------------


def test_run_forever_fires_due_slot_then_stops(schedule):
    record = []
    times = iter([
        _et(2026, 6, 17, 8, 30),   # FRED slot → fires
        _et(2026, 6, 17, 8, 31),   # nothing
    ])
    scheduler.run_forever(
        runners=_runners(record),
        schedule=schedule,
        sleep=lambda _s: None,
        now_fn=lambda: next(times),
        max_ticks=2,
    )
    assert record == ["fred"]


def test_run_forever_dedupes_within_a_minute(schedule):
    record = []
    same_minute = _et(2026, 6, 17, 11, 0)
    times = iter([same_minute, same_minute, _et(2026, 6, 17, 11, 1)])
    scheduler.run_forever(
        runners=_runners(record),
        schedule=schedule,
        sleep=lambda _s: None,
        now_fn=lambda: next(times),
        max_ticks=3,
    )
    assert record == ["eia"]  # EIA fired once despite two ticks in the minute


# --- migrations-on-boot preserved (AC#1) ---------------------------------


def test_boot_applies_migrations_before_scheduling(monkeypatch):
    order = []
    monkeypatch.setattr(etl_run, "apply_migrations", lambda: order.append("migrate"))
    monkeypatch.setattr(etl_run.scheduler, "run_forever", lambda *a, **k: order.append("schedule"))
    etl_run.main([])
    assert order == ["migrate", "schedule"]


def test_boot_one_shot_slot_mode(monkeypatch):
    order = []
    monkeypatch.setattr(etl_run, "apply_migrations", lambda: order.append("migrate"))
    monkeypatch.setattr(etl_run.scheduler, "run_slot", lambda name: order.append(f"slot:{name}"))
    monkeypatch.setattr(etl_run.scheduler, "run_forever", lambda *a, **k: order.append("forever"))
    etl_run.main(["--slot", "close-batch"])
    assert order == ["migrate", "slot:close-batch"]
