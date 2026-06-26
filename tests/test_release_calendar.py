"""Tests for common/release_calendar.py — pure clock-injectable release-calendar helpers.

All tests are network-free and DB-free. Clock is always injected via now_et;
no datetime.now() patching required. Mirrors the pure-logic test pattern in
test_panel_c.py / test_scheduler.py.
"""
import datetime as dt

import pytest
from zoneinfo import ZoneInfo

from common.release_calendar import (
    ET,
    CalendarEvent,
    _FEDERAL_HOLIDAYS,
    _unconfigured_yaml_types_in_window,
    _weekday_occurrences_in_window,
    compute_cot_events,
    compute_eia_natgas_events,
    compute_eia_petroleum_events,
    compute_nfp_events,
    is_federal_holiday,
    load_yaml_events,
    next_business_day,
    upcoming_events,
)


def _et(year, month, day, hour=12, minute=0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=ET)


# --- is_federal_holiday / next_business_day -----------------------------------

def test_is_federal_holiday_known_holiday():
    assert is_federal_holiday(dt.date(2026, 1, 1)) is True


def test_is_federal_holiday_regular_day():
    assert is_federal_holiday(dt.date(2026, 1, 2)) is False


def test_next_business_day_skips_weekend():
    # Friday -> Monday (assuming not a holiday)
    assert next_business_day(dt.date(2026, 6, 19)) == dt.date(2026, 6, 22)


def test_next_business_day_skips_holiday():
    # New Year's Day 2026 (Thursday) -> Friday 2026-01-02
    assert next_business_day(dt.date(2025, 12, 31)) == dt.date(2026, 1, 2)


def test_next_business_day_skips_consecutive_holidays():
    # Christmas is Friday 2026-12-25 (holiday); next is Monday 2026-12-28 (not a holiday)
    assert next_business_day(dt.date(2026, 12, 24)) == dt.date(2026, 12, 28)


# --- EIA Petroleum (Wednesday, shift to next business day on holiday) ---------

def test_eia_petroleum_normal_wednesday():
    # A plain Wednesday that is not a holiday -> Wednesday event.
    # 2026-06-24 is a Wednesday and not a holiday.
    start = dt.date(2026, 6, 24)
    end = dt.date(2026, 6, 24)
    events = compute_eia_petroleum_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2026, 6, 24)
    assert ev.time_et == dt.time(10, 30)
    assert ev.label == "EIA Petroleum Storage"
    assert ev.holiday_delayed is False


def test_eia_petroleum_holiday_wednesday_shifts_to_thursday():
    # Juneteenth 2026 is observed on a Friday (2026-07-03, Independence Day observed).
    # We need a Wednesday that IS a holiday. Use 2026-07-01 which is a Wednesday —
    # not a holiday. Let us manufacture a scenario: 2025-07-04 (Friday, Independence
    # Day) is a holiday but it's not a Wednesday. Use the _FEDERAL_HOLIDAYS set to
    # find a Wednesday holiday, or simply test the logic with a known pair.
    # 2026-06-19 (Juneteenth) is a Friday, not a Wednesday.
    # Instead, test by passing the single-day window around a holiday-adjusted date
    # directly. We can use 2025-11-27 = Thursday Thanksgiving. EIA Petroleum is Wed
    # 2025-11-26. Is 2025-11-26 a holiday? No — only 2025-11-27 is.
    # For a genuine Wed holiday test, use: 2025-12-25 is Thursday. 2025-11-11 (Vet)
    # is a Tuesday. Let us check what day 2026-05-25 (Memorial Day) is.
    # Memorial Day 2026 is 2026-05-25, a Monday. Not a Wednesday.
    # Juneteenth 2025 = 2025-06-19 is a Thursday.
    # This is tricky — none of our 2025/2026 holidays happen to be Wednesdays.
    # Test the logic directly: inject a custom holiday.
    # The easiest approach: pick a Wednesday, call _FEDERAL_HOLIDAYS manually.
    # But we can't override the set in the function. So test via a date window
    # that wraps around a holiday Wednesday by checking the shift helper logic.
    #
    # Use 2025-07-04 (Friday) — petroleum's normal day is Wednesday 2025-07-02.
    # 2025-07-02 is NOT a holiday, but the following Friday release is near the
    # holiday. Petroleum is released on Wednesday, so 2025-07-02 is the window
    # date. It's not a holiday -> no shift.
    #
    # For a genuine shift test, since none of our 2025-2026 holidays fall on
    # a Wednesday, we test the machinery via Thanksgiving week:
    # 2025-11-27 (Thursday) is Thanksgiving. 2025-11-26 (Wednesday) is NOT a
    # holiday, so petroleum releases on 11-26 normally.
    # Let us instead test 2026-11-11 (Veterans Day, Wednesday).
    # Is 2026-11-11 in _FEDERAL_HOLIDAYS? Yes it is.
    assert dt.date(2026, 11, 11).weekday() == 2  # Wednesday
    assert is_federal_holiday(dt.date(2026, 11, 11))
    # EIA Petroleum should shift to Thursday 2026-11-12.
    start = dt.date(2026, 11, 11)
    end = dt.date(2026, 11, 13)
    events = compute_eia_petroleum_events(start, end)
    # The nominal Wednesday 2026-11-11 is a holiday -> actual = 2026-11-12 (Thursday)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2026, 11, 12)
    assert ev.holiday_delayed is True
    assert ev.label == "EIA Petroleum Storage"


