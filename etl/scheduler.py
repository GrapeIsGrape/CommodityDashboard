"""Target-agnostic ETL scheduling layer (#23).

Drives the existing per-source ``run()`` functions at named, ET-anchored slots.
The same ``etl`` image works three ways with no code change (CLAUDE.md §2):

* **Long-running in-process scheduler** (Docker Compose): :func:`run_forever`
  ticks once a minute, resolves the current wall-clock minute in
  ``America/New_York`` (DST-automatic via :mod:`zoneinfo`), and fires any slot
  whose minute matches.
* **One-shot single-slot CLI** (Railway cron / Synology DSM Task Scheduler):
  :func:`run_slot` fires one named slot then exits, so an external cron owns the
  timing.

The slot/session-window/weekday logic is **pure and clock-injectable** — it
takes the "current ET time" as an argument and never reads the wall clock — so
it is unit-tested without a real clock or network. Source dispatch keeps the
**per-source isolation** the sources already guarantee per symbol/series: one
source raising is caught, logged, and does not abort the rest of the slot.

The **session-window self-guard** (AC#4) is the portability backstop: the
timing-sensitive market-data batch (iv / vol_indices / prices / curve_shape)
only runs inside the valid ET close window. If a UTC-only external cron (or a
DST-drifted invocation) fires it outside that window, the guard skips the work
and logs why, rather than pulling a stale / zero-bid chain.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

from sqlalchemy import create_engine as _create_engine, text as _text

from common.config import get_database_url, load_scheduler_config
from etl import run_log

logger = logging.getLogger("etl.scheduler")

DEFAULT_TIMEZONE = "America/New_York"

DAYS_DAILY = "daily"
DAYS_WEEKDAYS = "weekdays"


def parse_hhmm(value: str) -> dt.time:
    """Parse a ``"HH:MM"`` config string into a :class:`datetime.time`."""
    hour, minute = value.split(":")
    return dt.time(int(hour), int(minute))


@dataclass(frozen=True)
class Slot:
    """One scheduled slot: a name, an ET fire-time, a day-of-week rule, whether
    it is session-guarded, and the source modules it dispatches."""

    name: str
    at: dt.time
    days: str
    session_guarded: bool
    sources: tuple[str, ...]

    def fires_on_weekday(self, weekday: int) -> bool:
        """Should this slot run on the given weekday (Mon=0 … Sun=6)?

        Weekday-only slots (the market-data batch) are skipped Sat/Sun;
        release-driven (daily) slots fire every day so holiday-shifted releases
        self-absorb via the idempotent poll (AC#5/AC#7)."""
        if self.days == DAYS_WEEKDAYS:
            return weekday < 5
        return True


@dataclass(frozen=True)
class SessionWindow:
    """The ET wall-clock window inside which a session-guarded run is valid."""

    open: dt.time
    close: dt.time

    def contains(self, when: dt.time) -> bool:
        return self.open <= when <= self.close


@dataclass(frozen=True)
class Schedule:
    """The full parsed schedule: timezone, session window, and slots by name."""

    timezone: str
    session_window: SessionWindow
    slots: dict[str, Slot]


def build_schedule(config: Mapping, tz_override: Optional[str] = None) -> Schedule:
    """Build a :class:`Schedule` from a raw config mapping (pure — no I/O)."""
    window = config.get("session_window", {})
    session_window = SessionWindow(
        open=parse_hhmm(window.get("open", "15:25")),
        close=parse_hhmm(window.get("close", "16:35")),
    )
    slots: dict[str, Slot] = {}
    for name, spec in (config.get("slots") or {}).items():
        slots[name] = Slot(
            name=name,
            at=parse_hhmm(spec["at"]),
            days=spec.get("days", DAYS_DAILY),
            session_guarded=bool(spec.get("session_guarded", False)),
            sources=tuple(spec["sources"]),
        )
    timezone = tz_override or config.get("timezone") or DEFAULT_TIMEZONE
    return Schedule(timezone=timezone, session_window=session_window, slots=slots)


def load_schedule() -> Schedule:
    """Load the schedule from config, honouring the ETL_TZ env override."""
    return build_schedule(load_scheduler_config(), tz_override=os.environ.get("ETL_TZ"))


def in_session_window(schedule: Schedule, now_et: dt.datetime) -> bool:
    """AC#4 guard: is ``now_et`` a valid ET session-window moment for the
    timing-sensitive market-data batch? Weekday + inside the ET close window.

    Pure and clock-injectable: ``now_et`` is an ET-localized datetime supplied by
    the caller. Holiday-tolerant by design — it does not consult a holiday
    calendar; an accidental holiday run is absorbed downstream by the sources'
    honest-NULL / no-row behavior (AC#5, edge cases)."""
    if now_et.weekday() >= 5:
        return False
    return schedule.session_window.contains(now_et.time())


def slots_due_at(schedule: Schedule, now_et: dt.datetime) -> list[Slot]:
    """The slots whose fire-minute matches ``now_et`` and whose day-of-week rule
    admits today. Pure — used by the in-process tick loop and by tests."""
    due: list[Slot] = []
    for slot in schedule.slots.values():
        if slot.at.hour == now_et.hour and slot.at.minute == now_et.minute:
            if slot.fires_on_weekday(now_et.weekday()):
                due.append(slot)
    return due


# Source dispatch ----------------------------------------------------------

SourceRunner = Callable[[], None]

# Signature: (slot, source, run_date, status, *, run_started_at, run_finished_at,
# detail). Defaults to the best-effort DB writer; tests inject a mock so the pure
# dispatch logic stays DB-free.
Heartbeat = Callable[..., bool]


def _default_source_runners() -> dict[str, SourceRunner]:
    """Resolve each source module's existing ``run()`` entrypoint, imported
    lazily so the pure scheduling logic (and its tests) never pull in
    yfinance/requests. Keyed by the module name used in config ``sources:``."""
    from etl.sources import cftc, curve_shape, eia, fred, iv, prices, usda, vol_indices

    return {
        "fred": fred.run,
        "eia": eia.run,
        "usda": usda.run,
        "cftc": cftc.run,
        "curve_shape": curve_shape.run,
        "iv": iv.run,
        "vol_indices": vol_indices.run,
        "prices": prices.run,
    }


def dispatch_slot(
    schedule: Schedule,
    slot: Slot,
    now_et: dt.datetime,
    runners: Mapping[str, SourceRunner],
    heartbeat: Optional[Heartbeat] = None,
) -> list[str]:
    """Run every source in ``slot`` with per-source isolation (AC#6).

    If the slot is session-guarded and ``now_et`` is outside the valid ET window,
    the whole batch is skipped and logged (AC#4) — nothing runs, and every source
    is positively recorded as ``skipped`` in the run-log so "guard skipped it" is
    distinguishable from "never fired" (#24 AC#4). Otherwise each source's
    ``run()`` is invoked in turn; one raising is caught + logged and the remaining
    sources still execute (AC#6). Idempotency is the sources' own
    upsert-on-natural-key (AC#7), so a re-dispatch is safe.

    A run-log row is written per source per dispatch (#24): ``success`` on return,
    ``failure`` with a short redacted ``detail`` on raise, ``skipped`` on the
    guard path. The ``heartbeat`` write is **best-effort** — defaulted to the DB
    writer but injectable for tests — and a failing heartbeat is itself swallowed
    inside the writer so it can never abort the batch (#24 AC#5). The ET schedule
    day is ``now_et.date()`` so a late-ET run crossing UTC midnight still books to
    the correct day.

    Returns the list of source names that completed without raising (mostly for
    observability / tests)."""
    heartbeat = heartbeat if heartbeat is not None else run_log.write_run_log
    run_date = now_et.date()

    if slot.session_guarded and not in_session_window(schedule, now_et):
        logger.warning(
            "Slot %s skipped: %s ET is outside the valid session window %s–%s "
            "(weekday market-data batch). Sources %s not run — refusing a "
            "stale/zero-bid chain (AC#4).",
            slot.name,
            now_et.strftime("%Y-%m-%d %H:%M %Z"),
            schedule.session_window.open.strftime("%H:%M"),
            schedule.session_window.close.strftime("%H:%M"),
            list(slot.sources),
        )
        skip_detail = (
            f"session guard: {now_et.strftime('%H:%M %Z')} outside "
            f"{schedule.session_window.open.strftime('%H:%M')}–"
            f"{schedule.session_window.close.strftime('%H:%M')} ET"
        )
        for name in slot.sources:
            _record_heartbeat(
                heartbeat,
                slot.name,
                name,
                run_date,
                run_log.STATUS_SKIPPED,
                detail=skip_detail,
            )
        return []

    succeeded: list[str] = []
    for name in slot.sources:
        runner = runners.get(name)
        if runner is None:
            logger.error("Slot %s references unknown source %r; skipping.", slot.name, name)
            continue
        logger.info("Slot %s: running source %s...", slot.name, name)
        started_at = run_log.now_utc()
        try:
            runner()
            succeeded.append(name)
            _record_heartbeat(
                heartbeat,
                slot.name,
                name,
                run_date,
                run_log.STATUS_SUCCESS,
                run_started_at=started_at,
                run_finished_at=run_log.now_utc(),
            )
        except Exception as exc:
            logger.exception(
                "Slot %s: source %s failed; continuing with the rest of the slot.",
                slot.name,
                name,
            )
            _record_heartbeat(
                heartbeat,
                slot.name,
                name,
                run_date,
                run_log.STATUS_FAILURE,
                run_started_at=started_at,
                run_finished_at=run_log.now_utc(),
                detail=run_log.summarize_exception(exc),
            )
    logger.info(
        "Slot %s complete: %d/%d sources succeeded.",
        slot.name,
        len(succeeded),
        len(slot.sources),
    )
    return succeeded


def _record_heartbeat(heartbeat: Heartbeat, slot: str, source: str, run_date: dt.date,
                      status: str, **kwargs) -> None:
    """Invoke the heartbeat writer, isolating it so even an unexpected raise in a
    custom writer cannot abort the source batch (#24 AC#5)."""
    try:
        heartbeat(slot, source, run_date, status, **kwargs)
    except Exception:
        logger.warning(
            "Run-log heartbeat invocation raised for slot=%s source=%s; "
            "ignored (observability only).",
            slot,
            source,
            exc_info=True,
        )


def run_slot(
    slot_name: str,
    runners: Optional[Mapping[str, SourceRunner]] = None,
    schedule: Optional[Schedule] = None,
    now_et: Optional[dt.datetime] = None,
) -> list[str]:
    """Fire a single named slot once and return — the one-shot CLI mode used by
    an external cron (Railway / DSM). ``now_et`` defaults to the real current ET
    time so the session guard still applies even under a UTC cron."""
    schedule = schedule or load_schedule()
    runners = runners if runners is not None else _default_source_runners()
    if slot_name not in schedule.slots:
        raise SystemExit(
            f"Unknown slot {slot_name!r}. Configured slots: {sorted(schedule.slots)}"
        )
    now_et = now_et or dt.datetime.now(ZoneInfo(schedule.timezone))
    return dispatch_slot(schedule, schedule.slots[slot_name], now_et, runners)


_TRIGGER_POLL_SQL = "SELECT id, slot FROM etl_manual_trigger WHERE processed_at IS NULL LIMIT 1"
_TRIGGER_MARK_SQL = "UPDATE etl_manual_trigger SET processed_at = now() WHERE id = :id"

# Tracks whether a trigger-table error has already been logged at WARNING so the
# loop does not spam the log on every tick when the table is absent or the DB is
# temporarily down (AC#14).
_trigger_error_logged = False


def _check_manual_trigger(
    schedule: Schedule,
    runners: Mapping[str, SourceRunner],
    now_et: dt.datetime,
    heartbeat: Optional[Heartbeat] = None,
    engine: "Optional[Engine]" = None,
) -> None:
    """Poll ``etl_manual_trigger`` for unprocessed rows; dispatch all slots if found.

    Called at the top of each ``run_forever`` tick (AC#11).  When an unprocessed
    row is found all configured slots are dispatched using the same
    :func:`dispatch_slot` machinery as a scheduled tick (AC#12).  Session-guarded
    slots still respect the ET session-window guard — a manual trigger outside
    the window produces honest-NULL rows, not fabricated data (AC#12).  After all
    dispatches the trigger row is marked ``processed_at = now()`` (AC#13).

    DB errors (``OperationalError`` / ``ProgrammingError``) are swallowed after
    logging once at WARNING so the scheduler loop continues uninterrupted and the
    log is not flooded on every tick (AC#14).
    """
    global _trigger_error_logged
    own_engine = engine is None
    try:
        if engine is None:
            engine = _create_engine(get_database_url(), pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                row = conn.execute(_text(_TRIGGER_POLL_SQL)).first()
            if row is None:
                return
            trigger_id = row[0]
            logger.info(
                "Manual ETL trigger received (id=%s slot=%s) — running all slots.",
                trigger_id, row[1],
            )
            _trigger_error_logged = False
            # Mark processed BEFORE dispatching so a second scheduler tick that
            # fires while slots are running does not see this row as unprocessed
            # and double-fire all ETL sources (sec-audit HIGH finding).
            with engine.begin() as conn:
                conn.execute(_text(_TRIGGER_MARK_SQL), {"id": trigger_id})
            logger.info("Manual trigger id=%s marked processed; dispatching slots.", trigger_id)
            for slot in schedule.slots.values():
                dispatch_slot(schedule, slot, now_et, runners, heartbeat=heartbeat)
        finally:
            if own_engine:
                engine.dispose()
    except Exception:
        if not _trigger_error_logged:
            logger.warning(
                "Manual trigger poll failed; skipping this tick. "
                "Loop continues uninterrupted (AC#14).",
                exc_info=True,
            )
            _trigger_error_logged = True


def run_forever(
    runners: Optional[Mapping[str, SourceRunner]] = None,
    schedule: Optional[Schedule] = None,
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Optional[Callable[[], dt.datetime]] = None,
    max_ticks: Optional[int] = None,
    engine: "Optional[Engine]" = None,
) -> None:
    """Long-running in-process scheduler (Compose mode): wake once a minute,
    resolve the current ET minute, and dispatch any due slot.

    Dedupes within a minute so a slot fires at most once per calendar minute even
    if the loop wakes twice. ``now_fn``/``sleep``/``max_ticks`` are injectable so
    the loop is testable without real time."""
    schedule = schedule or load_schedule()
    runners = runners if runners is not None else _default_source_runners()
    tz = ZoneInfo(schedule.timezone)
    now_fn = now_fn or (lambda: dt.datetime.now(tz))

    logger.info(
        "ETL in-process scheduler started (tz=%s). Slots: %s",
        schedule.timezone,
        {name: slot.at.strftime("%H:%M") for name, slot in schedule.slots.items()},
    )

    last_fired_minute: Optional[tuple[int, int, int, int, int]] = None
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        now_et = now_fn()
        _check_manual_trigger(schedule, runners, now_et, engine=engine)
        minute_key = (now_et.year, now_et.month, now_et.day, now_et.hour, now_et.minute)
        if minute_key != last_fired_minute:
            for slot in slots_due_at(schedule, now_et):
                dispatch_slot(schedule, slot, now_et, runners)
            last_fired_minute = minute_key
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        sleep(_seconds_to_next_minute(now_et))


def _seconds_to_next_minute(now: dt.datetime) -> float:
    """Seconds until the next wall-clock minute boundary (caps loop drift)."""
    return max(1.0, 60.0 - now.second - now.microsecond / 1_000_000.0)
