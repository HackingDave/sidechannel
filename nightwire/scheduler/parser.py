"""Human-friendly schedule expression parser.

Converts natural time expressions into schedule type + parameters.
No cron syntax — designed for human readability.

Supported expressions:
    every hour
    every 6 hours
    every 30 minutes
    daily at 5am
    daily at 5:30pm
    every monday at 9am
    every weekday at 8am
    every weekend at 10am
    twice daily at 8am and 5pm
"""

import re
from typing import Optional, Tuple

from .models import ScheduleType


# Day name mapping
_DAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# Patterns (order matters — most specific first)
_TIME_PATTERN = r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?'


def _parse_time(match_str: str) -> Optional[str]:
    """Parse a time string like '5am', '5:30pm', '17:00' into HH:MM format."""
    m = re.match(_TIME_PATTERN, match_str.strip(), re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_schedule_expression(expr: str) -> Tuple[Optional[ScheduleType], Optional[dict], Optional[str]]:
    """Parse a human-friendly schedule expression.

    Returns:
        (schedule_type, params, description) on success.
        (None, None, error_message) on failure.
    """
    expr = expr.strip().lower()

    # "twice daily at 8am and 5pm"
    m = re.match(r'twice\s+daily\s+at\s+' + _TIME_PATTERN + r'\s+and\s+' + _TIME_PATTERN, expr)
    if m:
        t1 = _parse_time(f"{m.group(1)}:{m.group(2) or '00'}{m.group(3) or ''}")
        t2 = _parse_time(f"{m.group(4)}:{m.group(5) or '00'}{m.group(6) or ''}")
        if t1 and t2:
            desc = f"twice daily at {t1} and {t2}"
            return ScheduleType.DAILY, {"times": sorted([t1, t2])}, desc

    # "every weekday at 8am"
    m = re.match(r'every\s+weekday\s+at\s+(.+)', expr)
    if m:
        t = _parse_time(m.group(1))
        if t:
            return ScheduleType.WEEKDAY, {"time": t}, f"weekdays at {t}"

    # "every weekend at 10am"
    m = re.match(r'every\s+weekend\s+at\s+(.+)', expr)
    if m:
        t = _parse_time(m.group(1))
        if t:
            return ScheduleType.WEEKEND, {"time": t}, f"weekends at {t}"

    # "every monday at 9am" / "every tuesday at 3pm"
    day_names = "|".join(_DAYS.keys())
    m = re.match(rf'every\s+({day_names})\s+at\s+(.+)', expr)
    if m:
        day = m.group(1)
        t = _parse_time(m.group(2))
        if t and day in _DAYS:
            return ScheduleType.WEEKLY, {"day": _DAYS[day], "day_name": day, "time": t}, f"every {day} at {t}"

    # "daily at 5am" / "every day at 5am"
    m = re.match(r'(?:every\s+day|daily)\s+at\s+(.+)', expr)
    if m:
        t = _parse_time(m.group(1))
        if t:
            return ScheduleType.DAILY, {"times": [t]}, f"daily at {t}"

    # "every N hours" / "every N minutes" / "every hour"
    m = re.match(r'every\s+(\d+)?\s*(hour|minute)s?', expr)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2)
        minutes = n * 60 if unit == "hour" else n
        if minutes < 1:
            return None, None, "Interval must be at least 1 minute."
        if minutes > 10080:  # 1 week
            return None, None, "Interval cannot exceed 1 week (10080 minutes)."
        if unit == "hour":
            desc = f"every {n} hour{'s' if n != 1 else ''}"
        else:
            desc = f"every {n} minute{'s' if n != 1 else ''}"
        return ScheduleType.INTERVAL, {"minutes": minutes}, desc

    # Nothing matched
    return None, None, (
        "Could not parse schedule. Examples:\n"
        "  every hour\n"
        "  every 6 hours\n"
        "  every 30 minutes\n"
        "  daily at 5am\n"
        "  daily at 5:30pm\n"
        "  every monday at 9am\n"
        "  every weekday at 8am\n"
        "  every weekend at 10am\n"
        "  twice daily at 8am and 5pm"
    )