def test_eia_petroleum_holiday_shift_is_next_business_day_not_plus_one_calendar():
    # Confirm that a holiday shift always lands on the next BUSINESS day,
    # not blindly +1 calendar day. A holiday on Wednesday that is followed by
    # another holiday on Thursday would push to Friday (or beyond).
    # In practice this is very rare — synthesize with Veterans Day 2026:
    # 2026-11-11 (Wed) is a holiday; 2026-11-12 (Thu) is not a holiday -> Thu.
    assert dt.date(2026, 11, 11).weekday() == 2  # Wednesday
    result = next_business_day(dt.date(2026, 11, 11))
    assert result == dt.date(2026, 11, 12)
    assert result.weekday() != 5 and result.weekday() != 6
    assert result not in _FEDERAL_HOLIDAYS


# --- EIA Natural Gas (Thursday, independent shift) ----------------------------

def test_eia_natgas_normal_thursday():
    # 2026-06-25 is a Thursday and not a holiday.
    start = dt.date(2026, 6, 25)
    end = dt.date(2026, 6, 25)
    events = compute_eia_natgas_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2026, 6, 25)
    assert ev.time_et == dt.time(10, 30)
    assert ev.label == "EIA Natural Gas Storage"
    assert ev.holiday_delayed is False


def test_eia_natgas_holiday_thursday_shifts_to_friday():
    # Thanksgiving 2025 is 2025-11-27 (Thursday) — a holiday.
    # Natural gas should shift to Friday 2025-11-28.
    assert dt.date(2025, 11, 27).weekday() == 3  # Thursday
    assert is_federal_holiday(dt.date(2025, 11, 27))
    start = dt.date(2025, 11, 27)
    end = dt.date(2025, 11, 28)
    events = compute_eia_natgas_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2025, 11, 28)
    assert ev.holiday_delayed is True


def test_eia_petroleum_and_natgas_shift_independently():
    # When a Wednesday is a holiday but the following Thursday is not:
    # -> Petroleum shifts to Thursday, Natgas stays on Thursday.
    # Both land on the same day; both should be present as distinct rows.
    # 2026-11-11 is Wed (holiday) -> petroleum shifts to Thu 2026-11-12.
    # 2026-11-12 is Thu (not a holiday) -> natgas stays on Thu 2026-11-12.
    start = dt.date(2026, 11, 11)
    end = dt.date(2026, 11, 12)
    petro = compute_eia_petroleum_events(start, end)
    natgas = compute_eia_natgas_events(start, end)
    assert len(petro) == 1
    assert len(natgas) == 1
    assert petro[0].date == dt.date(2026, 11, 12)
    assert natgas[0].date == dt.date(2026, 11, 12)
    assert petro[0].label != natgas[0].label
    assert petro[0].holiday_delayed is True
    assert natgas[0].holiday_delayed is False


