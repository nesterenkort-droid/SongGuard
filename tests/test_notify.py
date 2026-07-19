"""Notification scheduling: quiet hours + digest slots (pure)."""

from datetime import UTC, datetime

from app.services.notify import _next_daily_slot, _next_weekly_slot, apply_quiet_hours


def test_quiet_hours_pushes_past_window_same_night():
    # 23:00 UTC, quiet window 22-7 -> pushed to 07:00 same calendar day rollover.
    now = datetime(2026, 7, 20, 23, 0, tzinfo=UTC)
    result = apply_quiet_hours(now, 22, 7)
    assert result.hour == 7
    assert result > now


def test_quiet_hours_no_window_returns_unchanged():
    now = datetime(2026, 7, 20, 23, 0, tzinfo=UTC)
    assert apply_quiet_hours(now, None, None) == now


def test_quiet_hours_outside_window_unchanged():
    now = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    assert apply_quiet_hours(now, 22, 7) == now


def test_daily_slot_is_next_8am_utc():
    early = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    assert _next_daily_slot(early).hour == 8
    assert _next_daily_slot(early).day == 20

    late = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    nxt = _next_daily_slot(late)
    assert nxt.hour == 8
    assert nxt.day == 21


def test_weekly_slot_is_next_monday_8am():
    # 2026-07-20 is a Monday.
    monday_morning = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    slot = _next_weekly_slot(monday_morning)
    assert slot.weekday() == 0
    assert slot.hour == 8
    assert slot.date() == monday_morning.date()

    monday_afternoon = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    slot2 = _next_weekly_slot(monday_afternoon)
    assert slot2.weekday() == 0
    assert slot2 > monday_afternoon
    assert (slot2 - monday_afternoon).days >= 6
