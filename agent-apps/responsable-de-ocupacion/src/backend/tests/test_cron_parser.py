"""R-CRON-1 / R-OPS-6 regression test — ``watcher._parse_cron_field``
+ ``watcher._cron_matches``.

The skill's lightweight cron parser is the runtime counterpart of
the SPA's visual CronBuilder. Together they make ``cron_expr`` a
first-class watcher type — pre-v7.3 the executor only honoured
``interval_sec`` and silently ignored cron schedules. The regressions
this test guards:

  * The 5 supported field tokens (``*``, integer, range, list, step)
    expand correctly when staying within bounds.
  * Out-of-range / inverted / negative-step inputs raise
    ``ValueError`` (so the watcher loop skips them with a warning
    instead of crashing the whole executor).
  * ``_cron_matches`` honours Sunday-is-0-AND-7, day-of-week numbering
    (cron Sun=0..Sat=6, not Python's Mon=0..Sun=6) and the per-field
    AND semantics. These were the off-by-one bugs that produced the
    "watcher silently never fires" symptom in early v7.3 testing.

Sync test (no asyncio, no aiosqlite) — fast and deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.autonomous.watcher import _cron_matches, _parse_cron_field


# ─── Field expansion ────────────────────────────────────────────────


def test_star_expands_to_full_range() -> None:
    assert _parse_cron_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}


def test_single_integer() -> None:
    assert _parse_cron_field("17", 0, 59) == {17}


def test_range() -> None:
    assert _parse_cron_field("1-5", 0, 23) == {1, 2, 3, 4, 5}


def test_list() -> None:
    assert _parse_cron_field("1,3,5", 0, 6) == {1, 3, 5}


def test_step_over_star() -> None:
    # */2 between 0 and 23 → 0, 2, 4, …, 22
    assert _parse_cron_field("*/2", 0, 23) == set(range(0, 24, 2))


def test_step_over_range() -> None:
    assert _parse_cron_field("10-20/3", 0, 59) == {10, 13, 16, 19}


def test_mixed_list_and_range() -> None:
    assert _parse_cron_field("0,15-17,30", 0, 59) == {0, 15, 16, 17, 30}


# ─── Error handling — the executor MUST keep running ────────────────


@pytest.mark.parametrize(
    "spec",
    [
        "60",            # past upper bound
        "-1",            # negative
        "30-10",         # inverted range
        "*/0",           # zero step
        "*/-1",          # negative step
        "abc",           # garbage
        "",              # empty → no values
    ],
)
def test_invalid_field_raises_value_error(spec: str) -> None:
    with pytest.raises(ValueError):
        _parse_cron_field(spec, 0, 59)


# ─── End-to-end: _cron_matches honours weekday + the every-2h
#                default the CronBuilder seeds (R-CRON-1) ────────────


def test_every_2_hours_default_matches_aligned_buckets() -> None:
    """The CronBuilder default seed is ``0 */2 * * *`` (every 2h at
    minute :00). Validate the runtime agrees: minute=0 + an even hour
    must match; any other minute must NOT match; an odd hour at
    minute 0 must NOT match either."""
    expr = "0 */2 * * *"
    # Match: 00:00, 02:00, …, 22:00
    for hour in range(0, 24, 2):
        dt = datetime(2026, 5, 15, hour, 0, tzinfo=timezone.utc)
        assert _cron_matches(expr, dt), f"expected match at {hour:02d}:00"
    # No match at 01:00, 03:00, …
    for hour in range(1, 24, 2):
        dt = datetime(2026, 5, 15, hour, 0, tzinfo=timezone.utc)
        assert not _cron_matches(expr, dt), f"unexpected match at {hour:02d}:00"
    # No match at xx:15 for any aligned hour.
    for hour in range(0, 24, 2):
        dt = datetime(2026, 5, 15, hour, 15, tzinfo=timezone.utc)
        assert not _cron_matches(expr, dt), f"unexpected match at {hour:02d}:15"


def test_weekly_weekdays_at_9_matches_correct_day() -> None:
    # Mon-Fri 09:00 — cron day-of-week 1-5.
    expr = "0 9 * * 1-5"
    # Friday 2026-05-15 09:00 UTC → match.
    assert _cron_matches(expr, datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc))
    # Saturday 2026-05-16 09:00 UTC → no match.
    assert not _cron_matches(expr, datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc))
    # Sunday 2026-05-17 09:00 UTC → no match.
    assert not _cron_matches(expr, datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc))


def test_sunday_accepts_zero_and_seven() -> None:
    # Cron lets users write either ``0`` (Sun) or ``7`` (Sun) in the
    # DOW field. The runtime must accept both spellings.
    sun = datetime(2026, 5, 17, 6, 0, tzinfo=timezone.utc)
    assert _cron_matches("0 6 * * 0", sun)
    assert _cron_matches("0 6 * * 7", sun)