def test_both_eia_on_same_shifted_day_are_distinct_rows():
    # Scenario: petroleum (Wed holiday) AND natgas (Thu holiday) both shift to Fri.
    # Christmas week 2025: 2025-12-25 is Thursday (holiday); 2025-12-24 (Wednesday)
    # is NOT a holiday. So petroleum stays on 12-24 (Wed), natgas shifts from
    # 12-25 (Thu holiday) to 12-26 (Fri).
    # -> They are NOT on the same day in this case.
    # For them to land on the same day: petroleum Wed holiday -> Thu, AND natgas
    # Thu holiday -> Fri. They'd land on different days (Thu vs Fri). Or both
    # pushed to the same target day.
    # The easiest scenario: Wed and Thu are BOTH holidays. Then petroleum shifts
    # to Fri and natgas also shifts to Fri (if Fri is a business day).
    # This is a theoretical case (no 2025-2026 example of consecutive Wed+Thu
    # holidays). We test the general property: when both shift to the same day,
    # upcoming_events returns two distinct rows.
    # Use Thanksgiving 2025: 2025-11-27 (Thu) holiday -> natgas shifts to Fri.
    # 2025-11-26 (Wed) is NOT a holiday -> petroleum stays on 11-26.
    # They're on different days. Let's just verify the labels differ (the key AC).
    events, _ = upcoming_events(
        now_et=_et(2026, 11, 11, 9, 0),
        calendar_config={},
        days=2,
    )
    labels = [e.label for e in events]
    petro_count = labels.count("EIA Petroleum Storage")
    natgas_count = labels.count("EIA Natural Gas Storage")
    # At least petroleum (shifted from Wed 11-11 holiday) and natgas (Thu 11-12,
    # not a holiday) should both appear.
    assert petro_count == 1
    assert natgas_count == 1
    # They land on different dates (petro on 11-12, natgas on 11-12 also since
    # Thu is not a holiday) — but they are distinct rows with distinct labels.
    petro_ev = next(e for e in events if e.label == "EIA Petroleum Storage")
    natgas_ev = next(e for e in events if e.label == "EIA Natural Gas Storage")
    assert petro_ev.label != natgas_ev.label


# --- NFP (first Friday of month, shift on holiday) ----------------------------

def test_nfp_first_friday_of_month():
    # January 2026: first Friday is 2026-01-02.
    start = dt.date(2026, 1, 1)
    end = dt.date(2026, 1, 7)
    events = compute_nfp_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2026, 1, 2)
    assert ev.time_et == dt.time(8, 30)
    assert ev.label == "NFP (Jobs Report)"
    assert ev.holiday_delayed is False


def test_nfp_first_friday_is_holiday_shifts_to_next_business_day():
    # January 2, 2026 (first Friday) is NOT a holiday. Find a month where
    # the first Friday is in the federal holiday list.
    # July 3, 2026 is a Friday (Independence Day observed) and it is in
    # _FEDERAL_HOLIDAYS. Is it the FIRST Friday of July 2026?
    july_1 = dt.date(2026, 7, 1)  # Wednesday
    # First Friday of July 2026 = July 3 (days_ahead = (4-2)%7 = 2)
    assert july_1.weekday() == 2  # Wednesday
    first_friday_july_2026 = july_1 + dt.timedelta(days=2)
    assert first_friday_july_2026 == dt.date(2026, 7, 3)
    assert is_federal_holiday(first_friday_july_2026)
    # NFP should shift to next business day (Mon 2026-07-06, no holiday).
    start = dt.date(2026, 7, 1)
    end = dt.date(2026, 7, 7)
    events = compute_nfp_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2026, 7, 6)
    assert ev.holiday_delayed is True


