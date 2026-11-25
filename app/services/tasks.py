"""Day-to-day tasks: same-name processing queue + cancellable reminders.

Same-name rule
--------------
"eat dinner at 8pm" and "eat dinner at 9pm" share the name key "eat dinner".
While a task with that key is *active*, a new one is created with status
"queued" (queued_behind_id -> the active task) and is put into an in-memory
processing queue that lives for the whole runtime of the app (rebuilt from the
database on startup). Completing / deleting the active task activates the next
queued one (status active, reminder scheduled, task:changed + notify).
Name keys come from simple token normalisation of the router's clean title;
pgvector similarity >= 0.9 between titles is the semantic fallback.

Reminders
---------
One background job per task with a due time: kind "reminder",
start_at = due_at, cancel_deadline = due_at - 3 min (JobManager refuses later
cancels). A user cancel sets reminder_cancelled so it is never rescheduled.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import hooks, vectors
from app.core.embeddings import embed_one
from app.core.jobs import JobContext, deadline_before, jobs
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.core.utils import chat_ref, fmt_local, next_no, tz_for, utcnow
from app.db import SessionLocal
from app.models import Task, User

log = logging.getLogger("deminic.tasks")

STATUSES = ("active", "queued", "done", "cancelled")

# --------------------------------------------------------------------------- name keys (no regex)
_FILLER_PREFIXES = (
    "please remind me to ", "remind me to ", "remind me ", "reminder to ", "reminder: ", "reminder ", "i need to ",
    "i have to ", "i must ", "i should ", "please ", "add a task to ", "add task to ", "create a task to ",
    "a task to ", "task to ", "task: ", "to do: ", "to-do: ", "todo: ", "to do ", "todo ", "don't forget to ",
    "dont forget to ", "remember to ", "i want to ", "i'll ", "i will ", "let me ", "go ",
)
_DROP = set(
    "a an the my at by on around about till until today tonight tomorrow tmrw tmr morning evening afternoon night "
    "noon midnight am pm a.m p.m oclock o'clock this next every daily weekly week weekend in min mins minute minutes "
    "hr hrs hour hours day days monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur "
    "thurs fri sat sun january february march april may june july august september october november december jan feb "
    "mar apr jun jul aug sep sept oct nov dec".split()
)
_STRIP = "!?.,;:'\"()[]{}"


def _is_time_token(w: str) -> bool:
    t = w.lower().rstrip(".")
    for suf in ("a.m", "p.m", "am", "pm", "hrs", "hr", "h", "st", "nd", "rd", "th"):
        if t.endswith(suf) and len(t) > len(suf):
            core = t[: -len(suf)]
            if core.replace(":", "").replace(".", "").isdigit():
                return True
    core = t.replace(":", "").replace("-", "").replace("/", "").replace(".", "")
    return core.isdigit()


def name_key(title: str) -> str:
    """'Remind me to eat dinner at 9pm' -> 'eat dinner'."""
    s = " ".join((title or "").lower().split())
    changed = True
    while changed:
        changed = False
        for p in _FILLER_PREFIXES:
            if s.startswith(p):
                s = s[len(p):]
                changed = True
    words = []
    for raw in s.split():
        w = raw.strip(_STRIP)
        if not w or w in _DROP or _is_time_token(w):
            continue
        words.append(w)
    key = " ".join(words).strip()
    return (key or s or "task")[:200]


def clean_title(title: str) -> str:
    t = " ".join((title or "").split()).strip(" .")
    low = t.lower()
    for p in _FILLER_PREFIXES:
        if low.startswith(p) and len(t) > len(p) + 1:
            t = t[len(p):]
            break
    return (t[:1].upper() + t[1:])[:200] if t else "Untitled task"


# --------------------------------------------------------------------------- in-memory processing queue
class ProcessingQueue:
    """(user_id, section, name_key) -> deque[task_id]; lives for the whole app runtime."""

    def __init__(self) -> None:
        self._q: dict[tuple[int, str, str], deque[int]] = defaultdict(deque)
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, uid: int) -> asyncio.Lock:
        lk = self._locks.get(uid)
        if lk is None:
            lk = self._locks[uid] = asyncio.Lock()
        return lk

    def push(self, uid: int, section: str, key: str, task_id: int) -> None:
        d = self._q[(uid, section, key)]
        if task_id not in d:
            d.append(task_id)

    def remove(self, uid: int, task_id: int) -> None:
        for k in [k for k in self._q if k[0] == uid]:
            d = self._q[k]
            if task_id in d:
                d.remove(task_id)
            if not d:
                self._q.pop(k, None)

    def pop(self, uid: int, section: str, key: str) -> int | None:
        d = self._q.get((uid, section, key))
        if not d:
            return None
        tid = d.popleft()
        if not d:
            self._q.pop((uid, section, key), None)
        return tid

    def peek_all(self, uid: int, section: str, key: str) -> list[int]:
        return list(self._q.get((uid, section, key), ()))

    def position(self, uid: int, task_id: int) -> tuple[int, int] | None:
        for k, d in self._q.items():
            if k[0] == uid and task_id in d:
                return list(d).index(task_id) + 1, len(d)
        return None

    def keys(self) -> list[tuple[int, str, str]]:
        return [k for k, d in self._q.items() if d]

    def items(self) -> list[tuple[tuple[int, str, str], list[int]]]:
        return [(k, list(d)) for k, d in self._q.items()]

    def all_ids(self) -> set[int]:
        return {i for d in self._q.values() for i in d}

    def snapshot(self, uid: int) -> list[dict]:
        return [{"section": k[1], "name_key": k[2], "task_ids": list(d)} for k, d in self._q.items() if k[0] == uid and d]

    def clear(self) -> None:
        self._q.clear()


queue = ProcessingQueue()
_bg: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


# --------------------------------------------------------------------------- serialisation
def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def describe_task(t: Task, tzname: str) -> str:
    bits = [f"task#{t.task_no}: {t.title}"]
    if t.due_at:
        bits.append(f"due {fmt_local(t.due_at, tzname)}")
    if t.location_name:
        bits.append(f"created at {t.location_name}")
    bits.append(f"[{t.status}{', stale' if t.stale else ''}]")
    return " - ".join(bits)


def reminder_info(uid: int, job_id: str | None) -> dict | None:
    if not job_id:
        return None
    h = jobs.get(uid, job_id)
    if h is None:
        return None
    p = h.public()
    return {"request_id": p["request_id"], "status": p["status"], "can_cancel": p["can_cancel"],
            "cancel_deadline": p["cancel_deadline"], "cancel_reason": p["cancel_reason"]}


def task_dict(t: Task, tzname: str, *, behind_no: int | None = None) -> dict:
    due = _aware(t.due_at)
    pos = queue.position(t.user_id, t.id) if t.status == "queued" else None
    return {
        "task_no": t.task_no,
        "ref": f"task#{t.task_no}",
        "section": t.section,
        "title": t.title,
        "name_key": t.name_key,
        "description": t.description or "",
        "status": t.status,
        "stale": bool(t.stale),
        "due_at": due.isoformat() if due else None,
        "due_local": fmt_local(due, tzname) if due else "",
        "overdue": bool(t.status == "active" and due and due < utcnow()),
        "location_name": t.location_name or "",
        "latitude": t.latitude,
        "longitude": t.longitude,
        "source_ref": chat_ref(t.source_chat_no, t.source_msg_no) or None,
        "queued_behind_no": behind_no,
        "queue_position": {"position": pos[0], "length": pos[1]} if pos else None,
        "reminder": reminder_info(t.user_id, t.reminder_job_id) if t.status == "active" else None,
        "reminder_cancelled": bool(t.reminder_cancelled),
        "reminded_at": _aware(t.reminded_at).isoformat() if t.reminded_at else None,
        "created_at": _aware(t.created_at).isoformat() if t.created_at else None,
        "completed_at": _aware(t.completed_at).isoformat() if t.completed_at else None,
    }


async def behind_numbers(db: AsyncSession, uid: int, rows: list[Task]) -> dict[int, int]:
    """task id -> task_no for every queued_behind_id (ONE query, no N+1)."""
    known = {t.id: t.task_no for t in rows}
    missing = {t.queued_behind_id for t in rows if t.queued_behind_id and t.queued_behind_id not in known}
    if missing:
        for tid, no in (await db.execute(select(Task.id, Task.task_no).where(Task.user_id == uid, Task.id.in_(missing)))).all():
            known[tid] = no
    return known


async def dicts(db: AsyncSession, uid: int, rows: list[Task], tzname: str) -> list[dict]:
    nos = await behind_numbers(db, uid, rows)
    return [task_dict(t, tzname, behind_no=nos.get(t.queued_behind_id) if t.queued_behind_id else None) for t in rows]


# --------------------------------------------------------------------------- reminders
def kill_reminder(uid: int, job_id: str | None, reason: str) -> None:
    """Internal (system) cancel - bypasses the 3-minute user cancel window."""
    if not job_id:
        return
    h = jobs.get(uid, job_id)
    if h and h.task and not h.task.done() and h.status in ("running", "scheduled"):
        h.cancel_reason = reason
        h.task.cancel()


async def _mark_reminder_cancelled(uid: int, task_id: int, no: int) -> None:
    async with SessionLocal() as db:
        await db.execute(update(Task).where(Task.id == task_id, Task.user_id == uid).values(reminder_cancelled=True))
        await db.commit()
    await ulog(uid, f"reminder for task#{no} cancelled by you - it will not be rescheduled", src="tasks")
    await emit_user(uid, "task:changed", {"task_no": no, "action": "reminder_cancelled"})


async def _fire_reminder(ctx: JobContext, task_id: int) -> dict:
    uid = ctx.user_id
    async with SessionLocal() as db:
        t = (await db.execute(select(Task).where(Task.id == task_id, Task.user_id == uid))).scalar_one_or_none()
        if t is None:
            await ctx.log("reminder skipped - task was deleted")
            return {"skipped": "deleted"}
        if t.status != "active":
            await ctx.log(f"reminder skipped - task#{t.task_no} is {t.status}")
            return {"skipped": t.status}
        if t.stale:
            await ctx.log(f"reminder skipped - task#{t.task_no} is stale (its memory was deleted)")
            return {"skipped": "stale"}
        tzname = (await db.execute(select(User.timezone).where(User.id == uid))).scalar_one_or_none() or "UTC"
        t.reminded_at = utcnow()
        no, title, loc, due = t.task_no, t.title, t.location_name, _aware(t.due_at)
        await db.commit()
    when = fmt_local(due, tzname) if due else ""
    await emit_user(uid, "reminder", {"title": title, "when": when, "ref": f"task#{no}", "kind": "task",
                                      "body": f"task#{no} is due now" + (f" · {loc}" if loc else "")})
    await emit_user(uid, "task:changed", {"task_no": no, "action": "reminded"})
    await ctx.log(f"⏰ reminder fired for task#{no} '{title}'")
    return {"task_no": no, "fired": True}


async def schedule_reminder(uid: int, t: Task, tzname: str) -> str | None:
    """Spawn the reminder job for an active task with a future due time."""
    due = _aware(t.due_at)
    if not due or t.status != "active" or t.stale or t.reminder_cancelled or due <= utcnow():
        return None
    task_id, no, title = t.id, t.task_no, t.title

    async def _fn(ctx: JobContext):
        return await _fire_reminder(ctx, task_id)

    h = await jobs.spawn(uid, "reminder", f"reminder • task#{no} {title}"[:200], _fn, cancellable=True,
                         cancel_deadline=deadline_before(due), start_at=due, retries=1,
                         meta={"task_no": no, "ref": f"task#{no}", "due_at": due.isoformat(), "section": t.section})

    def _done(_fut) -> None:
        if h.status == "cancelled" and (h.cancel_reason or "").startswith("cancelled by user"):
            _spawn_bg(_mark_reminder_cancelled(uid, task_id, no))

    if h.task is not None:
        h.task.add_done_callback(_done)
    dl = deadline_before(due)
    await ulog(uid, f"reminder scheduled for task#{no} at {fmt_local(due, tzname)} "
                    f"(cancellable until {fmt_local(dl, tzname)})", src="tasks")
    return h.id


# --------------------------------------------------------------------------- queries
async def get_task(db: AsyncSession, uid: int, task_no: int) -> Task | None:
    return (await db.execute(select(Task).where(Task.user_id == uid, Task.task_no == task_no))).scalar_one_or_none()


async def find_blocker(db: AsyncSession, uid: int, section: str, key: str, title: str, *,
                       exclude_id: int | None = None) -> Task | None:
    """Active task with the same name (exact key first, pgvector >= 0.9 second)."""
    stmt = select(Task).where(Task.user_id == uid, Task.section == section, Task.status == "active", Task.name_key == key)
    if exclude_id:
        stmt = stmt.where(Task.id != exclude_id)
    t = (await db.execute(stmt.order_by(Task.created_at).limit(1))).scalar_one_or_none()
    if t is not None:
        return t
    vec, model = await embed_one(title)
    hits = await vectors.search_vec(db, uid, section, vec, model, k=5, types=["task"], min_score=0.9)
    ids = [h["source_id"] for h in hits if h["source_id"] != exclude_id]
    if not ids:
        return None
    rows = (await db.execute(select(Task).where(Task.user_id == uid, Task.id.in_(ids), Task.status == "active"))).scalars().all()
    if not rows:
        return None
    rank = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda r: rank.get(r.id, 99))[0]


async def resolve_task(db: AsyncSession, uid: int, section: str | None, *, task_no: int | None = None,
                       query: str | None = None, statuses: tuple[str, ...] = ("active", "queued")) -> Task | None:
    if task_no:
        return await get_task(db, uid, int(task_no))
    if not query:
        return None
    key = name_key(query)
    stmt = select(Task).where(Task.user_id == uid, Task.status.in_(statuses), Task.name_key == key)
    rows = (await db.execute(stmt.order_by(Task.status, Task.created_at))).scalars().all()
    if section:
        rows = sorted(rows, key=lambda r: r.section != section)
    if rows:
        return rows[0]
    vec, model = await embed_one(query)
    for sec in ([section] if section else []) + [s for s in ("personal", "business", "coding") if s != section]:
        hits = await vectors.search_vec(db, uid, sec, vec, model, k=8, types=["task"], min_score=0.45)
        ids = [h["source_id"] for h in hits]
        if not ids:
            continue
        cand = (await db.execute(select(Task).where(Task.user_id == uid, Task.id.in_(ids), Task.status.in_(statuses)))).scalars().all()
        if cand:
            rank = {i: n for n, i in enumerate(ids)}
            return sorted(cand, key=lambda r: rank.get(r.id, 99))[0]
    return None


async def list_tasks(db: AsyncSession, uid: int, *, section: str | None = None, status: str | None = None,
                     limit: int = 500) -> list[Task]:
    stmt = select(Task).where(Task.user_id == uid)
    if section:
        stmt = stmt.where(Task.section == section)
    if status in STATUSES:
        stmt = stmt.where(Task.status == status)
    elif status == "open":
        stmt = stmt.where(Task.status.in_(("active", "queued")))
    rows = list((await db.execute(stmt.order_by(Task.created_at.desc()).limit(limit))).scalars().all())
    order = {"active": 0, "queued": 1, "done": 2, "cancelled": 3}
    far = datetime(9999, 1, 1, tzinfo=timezone.utc)
    rows.sort(key=lambda t: (order.get(t.status, 9), _aware(t.due_at) or far))
    return rows


# --------------------------------------------------------------------------- mutations
async def create_task(db: AsyncSession, user: User, *, section: str = "personal", title: str, due_at: datetime | None = None,
                      description: str = "", location_name: str = "", lat: float | None = None,
                      lon: float | None = None, source_chat_no: int | None = None,
                      source_msg_no: int | None = None) -> dict:
    uid, tzname = user.id, user.timezone or "UTC"
    title = clean_title(title)
    key = name_key(title)
    async with queue.lock(uid):
        blocker = await find_blocker(db, uid, section, key, title)
        if blocker is not None:
            key = blocker.name_key  # semantic match -> share the blocker's queue
        no = await next_no(db, uid, "task")
        t = Task(user_id=uid, task_no=no, section=section, title=title, name_key=key,
                 description=(description or "")[:4000], due_at=due_at, status="queued" if blocker else "active",
                 queued_behind_id=blocker.id if blocker else None, location_name=(location_name or "")[:200],
                 latitude=lat, longitude=lon, source_chat_no=source_chat_no, source_msg_no=source_msg_no)
        db.add(t)
        await db.flush()
        await vectors.upsert(db, uid, section, "task", t.id, describe_task(t, tzname), embed_text=title)
        await db.commit()
        reminder_id = None
        if blocker is not None:
            queue.push(uid, section, key, t.id)
        else:
            reminder_id = await schedule_reminder(uid, t, tzname)
            if reminder_id:
                t.reminder_job_id = reminder_id
                await db.commit()
    where = f" at {t.location_name}" if t.location_name else ""
    if blocker is not None:
        await ulog(uid, f"task#{no} '{title}' QUEUED behind active task#{blocker.task_no} (same name '{key}'){where}", src="tasks")
    else:
        await ulog(uid, f"task#{no} '{title}' created{where}" + (f", due {fmt_local(due_at, tzname)}" if due_at else ""), src="tasks")
    await emit_user(uid, "task:changed", {"task_no": no, "action": "queued" if blocker else "created"})
    return {"task": t, "blocker": blocker, "reminder_id": reminder_id}


async def activate_next(db: AsyncSession, user: User, section: str, key: str) -> Task | None:
    """Promote the next queued same-name task (if no other active one exists)."""
    uid, tzname = user.id, user.timezone or "UTC"
    activated: Task | None = None
    async with queue.lock(uid):
        still = (await db.execute(select(Task.id).where(Task.user_id == uid, Task.section == section,
                                                        Task.name_key == key, Task.status == "active").limit(1))).first()
        if still:
            return None
        while True:
            nid = queue.pop(uid, section, key)
            if nid is None:
                return None
            t = (await db.execute(select(Task).where(Task.id == nid, Task.user_id == uid))).scalar_one_or_none()
            if t is None or t.status != "queued":
                continue
            t.status = "active"
            t.queued_behind_id = None
            rest = queue.peek_all(uid, section, key)
            if rest:
                await db.execute(update(Task).where(Task.user_id == uid, Task.id.in_(rest)).values(queued_behind_id=t.id))
            await vectors.upsert(db, uid, t.section, "task", t.id, describe_task(t, tzname), embed_text=t.title)
            await db.commit()
            rid = await schedule_reminder(uid, t, tzname)
            if rid:
                t.reminder_job_id = rid
                await db.commit()
            activated = t
            break
    await ulog(uid, f"processing queue: task#{activated.task_no} '{activated.title}' is now ACTIVE", src="tasks")
    await emit_user(uid, "task:changed", {"task_no": activated.task_no, "action": "activated"})
    await emit_user(uid, "notify", {"message": f"task#{activated.task_no} '{activated.title}' left the queue and is now active",
                                    "kind": "ok", "ms": 7000})
    return activated


async def complete_task(db: AsyncSession, user: User, t: Task) -> dict:
    uid, tzname = user.id, user.timezone or "UTC"
    if t.status == "done":
        return {"task": t, "already": True, "activated": None}
    prev, section, key = t.status, t.section, t.name_key
    t.status = "done"
    t.completed_at = utcnow()
    t.queued_behind_id = None
    kill_reminder(uid, t.reminder_job_id, "task completed")
    if prev == "queued":
        queue.remove(uid, t.id)
    await vectors.upsert(db, uid, section, "task", t.id, describe_task(t, tzname), embed_text=t.title)
    await db.commit()
    await ulog(uid, f"task#{t.task_no} '{t.title}' marked DONE", src="tasks")
    await emit_user(uid, "task:changed", {"task_no": t.task_no, "action": "done"})
    activated = await activate_next(db, user, section, key) if prev == "active" else None
    return {"task": t, "already": False, "activated": activated}


async def reopen_task(db: AsyncSession, user: User, t: Task) -> dict:
    """Undo 'done': goes back to active, or queued if a same-name task is active."""
    uid, tzname = user.id, user.timezone or "UTC"
    if t.status not in ("done", "cancelled"):
        return {"task": t}
    async with queue.lock(uid):
        blocker = await find_blocker(db, uid, t.section, t.name_key, t.title, exclude_id=t.id)
        t.completed_at = None
        if blocker:
            t.status, t.queued_behind_id = "queued", blocker.id
            queue.push(uid, t.section, blocker.name_key, t.id)
        else:
            t.status = "active"
        await vectors.upsert(db, uid, t.section, "task", t.id, describe_task(t, tzname), embed_text=t.title)
        await db.commit()
        if t.status == "active":
            rid = await schedule_reminder(uid, t, tzname)
            if rid:
                t.reminder_job_id = rid
                await db.commit()
    await emit_user(uid, "task:changed", {"task_no": t.task_no, "action": "reopened"})
    return {"task": t, "blocker": blocker}


async def delete_task(db: AsyncSession, user: User, t: Task) -> dict:
    uid = user.id
    prev, section, key, no, title = t.status, t.section, t.name_key, t.task_no, t.title
    kill_reminder(uid, t.reminder_job_id, "task deleted")
    queue.remove(uid, t.id)
    await vectors.remove(db, uid, "task", t.id)
    await db.execute(delete(Task).where(Task.id == t.id, Task.user_id == uid))
    await db.commit()
    await ulog(uid, f"task#{no} '{title}' deleted", src="tasks")
    await emit_user(uid, "task:changed", {"task_no": no, "action": "deleted"})
    activated = await activate_next(db, user, section, key) if prev == "active" else None
    return {"task_no": no, "activated": activated}


_UNSET = object()


async def update_task(db: AsyncSession, user: User, t: Task, *, title: str | None = None, description: str | None = None,
                      due_at=_UNSET, location_name: str | None = None) -> dict:
    uid, tzname = user.id, user.timezone or "UTC"
    old_key, changed = t.name_key, []
    if title is not None and clean_title(title) != t.title:
        t.title = clean_title(title)
        changed.append("title")
    if description is not None and description != (t.description or ""):
        t.description = description[:4000]
        changed.append("description")
    if location_name is not None and location_name != (t.location_name or ""):
        t.location_name = location_name[:200]
        changed.append("location")
    due_changed = due_at is not _UNSET and _aware(due_at) != _aware(t.due_at)
    if due_changed:
        t.due_at = due_at
        t.reminded_at = None
        t.reminder_cancelled = False
        kill_reminder(uid, t.reminder_job_id, "task rescheduled")
        t.reminder_job_id = None
        changed.append("due")
    new_key = name_key(t.title) if "title" in changed else old_key
    activated = None
    if new_key != old_key:
        t.name_key = new_key
    await vectors.upsert(db, uid, t.section, "task", t.id, describe_task(t, tzname), embed_text=t.title)
    await db.commit()
    if new_key != old_key:
        if t.status == "active":
            activated = await activate_next(db, user, t.section, old_key)
        elif t.status == "queued":
            queue.remove(uid, t.id)
            blocker = await find_blocker(db, uid, t.section, new_key, t.title, exclude_id=t.id)
            if blocker:
                t.queued_behind_id = blocker.id
                queue.push(uid, t.section, blocker.name_key, t.id)
            else:
                t.status, t.queued_behind_id = "active", None
            await db.commit()
    if t.status == "active" and (due_changed or (new_key != old_key and not t.reminder_job_id)):
        rid = await schedule_reminder(uid, t, tzname)
        if rid:
            t.reminder_job_id = rid
            await db.commit()
    if changed:
        await ulog(uid, f"task#{t.task_no} updated ({', '.join(changed)})", src="tasks")
        await emit_user(uid, "task:changed", {"task_no": t.task_no, "action": "updated"})
    return {"task": t, "changed": changed, "activated": activated}


# --------------------------------------------------------------------------- startup / maintenance
async def _reconcile() -> None:
    """Activate queue heads whose blocking task no longer exists / is no longer active."""
    keys = queue.keys()
    if not keys:
        return
    async with SessionLocal() as db:
        uids = {k[0] for k in keys}
        names = {k[2] for k in keys}
        active = {
            tuple(r) for r in (await db.execute(select(Task.user_id, Task.section, Task.name_key).where(
                Task.status == "active", Task.user_id.in_(uids), Task.name_key.in_(names)))).all()
        }
        missing = [k for k in keys if k not in active]
        if not missing:
            return
        users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_({k[0] for k in missing})))).scalars().all()}
        for uid, section, key in missing:
            if uid in users:
                await activate_next(db, users[uid], section, key)


@hooks.on_startup("personal: rebuild task processing queue + reschedule reminders")
async def _startup() -> None:
    queue.clear()
    async with SessionLocal() as db:
        queued = (await db.execute(select(Task).where(Task.status == "queued").order_by(Task.created_at, Task.id))).scalars().all()
        for t in queued:
            queue.push(t.user_id, t.section, t.name_key, t.id)
    await _reconcile()
    now = utcnow()
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Task, User.timezone).join(User, User.id == Task.user_id).where(
                Task.status == "active", Task.due_at > now, Task.reminded_at.is_(None),
                Task.reminder_cancelled.is_(False), Task.stale.is_(False))
        )).all()
    # The read session is closed BEFORE spawning jobs: never hold a transaction (and its
    # locks) open across job spawns - that could queue behind DDL and stall startup.
    updates = []
    for t, tzname in rows:
        rid = await schedule_reminder(t.user_id, t, tzname or "UTC")
        if rid:
            updates.append({"id": t.id, "reminder_job_id": rid})
    if updates:
        async with SessionLocal() as db:
            await db.execute(update(Task), updates)
            await db.commit()
    log.info("task queue rebuilt (%d queued) - %d reminder(s) rescheduled", len(queue.all_ids()), len(rows))


@hooks.every_maintenance_cycle("personal: prune task processing queue")
async def _maintenance() -> None:
    ids = queue.all_ids()
    if ids:
        async with SessionLocal() as db:
            status = dict((await db.execute(select(Task.id, Task.status).where(Task.id.in_(ids)))).all())
        for (uid, _section, _key), tids in queue.items():
            for tid in tids:
                if status.get(tid) != "queued":
                    queue.remove(uid, tid)
    await _reconcile()


def local_day_bounds(day: str | None, tzname: str) -> tuple[datetime, datetime]:
    """'2026-11-25' (local) -> [start, end) in UTC."""
    tz = tz_for(tzname)
    if day:
        d = datetime.fromisoformat(day[:10])
        start = datetime(d.year, d.month, d.day, tzinfo=tz)
    else:
        n = datetime.now(tz)
        start = datetime(n.year, n.month, n.day, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
