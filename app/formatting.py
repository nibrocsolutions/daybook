"""Display helpers for money, time, and addresses."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, available_timezones

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+\Z")
_TIMEZONES = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timezone_names() -> set[str]:
    global _TIMEZONES
    if _TIMEZONES is None:
        _TIMEZONES = available_timezones()
    return _TIMEZONES


def clean_timezone(name: str | None, fallback: str = "America/New_York") -> str:
    candidate = (name or "").strip()
    if candidate in timezone_names():
        return candidate
    if fallback in timezone_names():
        return fallback
    return "UTC"


def zone(name: str) -> ZoneInfo:
    return ZoneInfo(clean_timezone(name))


def to_local(value: datetime | None, tz_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone(tz_name))


def local_day_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = zone(tz_name)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def format_money(cents: int | None) -> str:
    amount = 0 if cents is None else int(cents)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    return f"{sign}${amount // 100:,}.{amount % 100:02d}"


def format_percent(bps: int) -> str:
    value = (Decimal(int(bps)) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{value:.2f}"
    if text.endswith("00"):
        return text[:-3]
    if text.endswith("0"):
        return text[:-1]
    return text


def format_quantity(quantity) -> str:
    if quantity is None:
        return ""
    text = format(Decimal(str(quantity)).quantize(Decimal("0.01")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _clock(local: datetime) -> str:
    return local.strftime("%I:%M %p").lstrip("0")


def format_day(value: datetime | None, tz_name: str) -> str:
    local = to_local(value, tz_name)
    if local is None:
        return "Unscheduled"
    return f"{local.strftime('%a, %b')} {local.day}"


def format_when(value: datetime | None, tz_name: str) -> str:
    local = to_local(value, tz_name)
    if local is None:
        return "Unscheduled"
    return f"{local.strftime('%a, %b')} {local.day}, {local.year} · {_clock(local)}"


def format_clock(value: datetime | None, tz_name: str) -> str:
    local = to_local(value, tz_name)
    if local is None:
        return ""
    return _clock(local)


def format_range(start: datetime | None, end: datetime | None, tz_name: str) -> str:
    if start is None:
        return "Unscheduled"
    start_local = to_local(start, tz_name)
    assert start_local is not None
    if end is None:
        return _clock(start_local)
    end_local = to_local(end, tz_name)
    assert end_local is not None
    if start_local.date() == end_local.date():
        return f"{_clock(start_local)} – {_clock(end_local)}"
    return f"{_clock(start_local)} – {format_when(end, tz_name)}"


def datetime_local_value(value: datetime | None, tz_name: str) -> str:
    local = to_local(value, tz_name)
    if local is None:
        return ""
    return local.strftime("%Y-%m-%dT%H:%M")


def today_label(tz_name: str, moment: datetime | None = None) -> str:
    current = moment or datetime.now(zone(tz_name))
    local = current.astimezone(zone(tz_name))
    return f"{local.strftime('%A, %B')} {local.day}"


def greeting(tz_name: str, moment: datetime | None = None) -> str:
    current = moment or datetime.now(zone(tz_name))
    hour = current.astimezone(zone(tz_name)).hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def postal_address(line: str, city: str, region: str, postal: str) -> str:
    locality = ", ".join(part for part in [city.strip(), region.strip()] if part)
    if postal.strip():
        locality = f"{locality} {postal.strip()}".strip()
    return "\n".join(part for part in [line.strip(), locality] if part)


def tel_href(phone: str | None) -> str | None:
    raw = phone or ""
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return None
    if raw.strip().startswith("+"):
        return f"tel:+{digits}"
    return f"tel:{digits}"


def mailto_href(email: str | None) -> str | None:
    candidate = (email or "").strip()
    if not candidate or not _EMAIL.match(candidate):
        return None
    return f"mailto:{candidate}"


def maps_url(address: str | None) -> str | None:
    text = " ".join((address or "").split())
    if not text:
        return None
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(text)


def tax_label(rate_bps: int, tax_labor: bool) -> str:
    if rate_bps <= 0:
        return "No tax"
    rate = format_percent(rate_bps)
    if tax_labor:
        return f"{rate}%"
    return f"{rate}% on materials and other charges, not time"


def csv_safe(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