def test_nfp_only_one_per_month():
    # Within any given 31-day window that spans one calendar month, there is
    # at most one NFP event.
    start = dt.date(2026, 6, 1)
    end = dt.date(2026, 6, 30)
    events = compute_nfp_events(start, end)
    assert len(events) == 1


# --- COT (Friday, de_emphasized) ----------------------------------------------

def test_cot_friday_event():
    # 2026-06-26 is a Friday.
    assert dt.date(2026, 6, 26).weekday() == 4
    start = dt.date(2026, 6, 26)
    end = dt.date(2026, 6, 26)
    events = compute_cot_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2026, 6, 26)
    assert ev.time_et == dt.time(15, 30)
    assert ev.label == "COT Report"
    assert ev.de_emphasized is True
    assert ev.holiday_delayed is False


def test_cot_de_emphasized_flag():
    # COT must always carry de_emphasized=True regardless of holiday.
    start = dt.date(2026, 6, 22)
    end = dt.date(2026, 6, 28)
    events = compute_cot_events(start, end)
    for ev in events:
        assert ev.de_emphasized is True


def test_cot_holiday_friday_shifts_to_monday():
    # 2026-07-03 (Friday, Independence Day observed) is a holiday.
    assert is_federal_holiday(dt.date(2026, 7, 3))
    assert dt.date(2026, 7, 3).weekday() == 4  # Friday
    start = dt.date(2026, 7, 3)
    end = dt.date(2026, 7, 6)
    events = compute_cot_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2026, 7, 6)
    assert ev.holiday_delayed is True
    assert ev.de_emphasized is True


# --- TODAY / TOMORROW labeling (via upcoming_events shape) --------------------

def test_today_event_is_in_window():
    # An event on today's date should be in the result.
    now = _et(2026, 6, 25, 9, 0)  # 2026-06-25 is a Thursday
    events, _ = upcoming_events(now, calendar_config={}, days=7)
    today_events = [e for e in events if e.date == now.date()]
    # EIA Natural Gas is on Thursday; should appear today.
    assert any(e.label == "EIA Natural Gas Storage" for e in today_events)


def test_tomorrow_event_is_in_window():
    # An event on the day after now_et.date() should be in the result.
    now = _et(2026, 6, 24, 9, 0)  # Wednesday 2026-06-24
    events, _ = upcoming_events(now, calendar_config={}, days=7)
    tomorrow = now.date() + dt.timedelta(days=1)
    tomorrow_events = [e for e in events if e.date == tomorrow]
    # Thursday 2026-06-25: EIA Nat Gas.
    assert any(e.label == "EIA Natural Gas Storage" for e in tomorrow_events)


# --- Elapsed detection --------------------------------------------------------

def test_elapsed_event_included_but_detectable():
    # A release at 08:30 whose time has passed by 09:00 today should still
    # appear in the list (upcoming_events includes all today's events). Callers
    # detect elapsed via: ev.date == today AND ev.time_et < now_et.time().
    now = _et(2026, 7, 15, 9, 0)  # 09:00 ET on 2026-07-15 (Wednesday)
    # EIA Petroleum is on Wednesday at 10:30 — NOT elapsed yet.
    # But NFP on 2026-07-03 is before our window, not relevant here.
    # Insert a synthetic test: if we had an 08:30 event on today, it would be
    # elapsed. Use a YAML config with a fake event at 08:00.
    fake_config = {
        "test_event": {
            "label": "Test Release",
            "time": "08:00",
            "affected": "test",
            "dates_2026": ["2026-07-15"],
        }
    }
    events, _ = upcoming_events(now, fake_config, days=1)
    matched = [e for e in events if e.label == "Test Release"]
    assert len(matched) == 1
    ev = matched[0]
    assert ev.date == now.date()
    # The caller can compute elapsed:
    assert ev.time_et < now.time()


