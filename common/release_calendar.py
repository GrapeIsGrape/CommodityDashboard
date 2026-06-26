"""Pure release-calendar helpers for the 7-day event strip on the index page.

All time-sensitive functions accept a ``now_et`` (timezone-aware datetime in
America/New_York) for clock injection in tests — never call datetime.now()
internally. Stdlib-only; safe to import from both the dashboard image and the
etl image without triggering any third-party or etl/ import.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# US federal holidays relevant to EIA/NFP/COT release shifts.
# Covers 2025–2027; add the next year when 2027 events are within the 7-day window.
_FEDERAL_HOLIDAYS: frozenset[dt.date] = frozenset({
    dt.date(2025, 1, 1),   # New Year's
    dt.date(2025, 1, 20),  # MLK Day
    dt.date(2025, 2, 17),  # Presidents' Day
    dt.date(2025, 5, 26),  # Memorial Day
    dt.date(2025, 6, 19),  # Juneteenth
    dt.date(2025, 7, 4),   # Independence Day
    dt.date(2025, 9, 1),   # Labor Day
    dt.date(2025, 11, 11), # Veterans Day
    dt.date(2025, 11, 27), # Thanksgiving
    dt.date(2025, 12, 25), # Christmas
    dt.date(2026, 1, 1),   # New Year's
    dt.date(2026, 1, 19),  # MLK Day
    dt.date(2026, 2, 16),  # Presidents' Day
    dt.date(2026, 5, 25),  # Memorial Day
    dt.date(2026, 6, 19),  # Juneteenth
    dt.date(2026, 7, 3),   # Independence Day (observed Friday)
    dt.date(2026, 9, 7),   # Labor Day
    dt.date(2026, 11, 11), # Veterans Day
    dt.date(2026, 11, 26), # Thanksgiving
    dt.date(2026, 12, 25), # Christmas
    dt.date(2027, 1, 1),   # New Year's
    dt.date(2027, 1, 18),  # MLK Day
    dt.date(2027, 2, 15),  # Presidents' Day
    dt.date(2027, 5, 31),  # Memorial Day
    dt.date(2027, 6, 18),  # Juneteenth (observed Friday — Jun 19 is Saturday)
    dt.date(2027, 7, 5),   # Independence Day (observed Monday — Jul 4 is Sunday)
    dt.date(2027, 9, 6),   # Labor Day
    dt.date(2027, 11, 11), # Veterans Day
    dt.date(2027, 11, 25), # Thanksgiving
    dt.date(2027, 12, 24), # Christmas (observed Friday — Dec 25 is Saturday)
})


def is_federal_holiday(d: dt.date) -> bool:
    """Return True when ``d`` is in the known federal holiday list."""
    return d in _FEDERAL_HOLIDAYS


def next_business_day(d: dt.date) -> dt.date:
    """Return the next business day (Mon–Fri, non-federal-holiday) after ``d``."""
    candidate = d + dt.timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in _FEDERAL_HOLIDAYS:
        candidate += dt.timedelta(days=1)
    return candidate


@dataclass
class CalendarEvent:
    date: dt.date
    time_et: dt.time          # ET time (no tz info — always ET)
    label: str                # Display name
    affected: str             # Underlyings / panel reference
    holiday_delayed: bool = False  # True when shifted from normal cadence
    de_emphasized: bool = False    # True for positioning reads (COT)


def _weekday_occurrences_in_window(
    weekday: int,  # 0=Mon ... 6=Sun
    start: dt.date,
    end: dt.date,
) -> list[dt.date]:
    """All occurrences of a given weekday in [start, end]."""
    result: list[dt.date] = []
    days_ahead = (weekday - start.weekday()) % 7
    current = start + dt.timedelta(days=days_ahead)
    while current <= end:
        result.append(current)
        current += dt.timedelta(days=7)
    return result


def _is_wednesday_before_thanksgiving(d: dt.date) -> bool:
    """Return True when ``d`` is the Wednesday immediately before US Thanksgiving.

    US Thanksgiving is the 4th Thursday of November. The Wednesday before it is
    when EIA may release the petroleum report one day early (Tuesday that week).
    """
    if d.month != 11 or d.weekday() != 2:  # must be a November Wednesday
        return False
    thursday = d + dt.timedelta(days=1)
    if thursday.weekday() != 3:  # the next day must be Thursday
        return False
    # Check that thursday is the 4th Thursday of November.
    # The 4th Thursday is the one where (thursday.day - 1) // 7 == 3.
    return (thursday.day - 1) // 7 == 3


def compute_eia_petroleum_events(
    start: dt.date, end: dt.date
) -> list[CalendarEvent]:
    """EIA petroleum storage: Wednesday 10:30 ET, shifted to next business day on holiday."""
    events: list[CalendarEvent] = []
    for d in _weekday_occurrences_in_window(2, start, end):  # 2=Wed
        delayed = is_federal_holiday(d)
        actual = next_business_day(d) if delayed else d
        if actual > end:
            continue
        affected = "CL/RB/HO → Panel B"
        if _is_wednesday_before_thanksgiving(actual):
            affected += " (EIA may release Tue this week)"
        events.append(CalendarEvent(
            date=actual,
            time_et=dt.time(10, 30),
            label="EIA Petroleum Storage",
            affected=affected,
            holiday_delayed=delayed,
        ))
    return events


def compute_eia_natgas_events(
    start: dt.date, end: dt.date
) -> list[CalendarEvent]:
    """EIA natural gas storage: Thursday 10:30 ET, shifted to next business day on holiday.
    Independent of the petroleum shift."""
    events: list[CalendarEvent] = []
    for d in _weekday_occurrences_in_window(3, start, end):  # 3=Thu
        delayed = is_federal_holiday(d)
        actual = next_business_day(d) if delayed else d
        if actual > end:
            continue
        events.append(CalendarEvent(
            date=actual,
            time_et=dt.time(10, 30),
            label="EIA Natural Gas Storage",
            affected="NG/UNG → Panel B",
            holiday_delayed=delayed,
        ))
    return events


def compute_nfp_events(
    start: dt.date, end: dt.date
) -> list[CalendarEvent]:
    """NFP (Non-Farm Payrolls): first Friday of the month 08:30 ET, shifted to
    next business day on federal holiday."""
    events: list[CalendarEvent] = []
    year, month = start.year, start.month
    while True:
        first_day = dt.date(year, month, 1)
        days_to_friday = (4 - first_day.weekday()) % 7  # 4=Fri
        first_friday = first_day + dt.timedelta(days=days_to_friday)
        delayed = is_federal_holiday(first_friday)
        actual = next_business_day(first_friday) if delayed else first_friday
        if start <= actual <= end:
            events.append(CalendarEvent(
                date=actual,
                time_et=dt.time(8, 30),
                label="NFP (Jobs Report)",
                affected="GC/SI/CL/energy, DXY → Panel A/B",
                holiday_delayed=delayed,
            ))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        if dt.date(year, month, 1) > end + dt.timedelta(days=7):
            break
    return events


def compute_cot_events(
    start: dt.date, end: dt.date
) -> list[CalendarEvent]:
    """COT release: Friday 15:30 ET, shifted to next business day on holiday.
    De-emphasized: positioning read, not a vol-moving event."""
    events: list[CalendarEvent] = []
    for d in _weekday_occurrences_in_window(4, start, end):  # 4=Fri
        delayed = is_federal_holiday(d)
        actual = next_business_day(d) if delayed else d
        if actual > end:
            continue
        events.append(CalendarEvent(
            date=actual,
            time_et=dt.time(15, 30),
            label="COT Report",
            affected="Positioning → Panel C",
            holiday_delayed=delayed,
            de_emphasized=True,
        ))
    return events


def load_yaml_events(
    calendar_config: dict, start: dt.date, end: dt.date
) -> list[CalendarEvent]:
    """Load hardcoded YAML events (WASDE/FOMC/CPI) within [start, end].

    Malformed entries (bad time format, unparseable date) are skipped with no
    effect on other entries — per-entry isolation.
    """
    events: list[CalendarEvent] = []
    for _event_type, spec in calendar_config.items():
        if not isinstance(spec, dict):
            continue
        try:
            time_str = spec.get("time", "12:00")
            h, m = map(int, str(time_str).split(":"))
            event_time = dt.time(h, m)
        except (ValueError, AttributeError, TypeError):
            continue  # skip this event type entirely — bad time field
        label = spec.get("label", _event_type.upper())
        affected = spec.get("affected", "")
        for key in spec:
            if not key.startswith("dates_"):
                continue
            for date_str in (spec[key] or []):
                try:
                    d = dt.date.fromisoformat(str(date_str))
                except (ValueError, TypeError):
                    continue  # skip malformed date, keep others
                if start <= d <= end:
                    events.append(CalendarEvent(
                        date=d,
                        time_et=event_time,
                        label=label,
                        affected=affected,
                    ))
    return events


def _unconfigured_yaml_types_in_window(
    calendar_config: dict, start: dt.date, end: dt.date
) -> list[str]:
    """Return labels of YAML event types that have no configured dates for any
    year in the window — so the user knows WASDE/FOMC/CPI dates for that year
    are not loaded. Only fires when the window spans a year with no dates_YYYY
    key in the YAML."""
    window_years = set(range(start.year, end.year + 1))
    missing: list[str] = []
    for _event_type, spec in calendar_config.items():
        if not isinstance(spec, dict):
            continue
        configured_years: set[int] = set()
        for k in spec:
            if not k.startswith("dates_"):
                continue
            try:
                configured_years.add(int(k.replace("dates_", "")))
            except (ValueError, TypeError):
                pass  # non-year-suffixed key — ignore
        if window_years - configured_years:
            missing.append(spec.get("label", _event_type.upper()))
    return missing


def upcoming_events(
    now_et: dt.datetime,
    calendar_config: dict,
    days: int = 7,
) -> tuple[list[CalendarEvent], list[str]]:
    """Return ``(events_in_window, unconfigured_yaml_types)``.

    ``events_in_window``: all events from today through today + days - 1, sorted
    by (date, time_et). Events whose date == today AND time_et < now_et.time()
    are included (elapsed); callers detect elapsed via the same comparison.

    ``unconfigured_yaml_types``: labels of YAML event types with no dates
    configured for any year spanned by the window (for the year-boundary note).
    """
    today = now_et.date()
    end = today + dt.timedelta(days=days - 1)

    events: list[CalendarEvent] = []
    events.extend(compute_eia_petroleum_events(today, end))
    events.extend(compute_eia_natgas_events(today, end))
    events.extend(compute_nfp_events(today, end))
    events.extend(compute_cot_events(today, end))
    events.extend(load_yaml_events(calendar_config, today, end))

    events.sort(key=lambda e: (e.date, e.time_et))
    unconfigured = _unconfigured_yaml_types_in_window(calendar_config, today, end)
    return events, unconfigured
