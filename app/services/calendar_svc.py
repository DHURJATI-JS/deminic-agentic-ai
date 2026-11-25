"""Calendar events: Google Calendar template links, .ics export and the
"paste your Google Meet link" flow (the AI cannot create Meet links itself)."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import vectors
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.core.utils import chat_ref, fmt_local, next_no, utcnow
from app.models import CalendarEvent, Task, User

# Exact syntax of a Google Meet URL (regex is fine for fixed formats).
MEET_RE = re.compile(r"(?:https?://)?meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})\b", re.I)
MEET_NEW = "https://meet.google.com/new"


def find_meet_link(text: str | None) -> str | None:
    m = MEET_RE.search(text or "")
    return f"https://meet.google.com/{m.group(1).lower()}" if m else None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _end(ev: CalendarEvent) -> datetime:
    start = _aware(ev.starts_at)
    end = _aware(ev.ends_at)
    return end if end and end > start else start + timedelta(hours=1)


def _gfmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def details_text(ev: CalendarEvent) -> str:
    parts = []
    if ev.description:
        parts.append(ev.description)
    if ev.meet_link:
        parts.append(f"Google Meet: {ev.meet_link}")
    ref = chat_ref(ev.source_chat_no, ev.source_msg_no)
    parts.append(f"Added by Deminic (event#{ev.event_no}{', from ' + ref if ref else ''})")
    return "\n\n".join(parts)


def gcal_url(ev: CalendarEvent) -> str:
    params = {
        "action": "TEMPLATE",
        "text": ev.title,
        "dates": f"{_gfmt(_aware(ev.starts_at))}/{_gfmt(_end(ev))}",
        "details": details_text(ev),
    }
    loc = ev.location or ev.meet_link
    if loc:
        params["location"] = loc
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not out else 74):
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def ics_for(ev: CalendarEvent) -> str:
    uid = hashlib.sha256(f"deminic-event-{ev.id}-{ev.event_no}".encode()).hexdigest()[:24]
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Deminic//Agentic AI//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}@deminic.local",
        f"DTSTAMP:{_gfmt(utcnow())}",
        f"DTSTART:{_gfmt(_aware(ev.starts_at))}",
        f"DTEND:{_gfmt(_end(ev))}",
        f"SUMMARY:{_ics_escape(ev.title)}",
        f"DESCRIPTION:{_ics_escape(details_text(ev))}",
    ]
    loc = ev.location or ev.meet_link
    if loc:
        lines.append(f"LOCATION:{_ics_escape(loc)}")
    if ev.meet_link:
        lines.append(f"URL:{ev.meet_link}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def event_dict(ev: CalendarEvent, tzname: str) -> dict:
    start, end = _aware(ev.starts_at), _end(ev)
    return {
        "event_no": ev.event_no,
        "ref": f"event#{ev.event_no}",
        "section": ev.section,
        "title": ev.title,
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "start_local": fmt_local(start, tzname),
        "end_local": fmt_local(end, tzname),
        "location": ev.location or "",
        "meet_link": ev.meet_link or "",
        "needs_meet_link": bool(ev.needs_meet_link and not ev.meet_link),
        "description": ev.description or "",
        "stale": bool(ev.stale),
        "google_url": gcal_url(ev),
        "ics_url": f"/api/personal/events/{ev.event_no}.ics",
        "meet_new_url": MEET_NEW,
        "source_ref": chat_ref(ev.source_chat_no, ev.source_msg_no) or None,
        "created_at": _aware(ev.created_at).isoformat() if ev.created_at else None,
    }


def describe_event(ev: CalendarEvent, tzname: str) -> str:
    bits = [f"event#{ev.event_no}: {ev.title}", f"starts {fmt_local(_aware(ev.starts_at), tzname)}"]
    if ev.location:
        bits.append(f"at {ev.location}")
    if ev.meet_link:
        bits.append("Google Meet attached")
    return " - ".join(bits)


async def get_event(db: AsyncSession, uid: int, event_no: int) -> CalendarEvent | None:
    return (await db.execute(select(CalendarEvent).where(CalendarEvent.user_id == uid,
                                                         CalendarEvent.event_no == event_no))).scalar_one_or_none()


async def create_event(db: AsyncSession, user: User, *, section: str = "personal", title: str, starts_at: datetime,
                       ends_at: datetime | None = None, location: str = "", description: str = "",
                       meet_link: str = "", wants_meet: bool = False, source_chat_no: int | None = None,
                       source_msg_no: int | None = None) -> CalendarEvent:
    uid, tzname = user.id, user.timezone or "UTC"
    meet = find_meet_link(meet_link) or ""
    no = await next_no(db, uid, "event")
    ev = CalendarEvent(user_id=uid, event_no=no, section=section, title=" ".join(title.split())[:200] or "Event",
                       starts_at=starts_at, ends_at=ends_at, location=(location or "")[:255],
                       description=(description or "")[:4000], meet_link=meet, needs_meet_link=bool(wants_meet and not meet),
                       source_chat_no=source_chat_no, source_msg_no=source_msg_no)
    db.add(ev)
    await db.flush()
    await vectors.upsert(db, uid, section, "event", ev.id, describe_event(ev, tzname), embed_text=ev.title)
    await db.commit()
    await ulog(uid, f"event#{no} '{ev.title}' added for {fmt_local(_aware(starts_at), tzname)}"
                    + (" - waiting for a Google Meet link" if ev.needs_meet_link else ""), src="calendar")
    await emit_user(uid, "calendar:changed", {"event_no": no, "action": "created"})
    return ev


async def attach_meet(db: AsyncSession, user: User, ev: CalendarEvent, link: str) -> CalendarEvent:
    meet = find_meet_link(link)
    if not meet:
        raise ValueError("That is not a Google Meet link (expected https://meet.google.com/abc-defg-hij)")
    ev.meet_link = meet
    ev.needs_meet_link = False
    await vectors.upsert(db, user.id, ev.section, "event", ev.id, describe_event(ev, user.timezone), embed_text=ev.title)
    await db.commit()
    await ulog(user.id, f"Google Meet link attached to event#{ev.event_no}", src="calendar")
    await emit_user(user.id, "calendar:changed", {"event_no": ev.event_no, "action": "meet"})
    return ev


async def latest_pending_meet(db: AsyncSession, uid: int, chat_no: int | None = None) -> CalendarEvent | None:
    rows = (await db.execute(select(CalendarEvent).where(CalendarEvent.user_id == uid, CalendarEvent.needs_meet_link.is_(True))
                             .order_by(CalendarEvent.created_at.desc()).limit(10))).scalars().all()
    if not rows:
        return None
    if chat_no:
        same = [e for e in rows if e.source_chat_no == chat_no]
        if same:
            return same[0]
    return rows[0]


async def update_event(db: AsyncSession, user: User, ev: CalendarEvent, **fields) -> CalendarEvent:
    for k in ("title", "starts_at", "ends_at", "location", "description"):
        if k in fields and fields[k] is not None:
            setattr(ev, k, fields[k])
    if fields.get("meet_link") is not None:
        link = find_meet_link(fields["meet_link"]) if fields["meet_link"] else ""
        if fields["meet_link"] and not link:
            raise ValueError("Not a Google Meet link")
        ev.meet_link = link or ""
        ev.needs_meet_link = bool(ev.needs_meet_link and not ev.meet_link)
    await vectors.upsert(db, user.id, ev.section, "event", ev.id, describe_event(ev, user.timezone), embed_text=ev.title)
    await db.commit()
    await emit_user(user.id, "calendar:changed", {"event_no": ev.event_no, "action": "updated"})
    return ev


async def delete_event(db: AsyncSession, user: User, ev: CalendarEvent) -> None:
    no = ev.event_no
    await vectors.remove(db, user.id, "event", ev.id)
    await db.execute(delete(CalendarEvent).where(CalendarEvent.id == ev.id, CalendarEvent.user_id == user.id))
    await db.commit()
    await ulog(user.id, f"event#{no} deleted", src="calendar")
    await emit_user(user.id, "calendar:changed", {"event_no": no, "action": "deleted"})


async def range_items(db: AsyncSession, uid: int, start: datetime, end: datetime) -> tuple[list[CalendarEvent], list[Task]]:
    """Events overlapping [start, end) + tasks due in it - two queries total."""
    events = (await db.execute(select(CalendarEvent).where(
        CalendarEvent.user_id == uid, CalendarEvent.starts_at < end,
        CalendarEvent.starts_at >= start - timedelta(days=1)).order_by(CalendarEvent.starts_at))).scalars().all()
    events = [e for e in events if _end(e) > start]
    tasks = (await db.execute(select(Task).where(Task.user_id == uid, Task.due_at >= start, Task.due_at < end)
                              .order_by(Task.due_at))).scalars().all()
    return list(events), list(tasks)