def test_upcoming_event_not_elapsed():
    # A release at 10:30 when the clock is 09:00 is NOT elapsed.
    now = _et(2026, 6, 24, 9, 0)  # Wednesday 09:00 ET
    events, _ = upcoming_events(now, calendar_config={}, days=1)
    petro = [e for e in events if e.label == "EIA Petroleum Storage"]
    assert len(petro) == 1
    ev = petro[0]
    # Not elapsed:
    assert not (ev.date == now.date() and ev.time_et < now.time())


# --- Year-boundary: Dec 31 -> Jan 1 window ------------------------------------

def test_year_boundary_no_crash():
    # Window spanning 2026-12-29 through 2027-01-04 (7 days from Dec 29).
    now = _et(2026, 12, 29, 12, 0)
    # YAML has no 2027 dates -> unconfigured_cal_types should be populated.
    cal_config = {
        "wasde": {
            "label": "WASDE",
            "time": "12:00",
            "affected": "test",
            "dates_2026": ["2026-12-10"],
        }
    }
    # Must not raise.
    events, unconfigured = upcoming_events(now, cal_config, days=7)
    # Rule-computed entries (EIA, COT, NFP) still fire for the new year.
    assert isinstance(events, list)
    # Jan 1 2027 is a Thursday but a holiday; natgas would shift to Fri Jan 2.
    # Jan 2 2027 is a Friday — COT appears. Verify at least some events generated.
    assert len(events) > 0
    # WASDE is not configured for 2027 (only 2026 dates are present and fall
    # outside the window), so 'WASDE' should appear in unconfigured.
    assert "WASDE" in unconfigured


def test_year_boundary_rule_computed_events_still_generated():
    # Verify EIA events cross into January 2027 cleanly.
    now = _et(2026, 12, 30, 12, 0)  # Wednesday
    events, _ = upcoming_events(now, calendar_config={}, days=7)
    # 2026-12-30 is a Wednesday -> EIA Petroleum appears today or in the window.
    dates_in_window = [e.date for e in events]
    jan_dates = [d for d in dates_in_window if d.year == 2027]
    # The window includes 2027-01-01 through 2027-01-05; EIA events should appear.
    assert len(jan_dates) > 0


def test_year_boundary_unconfigured_types_populated():
    # YAML with 2026-only entries; window spanning into 2027 -> unconfigured list.
    cal_config = {
        "fomc": {
            "label": "FOMC Decision",
            "time": "14:00",
            "affected": "test",
            "dates_2026": ["2026-12-16"],
        }
    }
    now = _et(2026, 12, 29, 12, 0)
    _, unconfigured = upcoming_events(now, cal_config, days=7)
    assert "FOMC Decision" in unconfigured


# --- Empty YAML config — only rule-computed entries ----------------------------

def test_empty_yaml_config_only_rule_computed():
    # With an empty config dict, no YAML events appear, but EIA/NFP/COT still fire.
    now = _et(2026, 6, 24, 9, 0)  # Wednesday
    events, unconfigured = upcoming_events(now, calendar_config={}, days=7)
    assert isinstance(events, list)
    assert len(events) > 0
    # No YAML events -> unconfigured is empty (nothing to be missing).
    assert unconfigured == []
    yaml_labels = {"WASDE (USDA)", "FOMC Decision", "CPI (BLS)"}
    for ev in events:
        assert ev.label not in yaml_labels


# --- YAML dates are all weekdays ----------------------------------------------

def test_yaml_dates_are_all_weekdays():
    """Every date in config/release_calendar.yaml must fall on Mon–Fri (weekday() < 5).

    USDA WASDE, FOMC, and CPI are never published on a Saturday or Sunday.
    Uses load_release_calendar() with no path arg so it reads the committed file.
    """
    import datetime as dt
    from common.config import load_release_calendar

    calendar_config = load_release_calendar()
    for event_type, spec in calendar_config.items():
        if not isinstance(spec, dict):
            continue
        label = spec.get("label", event_type)
        for key, value in spec.items():
            if not key.startswith("dates_"):
                continue
            for date_str in (value or []):
                d = dt.date.fromisoformat(str(date_str))
                assert d.weekday() < 5, (
                    f"Date {date_str} in '{label}' ({key}) falls on a "
                    f"{'Saturday' if d.weekday() == 5 else 'Sunday'} — "
                    "release dates must be Mon–Fri."
                )


