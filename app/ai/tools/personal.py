"""Personal-section tools (tasks, calendar, family, guide, movies, music).

Tool selection is semantic (pgvector over description + examples). Regex is
only used for exact syntaxes (task#N references, Google Meet URLs).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.ai import failsafe
from app.ai.providers import parse_json_loose
from app.ai.tools import ToolCtx, ToolResult, tool
from app.core import vectors
from app.core.crypto_types import key_hash
from app.core.logs import ulog
from app.core.memory import describe, memory_cache
from app.core.sockets import emit_user
from app.core.utils import REF_RE, clip, fmt_local, local_now, next_no, parse_iso, parse_when, utcnow
from app.models import Email, Memory, User
from app.services import calendar_svc, media, memory_service
from app.services import tasks as tasks_svc
from app.services.mailer import user_smtp_configured
from app.security.crypto import decrypt

TASK_SECTIONS = ("personal", "business")
ALL = ("personal", "business", "coding")


# =========================================================================== helpers
def _once(ctx: ToolCtx, name: str) -> bool:
    """True the first time a tool runs in this message; the router sometimes
    emits the same expensive action twice."""
    done = ctx.extra.setdefault("_personal_done", set())
    if name in done:
        return False
    done.add(name)
    return True


def _dup() -> ToolResult:
    return ToolResult(text="(duplicate action skipped - already handled above)")


def _ref_no(text: str, kind: str) -> int | None:
    for m in REF_RE.finditer(text or ""):
        if m.group(1).lower() == kind:
            return int(m.group(2))
    return None


def _as_int(v) -> int | None:  # noqa: ANN001
    try:
        n = int(str(v).strip().lstrip("#").replace("task#", "").replace("event#", ""))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


_RELATIVE_DAY_WORDS = {"today", "tonight", "tomorrow", "tmrw", "tmr"}


def _has_relative_day(text: str) -> bool:
    return any(w.strip(".,!?;:'\"") in _RELATIVE_DAY_WORDS for w in (text or "").lower().split())


def _due(args_value, ctx: ToolCtx) -> datetime | None:  # noqa: ANN001
    """Router ISO first, deterministic parse as fallback / day-sanity check;
    'today 9pm' already past -> tomorrow."""
    due = parse_iso(args_value, ctx.tz) if isinstance(args_value, str) and args_value.strip().lower() not in ("", "null", "none") else None
    det = parse_when(ctx.text, ctx.tz)
    if due is None:
        due = det
    elif det is not None and _has_relative_day(ctx.text):
        tz = local_now(ctx.tz).tzinfo
        ld, dd = due.astimezone(tz), det.astimezone(tz)
        # small models sometimes get "tomorrow" wrong by a day while the time is right
        if (ld.hour, ld.minute) == (dd.hour, dd.minute) and ld.date() != dd.date():
            due = det
    if due is not None and due < utcnow() - timedelta(minutes=1):
        local_due = due.astimezone(local_now(ctx.tz).tzinfo)
        if local_due.date() == local_now(ctx.tz).date():
            due = due + timedelta(days=1)
    return due


def _loc(ctx: ToolCtx, args: dict) -> tuple[str, float | None, float | None]:
    name = str(args.get("location") or "").strip() or ctx.place or ""
    lat = lon = None
    if ctx.location:
        try:
            lat, lon = float(ctx.location["lat"]), float(ctx.location["lon"])
        except (KeyError, TypeError, ValueError):
            lat = lon = None
    return name[:200], lat, lon


def _task_card(action: str, t, tz: str, ctx: ToolCtx, **extra) -> dict:
    return {"type": "task", "action": action, "task": tasks_svc.task_dict(t, tz, behind_no=extra.pop("behind_no", None)),
            "ref": ctx.ref, **extra}


# --------------------------------------------------------------------------- contacts (global memory)
RELATION_ALIASES = {
    "mom": "mother", "mum": "mother", "mummy": "mother", "mama": "mother", "ma": "mother", "maa": "mother",
    "dad": "father", "papa": "father", "daddy": "father", "pa": "father", "bro": "brother", "sis": "sister",
    "hubby": "husband", "wifey": "wife", "grandma": "grandmother", "nani": "grandmother", "dadi": "grandmother",
    "grandpa": "grandfather", "nana": "grandfather", "dada": "grandfather",
}


def _canon(word: str) -> str:
    w = (word or "").lower().strip().replace("my ", "").strip(" .,'\"")
    return RELATION_ALIASES.get(w, w)


def contact_dict(m: Memory | dict) -> dict:
    e = memory_service.entry_of(m) if isinstance(m, Memory) else m
    d = e.get("data") or {}
    return {"id": e["id"], "name": d.get("name") or e["key"], "relation": d.get("relation", ""), "email": d.get("email", ""),
            "phone": d.get("phone", ""), "birthday": d.get("birthday", ""), "notes": d.get("notes", ""),
            "source_ref": (f"chat#{e['source_chat_no']}" + (f".{e['source_msg_no']}" if e.get("source_msg_no") else ""))
            if e.get("source_chat_no") else None, "created_at": e.get("created_at")}


async def list_contacts(db, uid: int) -> list[dict]:
    rows = (await db.execute(select(Memory).where(Memory.user_id == uid, Memory.section == "personal",
                                                  Memory.category == "contact", Memory.deleted_at.is_(None))
                             .order_by(Memory.created_at))).scalars().all()
    return [contact_dict(m) for m in rows]


async def save_contact(db, uid: int, *, name: str, relation: str = "", email: str = "", phone: str = "",
                       birthday: str = "", notes: str = "", source_chat_no: int | None = None,
                       source_msg_no: int | None = None) -> Memory:
    name = " ".join((name or "").split())[:80] or (relation.title() if relation else "Contact")
    data = {"name": name, "relation": (relation or "").strip().lower()[:40], "email": (email or "").strip()[:254],
            "phone": (phone or "").strip()[:40], "birthday": (birthday or "").strip()[:40], "notes": (notes or "").strip()[:500]}
    data = {k: v for k, v in data.items() if v or k in ("name",)}
    return await memory_service.upsert_memory(db, uid, "personal", "contact", name, data.get("relation", ""), data,
                                              source_chat_no=source_chat_no, source_msg_no=source_msg_no)


async def update_contact(db, uid: int, mem_id: int, fields: dict) -> Memory | None:
    m = (await db.execute(select(Memory).where(Memory.id == mem_id, Memory.user_id == uid, Memory.category == "contact",
                                               Memory.deleted_at.is_(None)))).scalar_one_or_none()
    if m is None:
        return None
    data = dict(m.data or {})
    for k in ("name", "relation", "email", "phone", "birthday", "notes"):
        if fields.get(k) is not None:
            data[k] = str(fields[k]).strip()[:500]
    new_key = memory_service.norm_key(data.get("name") or m.key)
    if new_key != m.key:
        clash = (await db.execute(select(Memory.id).where(Memory.user_id == uid, Memory.section == "personal",
                                                          Memory.category == "contact", Memory.deleted_at.is_(None),
                                                          Memory.key_hash == key_hash("contact", new_key),
                                                          Memory.id != m.id))).first()
        if clash:
            raise ValueError("Another family profile already has that name")
        m.key, m.key_hash = new_key, key_hash("contact", new_key)
    m.data = data
    m.value = data.get("relation", "")
    await db.flush()
    entry = memory_service.entry_of(m)
    await vectors.upsert(db, uid, "personal", "memory", m.id, describe(entry), embed_text=describe(entry))
    await db.commit()
    memory_cache.invalidate(uid, "personal")
    return m


async def find_contact(db, uid: int, who: str, vec: list[float] | None = None, model: str = "") -> dict | None:
    """Resolve a recipient by name / relation (O(1) dict) with pgvector as fallback."""
    table = await memory_cache.load(db, uid, "personal")
    contacts = list(table["by_cat"].get("contact", {}).values())
    if not contacts:
        return None
    w = _canon(who or "")
    if w:
        for e in contacts:
            d = e.get("data") or {}
            if w in (_canon(d.get("name", "")), _canon(d.get("relation", "")), e["key"]):
                return e
        for e in contacts:
            d = e.get("data") or {}
            first = (d.get("name") or "").split(" ")[0].lower()
            if first and (first == w or w.startswith(first) or first.startswith(w)):
                return e
    if vec is None and who:
        from app.core.embeddings import embed_one

        vec, model = await embed_one(f"contact {who}")
    if vec is not None:
        hits = await vectors.search_vec(db, uid, "personal", vec, model, k=6, types=["memory"], min_score=0.3)
        for h in hits:
            e = table["by_id"].get(int(h["source_id"]))
            if e and e["category"] == "contact":
                return e
    return None


def email_dict(e: Email) -> dict:
    return {"email_no": e.email_no, "ref": f"email#{e.email_no}", "kind": e.kind, "section": e.section,
            "to_email": e.to_email, "to_name": e.to_name, "subject": e.subject, "body": e.body, "status": e.status,
            "error": e.error or "", "source_ref": (f"chat#{e.source_chat_no}" + (f".{e.source_msg_no}" if e.source_msg_no else ""))
            if e.source_chat_no else None,
            "sent_at": e.sent_at.isoformat() if e.sent_at else None,
            "created_at": e.created_at.isoformat() if e.created_at else None}


EMAIL_SCHEMA = {"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                "required": ["subject", "body"]}


async def draft_family_email(user: User, contact: dict, purpose: str) -> tuple[str, str]:
    d = contact.get("data") or contact
    name = d.get("name") or contact.get("key") or "there"
    rel = d.get("relation") or "family member"
    raw, _ = await failsafe.complete(
        user.settings, "personal",
        [{"role": "system", "content": f"Write a short, warm, natural e-mail from {user.name} to {name} (their {rel}). "
                                       f"Plain text body (no markdown), greet {name.split(' ')[0]}, and sign off as "
                                       f"{user.name.split(' ')[0]}. Keep it under 150 words. JSON only."},
         {"role": "user", "content": f"What the e-mail is about: {clip(purpose, 800)}"}],
        uid=user.id, json_schema=EMAIL_SCHEMA, max_tokens=500, temperature=0.6, allow_online=False)
    data = parse_json_loose(raw) or {}
    subject = str(data.get("subject") or f"A note from {user.name.split(' ')[0]}")[:200]
    body = str(data.get("body") or purpose)[:5000]
    return subject, body


async def create_family_email(db, user: User, *, to_email: str, to_name: str, subject: str, body: str,
                              source_chat_no: int | None = None, source_msg_no: int | None = None) -> Email:
    no = await next_no(db, user.id, "email")
    e = Email(user_id=user.id, email_no=no, section="personal", kind="family", to_email=to_email[:254],
              to_name=to_name[:160], subject=subject[:255], body=body, status="draft",
              source_chat_no=source_chat_no, source_msg_no=source_msg_no)
    db.add(e)
    await db.commit()
    await ulog(user.id, f"email#{no} drafted to {to_name or to_email}", src="family")
    await emit_user(user.id, "email:changed", {"email_no": no, "action": "drafted"})
    return e


# =========================================================================== TASKS
@tool("create_task", "Create a to-do task or reminder for the user (day-to-day tasks with an optional due time)",
      args={"title": "short task title WITHOUT time/date words, e.g. 'Eat dinner'", "due": "ISO-8601 datetime with UTC offset, or null",
            "description": "optional details", "location": "optional place name"},
      sections=TASK_SECTIONS,
      examples=["remind me to eat dinner at 9pm", "add a task to call the bank tomorrow morning",
                "I need to buy groceries this evening", "create a to-do: submit the report by Friday 5pm",
                "remind me to take my medicine at 8am every day", "put pay the electricity bill on my task list",
                "set a reminder to water the plants in 30 minutes", "don't let me forget to call the plumber at 4"])
async def create_task(ctx: ToolCtx, args: dict) -> ToolResult:
    title = str(args.get("title") or "").strip() or ctx.text
    due = _due(args.get("due"), ctx)
    loc, lat, lon = _loc(ctx, args)
    await ctx.log(f"creating task '{clip(title, 60)}'" + (f" due {fmt_local(due, ctx.tz)}" if due else ""))
    res = await tasks_svc.create_task(ctx.db, ctx.user, section=ctx.section, title=title, due_at=due,
                                      description=str(args.get("description") or "")[:2000], location_name=loc,
                                      lat=lat, lon=lon, source_chat_no=ctx.chat_no, source_msg_no=ctx.msg_no)
    t, blocker = res["task"], res["blocker"]
    where = f" (created at {t.location_name})" if t.location_name else ""
    if blocker is not None:
        text = (f"task#{t.task_no} '{t.title}'{' due ' + fmt_local(due, ctx.tz) if due else ''} was created from {ctx.ref} but is "
                f"QUEUED: an active task with the same name already exists (task#{blocker.task_no} '{blocker.title}'"
                f"{', due ' + fmt_local(blocker.due_at, ctx.tz) if blocker.due_at else ''}). It waits in the processing "
                f"queue and activates automatically when task#{blocker.task_no} is marked done.{where}")
        card = _task_card("queued", t, ctx.tz, ctx, behind_no=blocker.task_no)
    else:
        rem = ""
        if res["reminder_id"]:
            rem = (f" A reminder is scheduled for {fmt_local(due, ctx.tz)} (it can be cancelled from Background tasks "
                   f"until 3 minutes before).")
        elif due and due <= utcnow():
            rem = " That time has already passed, so no reminder was scheduled."
        text = f"Created task#{t.task_no} '{t.title}'{' due ' + fmt_local(due, ctx.tz) if due else ''} from {ctx.ref}.{where}{rem}"
        card = _task_card("created", t, ctx.tz, ctx)
    return ToolResult(text=text, cards=[card], refs=[f"task#{t.task_no}"])


@tool("complete_task", "Mark one of the user's tasks as done / completed",
      args={"task": "task title or description, e.g. 'eat dinner'", "task_no": "task number if the user said task#N"},
      sections=TASK_SECTIONS,
      examples=["mark eat dinner as done", "I finished the groceries task", "task#3 is done", "complete the bank call task",
                "I've paid the electricity bill, tick it off", "done with my workout", "check off calling mom"])
async def complete_task(ctx: ToolCtx, args: dict) -> ToolResult:
    no = _as_int(args.get("task_no")) or _ref_no(ctx.text, "task")
    t = await tasks_svc.resolve_task(ctx.db, ctx.uid, ctx.section, task_no=no, query=str(args.get("task") or ctx.text))
    if t is None:
        return ToolResult(ok=False, text="I couldn't find an open task matching that - ask which task (task#N) to mark done.")
    res = await tasks_svc.complete_task(ctx.db, ctx.user, t)
    if res["already"]:
        return ToolResult(text=f"task#{t.task_no} '{t.title}' was already done.", refs=[f"task#{t.task_no}"])
    text = f"Marked task#{t.task_no} '{t.title}' as done."
    cards = [_task_card("completed", t, ctx.tz, ctx)]
    refs = [f"task#{t.task_no}"]
    if res["activated"] is not None:
        a = res["activated"]
        text += (f" The next same-name task in the processing queue, task#{a.task_no} '{a.title}'"
                 f"{' (due ' + fmt_local(a.due_at, ctx.tz) + ')' if a.due_at else ''}, is now active.")
        cards.append(_task_card("activated", a, ctx.tz, ctx))
        refs.append(f"task#{a.task_no}")
    return ToolResult(text=text, cards=cards, refs=refs)


@tool("delete_task", "Delete / cancel one of the user's tasks entirely",
      args={"task": "task title", "task_no": "task number if given"},
      sections=TASK_SECTIONS,
      examples=["delete the groceries task", "remove task#4", "cancel my dentist task, I don't need it",
                "get rid of the plumber reminder task", "drop the task about the report"])
async def delete_task(ctx: ToolCtx, args: dict) -> ToolResult:
    no = _as_int(args.get("task_no")) or _ref_no(ctx.text, "task")
    t = await tasks_svc.resolve_task(ctx.db, ctx.uid, ctx.section, task_no=no, query=str(args.get("task") or ctx.text),
                                     statuses=("active", "queued", "done"))
    if t is None:
        return ToolResult(ok=False, text="I couldn't find that task to delete.")
    no, title = t.task_no, t.title
    res = await tasks_svc.delete_task(ctx.db, ctx.user, t)
    text = f"Deleted task#{no} '{title}'."
    if res["activated"] is not None:
        text += f" task#{res['activated'].task_no} moved out of the processing queue and is now active."
    return ToolResult(text=text, cards=[{"type": "task", "action": "deleted", "task": {"task_no": no, "title": title,
                                                                                         "ref": f"task#{no}"}}])


@tool("list_tasks", "Show the user's tasks / to-do list (active, queued or completed)",
      args={"status": "open (default) | active | queued | done | all", "day": "optional ISO date to filter by due day"},
      sections=TASK_SECTIONS,
      examples=["what are my tasks today", "show my to-do list", "what's pending", "list my queued tasks",
                "what do I have to do tomorrow", "which tasks did I finish", "do I have anything due tonight"])
async def list_tasks(ctx: ToolCtx, args: dict) -> ToolResult:
    if not _once(ctx, "list_tasks"):
        return _dup()
    status = str(args.get("status") or "open").lower()
    if status not in ("open", "active", "queued", "done", "all"):
        status = "open"
    rows = await tasks_svc.list_tasks(ctx.db, ctx.uid, section=None, status=None if status == "all" else status, limit=200)
    day = str(args.get("day") or "").strip()
    if day[:4].isdigit():
        try:
            start, end = tasks_svc.local_day_bounds(day, ctx.tz)
            rows = [t for t in rows if t.due_at and start <= tasks_svc._aware(t.due_at) < end]  # noqa: SLF001
        except ValueError:
            pass
    items = await tasks_svc.dicts(ctx.db, ctx.uid, rows[:40], ctx.tz)
    if not items:
        return ToolResult(text=f"No {status} tasks found.", cards=[{"type": "task_list", "status": status, "tasks": []}])
    lines = [f"task#{i['task_no']} [{i['status']}{', stale' if i['stale'] else ''}] {i['title']}"
             + (f" - due {i['due_local']}" if i['due_local'] else "")
             + (f" - waiting behind task#{i['queued_behind_no']}" if i['queued_behind_no'] else "") for i in items[:25]]
    return ToolResult(text=f"{len(items)} {status} task(s):\n" + "\n".join(lines),
                      cards=[{"type": "task_list", "status": status, "tasks": items[:25]}],
                      refs=[i["ref"] for i in items[:10]])


# =========================================================================== CALENDAR
@tool("create_event", "Add an event / meeting / appointment to the user's calendar (Google Calendar link + .ics). "
                      "Set wants_meet=true when they want a Google Meet video call",
      args={"title": "event title", "start": "ISO-8601 start with UTC offset", "end": "ISO-8601 end or null",
            "location": "optional place", "description": "optional notes", "meet_link": "Google Meet URL if the user gave one",
            "wants_meet": "true if a Google Meet / video call is wanted"},
      sections=TASK_SECTIONS,
      examples=["schedule a meeting with John tomorrow at 3pm", "add a dentist appointment on Friday at 11am to my calendar",
                "set up a Google Meet with the team at 5pm", "book a call with Priya next Monday 10am on google meet",
                "put my sister's wedding on 12 December in the calendar", "create a calendar event for the parent teacher meeting",
                "add lunch with Rahul on Saturday 1pm at Cafe Mocha"])
async def create_event(ctx: ToolCtx, args: dict) -> ToolResult:
    start = _due(args.get("start"), ctx)
    if start is None:
        return ToolResult(ok=False, text="No date/time was given for the event - ask the user when it is.")
    end = parse_iso(args.get("end"), ctx.tz) if isinstance(args.get("end"), str) else None
    meet = calendar_svc.find_meet_link(str(args.get("meet_link") or "")) or calendar_svc.find_meet_link(ctx.text) or ""
    wants = str(args.get("wants_meet")).lower() in ("true", "1", "yes") or "google meet" in ctx.text.lower() or "meet link" in ctx.text.lower()
    ev = await calendar_svc.create_event(ctx.db, ctx.user, section=ctx.section,
                                         title=str(args.get("title") or clip(ctx.text, 80)), starts_at=start, ends_at=end,
                                         location=str(args.get("location") or "")[:255],
                                         description=str(args.get("description") or "")[:2000], meet_link=meet,
                                         wants_meet=wants, source_chat_no=ctx.chat_no, source_msg_no=ctx.msg_no)
    d = calendar_svc.event_dict(ev, ctx.tz)
    text = (f"Created event#{ev.event_no} '{ev.title}' on {d['start_local']} (from {ctx.ref}). The card has an "
            f"'Add to Google Calendar' button and an .ics download.")
    if d["needs_meet_link"]:
        text += (" I CANNOT create Google Meet links myself: ask the user to create one at https://meet.google.com/new "
                 f"and paste the link here - I will attach it to event#{ev.event_no} and the Google Calendar entry.")
    elif ev.meet_link:
        text += f" Google Meet link attached: {ev.meet_link}."
    return ToolResult(text=text, cards=[{"type": "event", "action": "created", "event": d, "ref": ctx.ref}],
                      refs=[f"event#{ev.event_no}"])


@tool("attach_meet_link", "The user pasted a Google Meet link - attach it to the calendar event that is waiting for one",
      args={"meet_link": "the https://meet.google.com/xxx-xxxx-xxx link", "event_no": "event number if mentioned"},
      sections=TASK_SECTIONS,
      examples=["here is the meet link https://meet.google.com/abc-defg-hij",
                "use this google meet link for the meeting https://meet.google.com/xyz-abcd-efg",
                "I created the meet: meet.google.com/kfg-bnvc-hjt", "add this meet link to the event",
                "meet link for event#2 is https://meet.google.com/qwe-rtyu-iop", "done, the google meet is ready: link"])
async def attach_meet_link(ctx: ToolCtx, args: dict) -> ToolResult:
    link = calendar_svc.find_meet_link(str(args.get("meet_link") or "")) or calendar_svc.find_meet_link(ctx.text)
    if not link:
        return ToolResult(ok=False, text="That doesn't contain a valid Google Meet link (https://meet.google.com/abc-defg-hij). "
                                         "Ask the user to create one at https://meet.google.com/new and paste it.")
    no = _as_int(args.get("event_no")) or _ref_no(ctx.text, "event")
    ev = await calendar_svc.get_event(ctx.db, ctx.uid, no) if no else await calendar_svc.latest_pending_meet(ctx.db, ctx.uid, ctx.chat_no)
    if ev is None:
        return ToolResult(ok=False, text="There is no calendar event waiting for a Meet link - ask which event (event#N) it belongs to, "
                                         "or create the event first.")
    await calendar_svc.attach_meet(ctx.db, ctx.user, ev, link)
    d = calendar_svc.event_dict(ev, ctx.tz)
    return ToolResult(text=f"Attached the Google Meet link {link} to event#{ev.event_no} '{ev.title}' ({d['start_local']}). "
                           "The 'Add to Google Calendar' button now includes it.",
                      cards=[{"type": "event", "action": "meet_attached", "event": d, "ref": ctx.ref}],
                      refs=[f"event#{ev.event_no}"])


@tool("list_events", "Show the user's calendar events / schedule for a day or range",
      args={"start": "ISO date or datetime (default today)", "days": "number of days to include (default 1)"},
      sections=TASK_SECTIONS,
      examples=["what's on my calendar this week", "do I have any meetings tomorrow", "show my schedule for Friday",
                "what events do I have today", "am I free on Saturday"])
async def list_events(ctx: ToolCtx, args: dict) -> ToolResult:
    if not _once(ctx, "list_events"):
        return _dup()
    day = str(args.get("start") or "")[:10]
    try:
        start, _ = tasks_svc.local_day_bounds(day if day[:4].isdigit() else None, ctx.tz)
    except ValueError:
        start, _ = tasks_svc.local_day_bounds(None, ctx.tz)
    days = max(1, min(_as_int(args.get("days")) or 1, 31))
    events, tasks = await calendar_svc.range_items(ctx.db, ctx.uid, start, start + timedelta(days=days))
    evs = [calendar_svc.event_dict(e, ctx.tz) for e in events]
    tds = await tasks_svc.dicts(ctx.db, ctx.uid, tasks, ctx.tz)
    lines = [f"event#{e['event_no']} {e['title']} - {e['start_local']}" + (" (needs Meet link)" if e["needs_meet_link"] else "")
             for e in evs] + [f"task#{t['task_no']} {t['title']} - due {t['due_local']} [{t['status']}]" for t in tds]
    text = f"{len(evs)} event(s) and {len(tds)} task(s) between {fmt_local(start, ctx.tz)} and the next {days} day(s)"
    return ToolResult(text=text + (":\n" + "\n".join(lines) if lines else "."),
                      cards=[{"type": "event_list", "events": evs, "tasks": tds, "days": days}])


# =========================================================================== FAMILY
@tool("create_contact", "Create or update a family-member / contact profile (name, relation, e-mail, phone, birthday) "
                        "stored in the user's global memory",
      args={"name": "person's name (or relation if no name)", "relation": "mother|father|brother|sister|wife|husband|son|daughter|friend|...",
            "email": "e-mail address", "phone": "phone number", "birthday": "birthday", "notes": "anything else"},
      sections=("personal",),
      examples=["create a profile for my brother Rahul, his email is rahul@example.com", "save my mom's number 9876543210",
                "add my wife Priya to family contacts", "my dad's email is dad@example.com",
                "make a family profile for my sister Anaya, birthday 4 May", "add my best friend Karan, karan@mail.com",
                "update my mother's phone to 98200 11111"])
async def create_contact(ctx: ToolCtx, args: dict) -> ToolResult:
    relation = _canon(str(args.get("relation") or ""))
    name = str(args.get("name") or "").strip()
    if not name and not relation:
        return ToolResult(ok=False, text="Ask for the person's name or relation.")
    if name and _canon(name) in set(RELATION_ALIASES.values()) | set(RELATION_ALIASES):
        relation = relation or _canon(name)
        name = name.strip().title()
    existing = await find_contact(ctx.db, ctx.uid, name or relation)
    if existing and not name:
        name = (existing.get("data") or {}).get("name") or existing["key"]
    m = await save_contact(ctx.db, ctx.uid, name=name or relation.title(), relation=relation,
                           email=str(args.get("email") or ""), phone=str(args.get("phone") or ""),
                           birthday=str(args.get("birthday") or ""), notes=str(args.get("notes") or ""),
                           source_chat_no=ctx.chat_no, source_msg_no=ctx.msg_no)
    await ctx.db.commit()
    c = contact_dict(m)
    await emit_user(ctx.uid, "memory:changed", {"section": "personal"})
    await ctx.log(f"family profile saved: {c['name']} ({c['relation'] or 'contact'})")
    details = ", ".join(f"{k}: {c[k]}" for k in ("relation", "email", "phone", "birthday") if c.get(k))
    return ToolResult(text=f"Saved the family profile for {c['name']} ({details}) in global memory (from {ctx.ref}).",
                      cards=[{"type": "contact", "action": "saved", "contact": c, "ref": ctx.ref}])


@tool("email_contact", "Write and prepare an e-mail to one of the user's family members / contacts "
                       "(drafted with a Send button that sends through the user's SMTP)",
      args={"recipient": "name or relation, e.g. 'mom' or 'Rahul'", "purpose": "what the e-mail should say",
            "subject": "optional subject", "body": "optional exact body"},
      sections=("personal",),
      examples=["email my mom that I'll be late for dinner", "send my brother a happy birthday email",
                "write to dad about the weekend trip", "mail my sister the flight details: AI 202 at 7am",
                "draft an email to Priya saying I miss her", "send an email to Rahul asking about the party"])
async def email_contact(ctx: ToolCtx, args: dict) -> ToolResult:
    if not _once(ctx, "email_contact"):
        return _dup()
    who = str(args.get("recipient") or "").strip()
    contact = await find_contact(ctx.db, ctx.uid, who, None, "")
    if contact is None:
        return ToolResult(ok=False, text=f"There is no family profile for '{who or 'that person'}'. Ask the user for their "
                                         "name and e-mail so you can create a profile first.")
    d = contact.get("data") or {}
    to_email, to_name = d.get("email", ""), d.get("name") or contact["key"]
    if not to_email:
        return ToolResult(ok=False, text=f"{to_name}'s profile has no e-mail address - ask the user for it.")
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not (subject and body):
        await ctx.log(f"drafting e-mail to {to_name}")
        s2, b2 = await draft_family_email(ctx.user, contact, str(args.get("purpose") or ctx.text))
        subject, body = subject or s2, body or b2
    e = await create_family_email(ctx.db, ctx.user, to_email=to_email, to_name=to_name, subject=subject, body=body,
                                  source_chat_no=ctx.chat_no, source_msg_no=ctx.msg_no)
    smtp = user_smtp_configured(ctx.settings)
    text = (f"Drafted email#{e.email_no} to {to_name} <{to_email}> - subject '{subject}' (from {ctx.ref}). It is saved as a "
            f"draft; the user presses Send on the card to send it via their SMTP."
            + ("" if smtp else " NOTE: SMTP is not configured yet - they must add it in Settings > E-mail before sending."))
    return ToolResult(text=text, cards=[{"type": "email_draft", "email": email_dict(e), "smtp": smtp, "ref": ctx.ref}],
                      refs=[f"email#{e.email_no}"])


# =========================================================================== GUIDE
GUIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "place": {"type": "string"}, "intro": {"type": "string"},
        "dishes": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "description": {"type": "string"}, "where": {"type": "string"}},
            "required": ["name", "description"]}},
        "places": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "why": {"type": "string"}}, "required": ["name", "why"]}},
        "tips": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
        "suggested_tasks": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
    },
    "required": ["place", "dishes", "places", "tips"],
}


@tool("travel_guide", "Local guide for where the user is or is going: must-try local dishes, places to visit, tips "
                      "(can be turned into tasks)",
      args={"place": "city / area (empty = user's current location)", "interests": "optional focus e.g. street food, vegetarian"},
      sections=("personal",),
      examples=["I just landed in Tokyo, what should I eat?", "I'm in Jaipur, recommend local dishes",
                "what are the must-try foods here", "visiting Goa this weekend, any tips?", "guide me around Paris",
                "what's famous to eat in Lucknow", "I'm at the beach in Kerala, what should I do here"])
async def travel_guide(ctx: ToolCtx, args: dict) -> ToolResult:
    if not _once(ctx, "travel_guide"):
        return _dup()
    place = str(args.get("place") or "").strip() or ctx.place
    if not place:
        return ToolResult(ok=False, text="The user's location is unknown - ask where they are (or turn on location sharing).")
    interests = str(args.get("interests") or "").strip()
    await ctx.log(f"guide: researching {place}")
    try:
        raw, _ = await failsafe.complete(
            ctx.settings, "personal",
            [{"role": "system", "content": "You are an expert local travel and food guide. Recommend real, specific local "
                                           "dishes (with where to try them), places and practical tips. Be accurate and "
                                           "concise. suggested_tasks are short to-do titles. JSON only."},
             {"role": "user", "content": f"Place: {place}. Interests: {interests or 'local food and highlights'}. "
                                         f"Local time: {local_now(ctx.tz).strftime('%A %H:%M')}."}],
            uid=ctx.uid, json_schema=GUIDE_SCHEMA, max_tokens=900, temperature=0.5, allow_online=False)
    except failsafe.AIUnavailable:
        return ToolResult(ok=False, text="The guide could not be generated right now.")
    g = parse_json_loose(raw) or {}
    g["place"] = g.get("place") or place
    dishes = [d for d in g.get("dishes") or [] if isinstance(d, dict) and d.get("name")][:6]
    places = [p for p in g.get("places") or [] if isinstance(p, dict) and p.get("name")][:5]
    tips = [str(t) for t in g.get("tips") or []][:5]
    sugg = [str(t)[:120] for t in g.get("suggested_tasks") or []][:4] or [f"Try {d['name']}" for d in dishes[:3]]
    card = {"type": "guide", "place": g["place"], "intro": str(g.get("intro") or "")[:400], "dishes": dishes, "places": places,
            "tips": tips, "suggested_tasks": sugg, "ref": ctx.ref}
    text = (f"Guide for {g['place']}: dishes - " + "; ".join(f"{d['name']} ({clip(d.get('description', ''), 80)})" for d in dishes)
            + ". Places - " + "; ".join(p["name"] for p in places) + ". Tips - " + " | ".join(tips)
            + ". The card lets the user add suggestions as tasks.")
    return ToolResult(text=text, cards=[card])


# =========================================================================== MOVIES & MUSIC
@tool("recommend_movies", "Recommend movies to watch (by genre, mood, or similar titles)",
      args={"request": "what kind of movies", "count": "how many (default 6)"},
      sections=("personal",),
      examples=["recommend some sci-fi movies", "suggest a feel-good movie for tonight", "movies like Inception",
                "what should I watch this weekend", "best thrillers from the 90s", "recommend a bollywood comedy for family night"])
async def recommend_movies(ctx: ToolCtx, args: dict) -> ToolResult:
    if not _once(ctx, "recommend_movies"):
        return _dup()
    req = str(args.get("request") or ctx.text)
    try:
        movies = await media.recommend_movies(ctx.settings, ctx.uid, req, _as_int(args.get("count")) or 6)
    except failsafe.AIUnavailable:
        return ToolResult(ok=False, text="Couldn't generate recommendations right now.")
    if not movies:
        return ToolResult(ok=False, text="No recommendations could be generated.")
    await ctx.log(f"recommended {len(movies)} movie(s)")
    return ToolResult(text="Recommended: " + "; ".join(f"{m['title']} ({m['year']}) - {m['why']}" for m in movies)
                           + ". Each card item has Play (YouTube) and Where-to-watch buttons.",
                      cards=[{"type": "movie_list", "movies": movies, "request": clip(req, 120)}])


async def _movie_card(ctx: ToolCtx, title: str, year: str, autoplay: bool) -> tuple[dict, str]:
    yt_key = decrypt(ctx.settings.youtube_api_key_enc)
    video, kind, alternatives = await media.find_movie_video(title, year, yt_key)
    watch = await media.where_to_watch(ctx.settings, title, year)
    if video and autoplay:
        await emit_user(ctx.uid, "player:play", {"kind": "youtube", "videoId": video["videoId"], "title": video["title"] or title})
        await ctx.log(f"playing {kind} '{clip(video['title'], 60)}' from YouTube")
    card = {"type": "movie", "title": watch.get("title") or title, "year": watch.get("year") or year, "video": video,
            "video_kind": kind, "alternatives": alternatives, "watch": watch, "autoplay": bool(video and autoplay),
            "youtube_search": media.youtube_search_url(f"{title} {year} full movie")}
    providers = ", ".join(p["name"] for p in watch.get("flatrate", [])) or ""
    what = "the full movie" if kind == "movie" else "the official trailer (the full movie isn't on YouTube)"
    status = (f"Playing {what} from YouTube in the player: '{video['title']}'." if video and autoplay else
              f"On YouTube there is {what}: '{video['title']}'." if video else "Couldn't find it on YouTube.")
    where = (f" Streaming in {watch['region']}: {providers}." if providers else
             f" Where-to-watch search links (Netflix, Prime Video, Hotstar, Apple TV, JustWatch) are on the card.")
    return card, status + where


@tool("play_movie", "Play a movie (playback ONLY from YouTube) and show where it streams",
      args={"title": "movie title", "year": "release year if known"},
      sections=("personal",),
      examples=["play Inception", "I want to watch The Matrix now", "play the movie Interstellar", "stream Oppenheimer for me",
                "put on Dangal", "start the movie 3 Idiots"])
async def play_movie(ctx: ToolCtx, args: dict) -> ToolResult:
    if not _once(ctx, "play_movie"):
        return _dup()
    title = str(args.get("title") or "").strip()
    if not title:
        return ToolResult(ok=False, text="Ask which movie to play.")
    card, text = await _movie_card(ctx, title, str(args.get("year") or ""), autoplay=True)
    return ToolResult(text=text, cards=[card])


@tool("where_to_watch", "Tell the user where a movie can be streamed (Netflix, Prime Video, Hotstar ...) without playing it",
      args={"title": "movie title", "year": "release year if known"},
      sections=("personal",),
      examples=["where can I watch Dangal", "is Inception on Netflix", "which app has Oppenheimer",
                "where is The Office streaming", "can I watch Interstellar on Prime Video"])
async def where_to_watch(ctx: ToolCtx, args: dict) -> ToolResult:
    if not _once(ctx, "where_to_watch"):
        return _dup()
    title = str(args.get("title") or "").strip()
    if not title:
        return ToolResult(ok=False, text="Ask which movie.")
    card, text = await _movie_card(ctx, title, str(args.get("year") or ""), autoplay=False)
    return ToolResult(text=text, cards=[card])


@tool("play_music", "Play music / songs / radio by genre, mood or a specific song - it starts playing alongside the chat",
      args={"query": "genre, mood or song e.g. 'synthwave' or 'Blinding Lights The Weeknd'",
            "kind": "genre (default) or song"},
      sections=ALL,
      examples=["play a synthwave song", "play some lofi beats", "put on relaxing jazz", "play Blinding Lights by The Weeknd",
                "I want to listen to bollywood music", "play some music for focus", "play classical music",
                "start a radio station with rock songs"])
async def play_music(ctx: ToolCtx, args: dict) -> ToolResult:
    if not _once(ctx, "play_music"):
        return _dup()
    query = str(args.get("query") or ctx.text).strip()[:120]
    kind = str(args.get("kind") or "genre").lower()
    yt_key = decrypt(ctx.settings.youtube_api_key_enc)
    station = None
    if kind != "song" and " by " not in query.lower():
        stations = await media.radio_search(query, limit=8)
        station = stations[0] if stations else None
        if station:
            await emit_user(ctx.uid, "player:play", {"kind": "audio", "title": station["name"], "url": station["url"],
                                                    "subtitle": f"{query} radio · {station['country'] or 'online'}",
                                                    "homepage": station["homepage"], "favicon": station["favicon"]})
            await ctx.log(f"music: streaming '{station['name']}' ({station['codec']} {station['bitrate']}kbps)")
            return ToolResult(text=f"Now playing the '{station['name']}' {query} radio stream in the player - it keeps playing "
                                   "while the user chats or switches tabs.",
                              cards=[{"type": "music", "query": query, "station": station, "more": stations[1:5]}])
    videos = await media.youtube_search(query, yt_key, limit=4)
    if videos:
        v = videos[0]
        await emit_user(ctx.uid, "player:play", {"kind": "youtube", "videoId": v["videoId"], "title": v["title"] or query})
        await ctx.log(f"music: playing '{clip(v['title'], 60)}' from YouTube")
        return ToolResult(text=f"Now playing '{v['title']}' from YouTube in the player.",
                          cards=[{"type": "music", "query": query, "video": v, "more_videos": videos[1:4]}])
    return ToolResult(ok=False, text=f"Couldn't find any stream for '{query}' right now.",
                      cards=[{"type": "music", "query": query, "error": "nothing found",
                              "youtube_search": media.youtube_search_url(query)}])
