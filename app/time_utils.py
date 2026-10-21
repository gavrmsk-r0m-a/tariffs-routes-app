from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

APP_TIMEZONE = timezone(timedelta(hours=3))
APP_TIMEZONE_LABEL = "GMT+3"
UTC = timezone.utc


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def to_app_datetime(value: object) -> datetime | None:
    """Convert a stored timestamp to the operational GMT+3 timezone.

    PostgreSQL TIMESTAMPTZ values are timezone-aware. Naive legacy values are
    treated as UTC so existing server-time records retain their real instant.
    """
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(APP_TIMEZONE)


def format_app_datetime(value: object, *, with_timezone: bool = False) -> str:
    parsed = to_app_datetime(value)
    if parsed is None:
        return "" if value is None else str(value)
    rendered = parsed.strftime("%Y-%m-%d %H:%M")
    return f"{rendered} ({APP_TIMEZONE_LABEL})" if with_timezone else rendered


def app_now_input_value(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(APP_TIMEZONE).strftime("%Y-%m-%dT%H:%M")


def app_input_to_utc(value: object) -> str:
    """Interpret datetime-local input as GMT+3 and return an aware UTC value."""
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError("Некорректный формат даты события")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=APP_TIMEZONE)
    return parsed.astimezone(UTC).isoformat()


def app_date_bound_to_utc(value: str, *, end_of_day: bool = False) -> str:
    parsed_date = date.fromisoformat(value)
    local_time = time.max if end_of_day else time.min
    local_dt = datetime.combine(parsed_date, local_time, tzinfo=APP_TIMEZONE)
    return local_dt.astimezone(UTC).isoformat()