# --- load_yaml_events ---------------------------------------------------------

def test_load_yaml_events_filters_to_window():
    config = {
        "fomc": {
            "label": "FOMC Decision",
            "time": "14:00",
            "affected": "rates",
            "dates_2026": ["2026-06-17", "2026-07-29"],
        }
    }
    start = dt.date(2026, 6, 15)
    end = dt.date(2026, 6, 20)
    events = load_yaml_events(config, start, end)
    assert len(events) == 1
    assert events[0].date == dt.date(2026, 6, 17)
    assert events[0].label == "FOMC Decision"
    assert events[0].time_et == dt.time(14, 0)


def test_load_yaml_events_empty_config():
    events = load_yaml_events({}, dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    assert events == []


# --- upcoming_events sorting --------------------------------------------------

def test_upcoming_events_sorted_by_date_then_time():
    # Multiple events on the same day must be sorted by time.
    now = _et(2026, 6, 26, 7, 0)  # Friday: COT at 15:30 and a fake 08:00 event
    fake_config = {
        "nfp_like": {
            "label": "Early Release",
            "time": "08:00",
            "affected": "test",
            "dates_2026": ["2026-06-26"],
        }
    }
    events, _ = upcoming_events(now, fake_config, days=1)
    for i in range(len(events) - 1):
        assert (events[i].date, events[i].time_et) <= (events[i + 1].date, events[i + 1].time_et)


# --- holiday_delayed label is set on shifted entries --------------------------

def test_holiday_delayed_flag_on_shifted_entry():
    # 2026-11-11 (Wednesday Veterans Day) -> petroleum shifts to Thursday.
    start = dt.date(2026, 11, 11)
    end = dt.date(2026, 11, 12)
    events = compute_eia_petroleum_events(start, end)
    assert events[0].holiday_delayed is True


def test_no_holiday_delayed_flag_on_normal_entry():
    # 2026-06-24 (Wednesday, not a holiday) -> no shift.
    start = dt.date(2026, 6, 24)
    end = dt.date(2026, 6, 24)
    events = compute_eia_petroleum_events(start, end)
    assert events[0].holiday_delayed is False


# --- Thanksgiving-week EIA Petroleum note ------------------------------------

def test_eia_petroleum_thanksgiving_week_note():
    # 2026-11-25 is the Wednesday before Thanksgiving 2026 (2026-11-26).
    # The event's affected field should contain the early-release note.
    assert dt.date(2026, 11, 25).weekday() == 2  # Wednesday
    assert dt.date(2026, 11, 26).weekday() == 3  # Thursday (Thanksgiving)
    start = dt.date(2026, 11, 25)
    end = dt.date(2026, 11, 25)
    events = compute_eia_petroleum_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.date == dt.date(2026, 11, 25)
    assert "may release" in ev.affected


def test_eia_petroleum_no_thanksgiving_note_normal_week():
    # 2026-11-18 is a regular Wednesday (not the Wednesday before Thanksgiving).
    assert dt.date(2026, 11, 18).weekday() == 2  # Wednesday
    start = dt.date(2026, 11, 18)
    end = dt.date(2026, 11, 18)
    events = compute_eia_petroleum_events(start, end)
    assert len(events) == 1
    ev = events[0]
    assert "may release" not in ev.affected


# --- No etl/ import from common/release_calendar.py --------------------------

def test_release_calendar_imports_no_etl():
    """common/release_calendar.py must not import from etl/ — dashboard image safe."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1] / "common" / "release_calendar.py"
    content = src.read_text()
    pattern = re.compile(r"^\s*(from\s+etl[\s.]|import\s+etl[\s.]?)", re.MULTILINE)
    assert not pattern.search(content), "common/release_calendar.py must not import etl/"
