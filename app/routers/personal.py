"""Personal section API: tasks (+ same-name processing queue), calendar (.ics,
Google Calendar links, Meet links), family profiles + e-mail, media, summary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import failsafe
from app.ai.tools import personal as ptools
from app.core.jobs import DuplicateJob, JobContext, jobs
from app.core.logs import ulog
from app.core.memory import memory_cache
from app.core.sockets import emit_user
from app.core.utils import clip, parse_iso, tz_for, utcnow
from app.db import SessionLocal, get_db
from app.models import Chat, ChatHistory, Email, Memory, Summary, User
from app.security.crypto import decrypt
from app.security.sessions import current_user
from app.services import calendar_svc, media, memory_service, summaries
from app.services import tasks as tasks_svc
from app.services.geo import place_for
from app.services.mailer import MailError, send_mail, user_smtp_configured

router = APIRouter(prefix="/api/personal", tags=["personal"])


def _tz(user: User) -> str:
    return user.timezone or "UTC"


# =========================================================================== tasks
class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_at: str | None = Field(default=None, max_length=40)
    description: str = Field(default="", max_length=4000)
    location_name: str | None = Field(default=None, max_length=200)
    location: dict | None = None
    section: str = Field(default="personal", pattern="^(personal|business)$")


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    due_at: str | None = Field(default=None, max_length=40)
    location_name: str | None = Field(default=None, max_length=200)


async def _task_or_404(db: AsyncSession, user: User, no: int):
    t = await tasks_svc.get_task(db, user.id, no)
    if t is None:
        raise HTTPException(404, "Task not found")
    return t


@router.get("/tasks")
async def list_tasks(status: str = "open", section: str = "all", db: AsyncSession = Depends(get_db),
                     user: User = Depends(current_user)):
    sec = section if section in ("personal", "business") else None
    st = status if status in ("open", "active", "queued", "done", "cancelled") else None
    rows = await tasks_svc.list_tasks(db, user.id, section=sec, status=st)
    items = await tasks_svc.dicts(db, user.id, rows, _tz(user))
    counts = {"active": 0, "queued": 0, "done": 0}
    for i in items:
        if i["status"] in counts:
            counts[i["status"]] += 1
    return {"tasks": items, "counts": counts, "queue": await _queue_view(db, user)}


async def _queue_view(db: AsyncSession, user: User) -> list[dict]:
    snap = tasks_svc.queue.snapshot(user.id)
    ids = {i for q in snap for i in q["task_ids"]}
    if not ids:
        return []
    from app.models import Task

    rows = {t.id: t for t in (await db.execute(select(Task).where(Task.user_id == user.id, Task.id.in_(ids)))).scalars().all()}
    blockers = {}
    keys = {(q["section"], q["name_key"]) for q in snap}
    if keys:
        act = (await db.execute(select(Task).where(Task.user_id == user.id, Task.status == "active",
                                                   Task.name_key.in_({k[1] for k in keys})))).scalars().all()
        blockers = {(t.section, t.name_key): t for t in act}
    out = []
    for q in snap:
        b = blockers.get((q["section"], q["name_key"]))
        out.append({
            "section": q["section"], "name_key": q["name_key"],
            "active": tasks_svc.task_dict(b, _tz(user)) if b else None,
            "waiting": [tasks_svc.task_dict(rows[i], _tz(user)) for i in q["task_ids"] if i in rows],
        })
    return out


@router.get("/tasks/queue")
async def task_queue(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return {"queue": await _queue_view(db, user)}


@router.post("/tasks")
async def create_task(body: TaskIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    due = parse_iso(body.due_at, _tz(user)) if body.due_at else None
    lat = lon = None
    loc_name = (body.location_name or "").strip()
    if isinstance(body.location, dict):
        try:
            lat, lon = float(body.location["lat"]), float(body.location["lon"])
        except (KeyError, TypeError, ValueError):
            lat = lon = None
        if lat is not None and not loc_name and user.settings.share_location:
            loc_name = await place_for({"lat": lat, "lon": lon})
    res = await tasks_svc.create_task(db, user, section=body.section, title=body.title, due_at=due,
                                      description=body.description, location_name=loc_name, lat=lat, lon=lon)
    t, b = res["task"], res["blocker"]
    return {"task": tasks_svc.task_dict(t, _tz(user), behind_no=b.task_no if b else None),
            "queued_behind": b.task_no if b else None}


@router.patch("/tasks/{task_no}")
async def patch_task(task_no: int, body: TaskPatch, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    t = await _task_or_404(db, user, task_no)
    kwargs = {}
    if "due_at" in body.model_fields_set:
        kwargs["due_at"] = parse_iso(body.due_at, _tz(user)) if body.due_at else None
    res = await tasks_svc.update_task(db, user, t, title=body.title, description=body.description,
                                      location_name=body.location_name, **kwargs)
    return {"task": tasks_svc.task_dict(res["task"], _tz(user)), "changed": res["changed"],
            "activated": res["activated"].task_no if res["activated"] else None}


@router.post("/tasks/{task_no}/complete")
async def complete_task(task_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    t = await _task_or_404(db, user, task_no)
    res = await tasks_svc.complete_task(db, user, t)
    a = res["activated"]
    return {"task": tasks_svc.task_dict(t, _tz(user)), "activated": tasks_svc.task_dict(a, _tz(user)) if a else None}


@router.post("/tasks/{task_no}/reopen")
async def reopen_task(task_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    t = await _task_or_404(db, user, task_no)
    res = await tasks_svc.reopen_task(db, user, t)
    b = res.get("blocker")
    return {"task": tasks_svc.task_dict(t, _tz(user), behind_no=b.task_no if b else None)}


@router.delete("/tasks/{task_no}")
async def delete_task(task_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    t = await _task_or_404(db, user, task_no)
    res = await tasks_svc.delete_task(db, user, t)
    return {"ok": True, "activated": res["activated"].task_no if res["activated"] else None}


# =========================================================================== calendar
class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    starts_at: str = Field(min_length=8, max_length=40)
    ends_at: str | None = Field(default=None, max_length=40)
    location: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=4000)
    meet_link: str = Field(default="", max_length=255)
    wants_meet: bool = False


class EventPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    starts_at: str | None = Field(default=None, max_length=40)
    ends_at: str | None = Field(default=None, max_length=40)
    location: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    meet_link: str | None = Field(default=None, max_length=255)


class MeetIn(BaseModel):
    meet_link: str = Field(min_length=10, max_length=255)


async def _event_or_404(db: AsyncSession, user: User, no: int):
    ev = await calendar_svc.get_event(db, user.id, no)
    if ev is None:
        raise HTTPException(404, "Event not found")
    return ev


@router.get("/calendar")
async def calendar_range(start: str | None = None, end: str | None = None, db: AsyncSession = Depends(get_db),
                         user: User = Depends(current_user)):
    tz = tz_for(_tz(user))
    try:
        if start:
            s = datetime.fromisoformat(start[:10])
            s = datetime(s.year, s.month, s.day, tzinfo=tz)
        else:
            n = datetime.now(tz)
            s = datetime(n.year, n.month, 1, tzinfo=tz)
        if end:
            e = datetime.fromisoformat(end[:10])
            e = datetime(e.year, e.month, e.day, tzinfo=tz)
        else:
            e = s + timedelta(days=42)
    except ValueError as ex:
        raise HTTPException(400, "Dates must be YYYY-MM-DD") from ex
    if e <= s or (e - s).days > 120:
        raise HTTPException(400, "Range must be 1-120 days")
    events, tasks = await calendar_svc.range_items(db, user.id, s.astimezone(timezone.utc), e.astimezone(timezone.utc))
    return {"start": s.isoformat(), "end": e.isoformat(), "timezone": _tz(user),
            "events": [calendar_svc.event_dict(x, _tz(user)) for x in events],
            "tasks": await tasks_svc.dicts(db, user.id, tasks, _tz(user))}


@router.post("/events")
async def create_event(body: EventIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    s = parse_iso(body.starts_at, _tz(user))
    if s is None:
        raise HTTPException(422, "Invalid start date/time")
    e = parse_iso(body.ends_at, _tz(user)) if body.ends_at else None
    if body.meet_link and not calendar_svc.find_meet_link(body.meet_link):
        raise HTTPException(422, "Meet link must look like https://meet.google.com/abc-defg-hij")
    ev = await calendar_svc.create_event(db, user, title=body.title, starts_at=s, ends_at=e, location=body.location,
                                         description=body.description, meet_link=body.meet_link, wants_meet=body.wants_meet)
    return {"event": calendar_svc.event_dict(ev, _tz(user))}


@router.get("/events/{event_no:int}.ics")
async def event_ics(event_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    ev = await _event_or_404(db, user, event_no)
    return Response(calendar_svc.ics_for(ev), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="event-{event_no}.ics"'})


@router.patch("/events/{event_no}")
async def patch_event(event_no: int, body: EventPatch, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    ev = await _event_or_404(db, user, event_no)
    fields = body.model_dump(exclude_unset=True)
    for k in ("starts_at", "ends_at"):
        if fields.get(k):
            fields[k] = parse_iso(fields[k], _tz(user))
            if fields[k] is None:
                raise HTTPException(422, f"Invalid {k}")
    try:
        ev = await calendar_svc.update_event(db, user, ev, **fields)
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from ex
    return {"event": calendar_svc.event_dict(ev, _tz(user))}


@router.post("/events/{event_no}/meet")
async def event_meet(event_no: int, body: MeetIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    ev = await _event_or_404(db, user, event_no)
    try:
        ev = await calendar_svc.attach_meet(db, user, ev, body.meet_link)
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from ex
    return {"event": calendar_svc.event_dict(ev, _tz(user))}


@router.delete("/events/{event_no}")
async def delete_event(event_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    ev = await _event_or_404(db, user, event_no)
    await calendar_svc.delete_event(db, user, ev)
    return {"ok": True}


# =========================================================================== family / contacts
class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    relation: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=40)
    birthday: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=500)


class ContactPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    relation: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    birthday: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=500)


def _check_email(addr: str) -> None:
    if addr and ("@" not in addr or "." not in addr.split("@")[-1] or " " in addr.strip()):
        raise HTTPException(422, {"field": "email", "message": "That e-mail address doesn't look valid"})


@router.get("/contacts")
async def list_contacts(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return {"contacts": await ptools.list_contacts(db, user.id)}


@router.post("/contacts")
async def add_contact(body: ContactIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    _check_email(body.email)
    m = await ptools.save_contact(db, user.id, name=body.name, relation=body.relation, email=body.email, phone=body.phone,
                                  birthday=body.birthday, notes=body.notes)
    await db.commit()
    await ulog(user.id, f"family profile saved: {body.name}", src="family")
    await emit_user(user.id, "memory:changed", {"section": "personal"})
    return {"contact": ptools.contact_dict(m)}


@router.patch("/contacts/{contact_id}")
async def patch_contact(contact_id: int, body: ContactPatch, db: AsyncSession = Depends(get_db),
                        user: User = Depends(current_user)):
    if body.email:
        _check_email(body.email)
    try:
        m = await ptools.update_contact(db, user.id, contact_id, body.model_dump(exclude_unset=True))
    except ValueError as ex:
        raise HTTPException(409, str(ex)) from ex
    if m is None:
        raise HTTPException(404, "Profile not found")
    await emit_user(user.id, "memory:changed", {"section": "personal"})
    return {"contact": ptools.contact_dict(m)}


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    m = (await db.execute(select(Memory.id).where(Memory.id == contact_id, Memory.user_id == user.id,
                                                  Memory.category == "contact"))).first()
    if not m:
        raise HTTPException(404, "Profile not found")
    await memory_service.delete_memory(db, user.id, "personal", contact_id)
    await db.commit()
    await ulog(user.id, f"family profile #{contact_id} deleted", src="family")
    await emit_user(user.id, "memory:changed", {"section": "personal"})
    return {"ok": True}


# =========================================================================== e-mails
class EmailIn(BaseModel):
    contact_id: int | None = None
    to_email: str = Field(default="", max_length=254)
    to_name: str = Field(default="", max_length=160)
    subject: str = Field(default="", max_length=255)
    body: str = Field(default="", max_length=20000)


class EmailDraftIn(BaseModel):
    contact_id: int
    purpose: str = Field(min_length=2, max_length=1000)


class EmailPatch(BaseModel):
    to_email: str | None = Field(default=None, max_length=254)
    to_name: str | None = Field(default=None, max_length=160)
    subject: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=20000)


async def _email_or_404(db: AsyncSession, user: User, no: int) -> Email:
    e = (await db.execute(select(Email).where(Email.user_id == user.id, Email.email_no == no))).scalar_one_or_none()
    if e is None:
        raise HTTPException(404, "E-mail not found")
    return e


async def _contact_entry(db: AsyncSession, user: User, contact_id: int) -> dict:
    entry = await memory_cache.by_id(db, user.id, "personal", contact_id)
    if not entry or entry.get("category") != "contact":
        raise HTTPException(404, "Profile not found")
    return entry


@router.get("/emails")
async def list_emails(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = (await db.execute(select(Email).where(Email.user_id == user.id, Email.section == "personal")
                             .order_by(Email.created_at.desc()).limit(200))).scalars().all()
    return {"emails": [ptools.email_dict(e) for e in rows], "smtp": user_smtp_configured(user.settings)}


@router.post("/emails")
async def create_email(body: EmailIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    to_email, to_name = body.to_email.strip(), body.to_name.strip()
    if body.contact_id:
        d = (await _contact_entry(db, user, body.contact_id)).get("data") or {}
        to_email, to_name = to_email or d.get("email", ""), to_name or d.get("name", "")
    if not to_email:
        raise HTTPException(422, {"field": "to_email", "message": "A recipient e-mail is required"})
    _check_email(to_email)
    e = await ptools.create_family_email(db, user, to_email=to_email, to_name=to_name, subject=body.subject or "(no subject)",
                                         body=body.body)
    return {"email": ptools.email_dict(e)}


@router.post("/emails/draft-ai")
async def draft_email(body: EmailDraftIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    entry = await _contact_entry(db, user, body.contact_id)
    d = entry.get("data") or {}
    if not d.get("email"):
        raise HTTPException(422, "This profile has no e-mail address")
    try:
        subject, text = await ptools.draft_family_email(user, entry, body.purpose)
    except failsafe.AIUnavailable as ex:
        raise HTTPException(503, "AI is unavailable right now") from ex
    e = await ptools.create_family_email(db, user, to_email=d["email"], to_name=d.get("name", ""), subject=subject, body=text)
    return {"email": ptools.email_dict(e)}


@router.patch("/emails/{email_no}")
async def patch_email(email_no: int, body: EmailPatch, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    e = await _email_or_404(db, user, email_no)
    if e.status == "sent":
        raise HTTPException(409, "Sent e-mails can't be edited")
    if body.to_email is not None:
        _check_email(body.to_email.strip())
        e.to_email = body.to_email.strip()
    for f in ("to_name", "subject", "body"):
        v = getattr(body, f)
        if v is not None:
            setattr(e, f, v)
    if e.status == "failed":
        e.status, e.error = "draft", ""
    await db.commit()
    await emit_user(user.id, "email:changed", {"email_no": email_no, "action": "edited"})
    return {"email": ptools.email_dict(e)}


@router.post("/emails/{email_no}/send")
async def send_email(email_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    e = await _email_or_404(db, user, email_no)
    if e.status == "sent":
        return {"ok": True, "already": True, "email": ptools.email_dict(e)}
    if not user_smtp_configured(user.settings):
        raise HTTPException(400, "SMTP is not configured - add your SMTP server, SMTP key and preferred mail in Settings → E-mail")
    try:
        await send_mail(user.settings, e.to_email, e.subject, e.body, to_name=e.to_name, from_name=user.name)
        e.status, e.sent_at, e.error = "sent", utcnow(), ""
        ok, msg = True, f"email#{email_no} sent to {e.to_name or e.to_email}"
    except MailError as ex:
        e.status, e.error = "failed", str(ex)[:500]
        ok, msg = False, f"email#{email_no} failed: {e.error}"
    await db.commit()
    await ulog(user.id, msg, level="info" if ok else "warn", src="family")
    await emit_user(user.id, "email:changed", {"email_no": email_no, "action": e.status})
    return {"ok": ok, "message": msg, "email": ptools.email_dict(e)}


@router.delete("/emails/{email_no}")
async def delete_email(email_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    e = await _email_or_404(db, user, email_no)
    await db.execute(delete(Email).where(Email.id == e.id, Email.user_id == user.id))
    await db.commit()
    await emit_user(user.id, "email:changed", {"email_no": email_no, "action": "deleted"})
    return {"ok": True}


# =========================================================================== media
@router.get("/media/movies")
async def media_movies(q: str = Query(min_length=1, max_length=120), year: str = "", user: User = Depends(current_user)):
    key = decrypt(user.settings.tmdb_api_key_enc)
    if key:
        found = await media.tmdb_search(key, q, year or None, limit=10)
        if found:
            return {"source": "tmdb", "movies": found}
    videos = await media.youtube_search(f"{q} {year} movie".strip(), decrypt(user.settings.youtube_api_key_enc), limit=8)
    return {"source": "youtube", "videos": videos}


@router.get("/media/watch")
async def media_watch(title: str = Query(min_length=1, max_length=120), year: str = "", user: User = Depends(current_user)):
    return await media.where_to_watch(user.settings, title, year or None)


@router.get("/media/movie-video")
async def media_movie_video(title: str = Query(min_length=1, max_length=120), year: str = "",
                            user: User = Depends(current_user)):
    video, kind, alts = await media.find_movie_video(title, year, decrypt(user.settings.youtube_api_key_enc))
    return {"video": video, "kind": kind, "alternatives": alts,
            "search_url": media.youtube_search_url(f"{title} {year} full movie".strip())}


@router.get("/media/youtube")
async def media_youtube(q: str = Query(min_length=1, max_length=150), user: User = Depends(current_user)):
    return {"videos": await media.youtube_search(q, decrypt(user.settings.youtube_api_key_enc), limit=8),
            "search_url": media.youtube_search_url(q)}


@router.get("/media/radio")
async def media_radio(q: str = Query(default="chillout", max_length=80), user: User = Depends(current_user)):
    return {"stations": await media.radio_search(q, limit=16)}


class RecommendIn(BaseModel):
    request: str = Field(min_length=2, max_length=600)
    count: int = Field(default=6, ge=1, le=8)


@router.post("/media/recommend")
async def media_recommend(body: RecommendIn, user: User = Depends(current_user)):
    try:
        return {"movies": await media.recommend_movies(user.settings, user.id, body.request, body.count)}
    except failsafe.AIUnavailable as ex:
        raise HTTPException(503, "AI is unavailable right now") from ex


# =========================================================================== summary
@router.get("/summary")
async def get_summary(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = (await db.execute(select(Summary).where(Summary.user_id == user.id, Summary.type == "personal")
                             .order_by(Summary.created_at.desc()).limit(12))).scalars().all()
    cur = next((r for r in rows if r.is_current), rows[0] if rows else None)
    running = [j for j in jobs.running_for(user.id, "summary") if j.meta.get("type") == "personal"]
    return {"current": summaries.as_dict(cur), "history": [summaries.as_dict(r) for r in rows if r is not cur],
            "running": running[0].id if running else None}


@router.post("/summary/regenerate")
async def regenerate_summary(user: User = Depends(current_user)):
    uid = user.id

    async def _job(ctx: JobContext):
        async with SessionLocal() as db:
            u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
            hist = (await db.execute(
                select(ChatHistory.messages).join(Chat, Chat.id == ChatHistory.chat_id)
                .where(Chat.user_id == uid, ChatHistory.user_id == uid, Chat.section == "personal")
                .order_by(Chat.updated_at.desc()).limit(12))).scalars().all()
            prompts: list[str] = []
            for msgs in hist:
                for m in reversed(msgs or []):
                    p = (m.get("prompt") or "").strip()
                    if p:
                        prompts.append(p)
            if not prompts:
                await ctx.progress(1.0, "no personal chats yet - nothing to summarise")
                return {"updated": False}
            await ctx.progress(0.3, f"summarising {min(len(prompts), 30)} recent message(s)")
            info = "Recent things the user told their assistant (newest first):\n" + "\n".join(
                f"- {clip(p, 240)}" for p in prompts[:30])
            row = await summaries.update_summary(db, u, "personal", clip(info, 4500))
            if row is None:
                raise failsafe.AIUnavailable("summary could not be generated")
            await ctx.progress(1.0, "personal summary updated")
            return {"updated": True}

    try:
        h = await jobs.spawn(uid, "summary", "regenerate personal summary & mind map", _job,
                             dedupe_key=f"summary:personal:{uid}", retries=1, timeout_s=360, meta={"type": "personal"})
    except DuplicateJob as ex:
        raise HTTPException(409, "The summary is already being regenerated") from ex
    return {"request_id": h.id}
