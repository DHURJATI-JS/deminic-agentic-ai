"""Shared helpers: per-user counters, references, time handling, text vars."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Per-user sequential numbers (chat#N, task#N, client#N ...)
# ---------------------------------------------------------------------------


async def next_no(db: AsyncSession, user_id: int, kind: str) -> int:
    row = await db.execute(
        text(
            "INSERT INTO user_counters (user_id, kind, value) VALUES (:u, :k, 1) "
            "ON CONFLICT (user_id, kind) DO UPDATE SET value = user_counters.value + 1 RETURNING value"
        ),
        {"u": user_id, "k": kind},
    )
    return int(row.scalar_one())


# ---------------------------------------------------------------------------
# References: chat#12, chat#12.3, task#4, client#2, email#5, event#3 ...
# ---------------------------------------------------------------------------
REF_RE = re.compile(r"\b(chat|task|client|email|event|reminder|txn)#(\d+)(?:\.(\d+))?\b", re.I)


def chat_ref(chat_no: int | None, msg_no: int | None = None) -> str:
    if not chat_no:
        return ""
    return f"chat#{chat_no}" + (f".{msg_no}" if msg_no else "")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def tz_for(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_now(tzname: str | None) -> datetime:
    return datetime.now(tz_for(tzname))


_TIME_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\bat\s+(\d{1,2})(?::(\d{2}))\b", re.I)
_REL_RE = re.compile(r"\bin\s+(\d{1,3})\s*(min(?:ute)?s?|hours?|hrs?|days?)\b", re.I)


def parse_when(text_in: str, tzname: str | None, base: datetime | None = None) -> datetime | None:
    """Very small natural-language time parser used as a fallback when the
    LLM does not supply an ISO timestamp. Returns an aware UTC datetime."""
    if not text_in:
        return None
    tz = tz_for(tzname)
    now = (base or datetime.now(tz)).astimezone(tz)
    s = text_in.lower()
    m = _REL_RE.search(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("min") else timedelta(hours=n) if unit.startswith(("h",)) else timedelta(days=n)
        return (now + delta).astimezone(timezone.utc)
    day = now.date()
    if "day after tomorrow" in s:
        day = day + timedelta(days=2)
    elif "tomorrow" in s or "tmrw" in s:
        day = day + timedelta(days=1)
    else:
        for i, wd in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")):
            if re.search(rf"\b{wd}\b", s):
                ahead = (i - now.weekday()) % 7 or 7
                day = day + timedelta(days=ahead)
                break
    m = _TIME_RE.search(s)
    hour = minute = None
    if m:
        if m.group(1):
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            ampm = m.group(3).lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
        else:
            hour, minute = int(m.group(4)), int(m.group(5) or 0)
    elif "tonight" in s:
        hour, minute = 20, 0
    elif "morning" in s:
        hour, minute = 9, 0
    elif "afternoon" in s:
        hour, minute = 14, 0
    elif "evening" in s:
        hour, minute = 18, 0
    elif "noon" in s:
        hour, minute = 12, 0
    if hour is None:
        if day != now.date():
            hour, minute = 9, 0
        else:
            return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if dt < now and day == now.date() and "today" not in s:
        dt = dt + timedelta(days=1)
    return dt.astimezone(timezone.utc)


def parse_iso(value: str | None, tzname: str | None = None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            d = date.fromisoformat(v[:10])
            dt = datetime(d.year, d.month, d.day, 9, 0)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_for(tzname))
    return dt.astimezone(timezone.utc)


def fmt_local(dt: datetime | None, tzname: str | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(tz_for(tzname)).strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")


# ---------------------------------------------------------------------------
# {business_name} variable - the AI writes the placeholder and the current
# business name is substituted at render time (so renames show everywhere).
# ---------------------------------------------------------------------------
BIZ_VAR_RE = re.compile(r"\{\{\s*business_name\s*\}\}|\{\s*business_name\s*\}")


def render_business_vars(text_in: str, business_name: str, *, escape: bool = False) -> str:
    if not text_in:
        return text_in
    name = business_name or "your business"
    if escape:
        name = html.escape(name)
    return BIZ_VAR_RE.sub(lambda _m: name, text_in)


def clip(text_in: str, n: int) -> str:
    text_in = text_in or ""
    return text_in if len(text_in) <= n else text_in[: n - 1] + "…"
