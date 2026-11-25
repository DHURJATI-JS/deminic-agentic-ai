"""Business section API: clients, payment reminders, credit/debit statements,
sent inbox, daily news, what-if simulations and the AI business summary.
Every query is scoped to the signed-in user; per-user numbers resolve refs."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs import DuplicateJob, JobContext, jobs
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.core.utils import clip, render_business_vars
from app.db import SessionLocal, get_db
from app.models import Chat, ChatHistory, Client, Email, NewsDigest, Reminder, Simulation, Summary, Transaction, User
from app.security.sessions import current_user
from app.services import business as biz
from app.services import news as news_svc
from app.services import simulation as sim_svc
from app.services import summaries

router = APIRouter(prefix="/api/business", tags=["business"])


# =========================================================================== overview
@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return await biz.overview(db, user)


# =========================================================================== clients
class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    company: str = Field(default="", max_length=160)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=4000)
    reason: str = Field(default="", max_length=2000)


class ClientPatch(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    company: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=4000)
    reason_added: str | None = Field(default=None, max_length=2000)


async def _client(db: AsyncSession, user: User, no: int) -> Client:
    c = (await db.execute(select(Client).where(Client.user_id == user.id, Client.client_no == no))).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "Client not found")
    return c


@router.get("/clients")
async def clients(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return {"clients": await biz.list_clients(db, user.id)}


@router.post("/clients")
async def create_client(body: ClientIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    email = body.email.strip()
    if email and "@" not in email:
        raise HTTPException(422, {"field": "email", "message": "Invalid e-mail address"})
    c, created = await biz.add_client(db, user, name=body.name, company=body.company, email=email, phone=body.phone,
                                      notes=body.notes, reason=body.reason or "added manually")
    if not created:
        raise HTTPException(409, {"message": f"A client with that name already exists (client#{c.client_no})",
                                  "client_no": c.client_no})
    return {"client": biz.client_view(c)}


@router.get("/clients/{no}")
async def client_detail(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    c = await _client(db, user, no)
    rems = await biz.list_reminders(db, user, client_id=c.id)
    pending = [r for r in rems if r["status"] == "pending"]
    return {"client": biz.client_view(c, pending=len(pending), reminders=len(rems)), "reminders": rems}


@router.patch("/clients/{no}")
async def update_client(no: int, body: ClientPatch, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    c = await _client(db, user, no)
    data = body.model_dump(exclude_none=True)
    if "email" in data and data["email"].strip() and "@" not in data["email"]:
        raise HTTPException(422, {"field": "email", "message": "Invalid e-mail address"})
    if "name" in data and not data["name"].strip():
        raise HTTPException(422, {"field": "name", "message": "Name is required"})
    for k, v in data.items():
        setattr(c, k, v.strip())
    await db.commit()
    await ulog(user.id, f"client#{no} updated ({', '.join(data) or 'no changes'})", src="business")
    await emit_user(user.id, "client:changed", {"what": "client", "client_no": no, "action": "updated"})
    return {"client": biz.client_view(c)}


@router.delete("/clients/{no}")
async def delete_client(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    c = await _client(db, user, no)
    return await biz.remove_client(db, user.id, c)


# =========================================================================== reminders
class ReminderIn(BaseModel):
    client_no: int | None = None
    client_name: str = Field(default="", max_length=160)
    title: str = Field(default="", max_length=200)
    amount: str = Field(default="", max_length=40)
    currency: str = Field(default="", max_length=8)
    due_at: str | None = Field(default=None, max_length=64)


class ReminderPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    amount: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=8)
    due_at: str | None = Field(default=None, max_length=64)


async def _reminder(db: AsyncSession, user: User, no: int):
    r = await biz.get_reminder(db, user.id, no)
    if r is None:
        raise HTTPException(404, "Reminder not found")
    return r


async def _reminder_out(db: AsyncSession, user: User, no: int) -> dict:
    rows = await biz.list_reminders(db, user)
    return next((x for x in rows if x["reminder_no"] == no), {})


@router.get("/reminders")
async def reminders(status: str = "all", db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return {"reminders": await biz.list_reminders(db, user, status=status)}


@router.post("/reminders")
async def create_reminder(body: ReminderIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    client = None
    if body.client_no:
        client = await _client(db, user, body.client_no)
    elif body.client_name.strip():
        client = await biz.find_client(db, user.id, body.client_name)
        if client is None:
            client, _ = await biz.add_client(db, user, name=body.client_name, reason="has a payment due (added with a reminder)")
    amount = biz.parse_amount(body.amount) if body.amount else None
    if body.amount and amount is None:
        raise HTTPException(422, {"field": "amount", "message": "Amount must be a number"})
    due = biz.parse_due(body.due_at, user.timezone) if body.due_at else None
    if body.due_at and due is None:
        raise HTTPException(422, {"field": "due_at", "message": "Unrecognised due date"})
    title = body.title.strip() or (f"{client.name} payment" if client else "Payment due")
    currency = biz.detect_currency(body.currency, body.amount, default=biz.default_currency(user))
    r = await biz.add_reminder(db, user, client=client, title=title, amount=amount, currency=currency, due_at=due)
    return {"reminder": await _reminder_out(db, user, r.reminder_no)}


@router.patch("/reminders/{no}")
async def update_reminder(no: int, body: ReminderPatch, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    r = await _reminder(db, user, no)
    if r.status != "pending":
        raise HTTPException(409, f"Reminder is {r.status}")
    if biz.inside_window(r):
        raise HTTPException(409, "Too late - reminders can't be changed within 3 minutes of being due")
    if body.title is not None and body.title.strip():
        r.title = body.title.strip()
    if body.amount is not None:
        amt = biz.parse_amount(body.amount) if body.amount.strip() else None
        if body.amount.strip() and amt is None:
            raise HTTPException(422, {"field": "amount", "message": "Amount must be a number"})
        r.amount = biz.money_str(amt)
    if body.currency is not None:
        r.currency = body.currency.strip().upper()[:8]
    reschedule = False
    if body.due_at is not None:
        due = biz.parse_due(body.due_at, user.timezone) if body.due_at.strip() else None
        if body.due_at.strip() and due is None:
            raise HTTPException(422, {"field": "due_at", "message": "Unrecognised due date"})
        r.due_at, reschedule = due, True
    old_job = r.job_id
    await db.commit()
    if reschedule:
        if old_job:
            await jobs.cancel(user.id, old_job, reason="reminder rescheduled")
            await biz.await_job_end(user.id, old_job)
        await biz.schedule_reminder(user.id, r.id)
    await ulog(user.id, f"reminder#{no} updated" + (" and rescheduled" if reschedule else ""), src="business")
    await emit_user(user.id, "client:changed", {"what": "reminder", "reminder_no": no, "action": "updated"})
    await db.refresh(r)  # job_id was updated by the scheduler's own session
    return {"reminder": await _reminder_out(db, user, no)}


@router.post("/reminders/{no}/done")
async def reminder_done(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    r = await _reminder(db, user, no)
    if r.status == "done":
        return {"ok": True, "message": "already done"}
    await biz.complete_reminder(db, user.id, r)
    return {"ok": True}


@router.post("/reminders/{no}/cancel")
async def reminder_cancel(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    r = await _reminder(db, user, no)
    ok, msg = await biz.cancel_reminder(db, user.id, r)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@router.delete("/reminders/{no}")
async def reminder_delete(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    r = await _reminder(db, user, no)
    if r.status == "pending" and biz.inside_window(r):
        raise HTTPException(409, "Too late - reminders can only be cancelled up to 3 minutes before they are due")
    if r.job_id:
        await jobs.cancel(user.id, r.job_id, reason="reminder deleted")
    await db.execute(delete(Reminder).where(Reminder.id == r.id, Reminder.user_id == user.id))
    await db.commit()
    await ulog(user.id, f"reminder#{no} deleted", src="business")
    await emit_user(user.id, "client:changed", {"what": "reminder", "reminder_no": no, "action": "deleted"})
    return {"ok": True}


# =========================================================================== statements
class TxnIn(BaseModel):
    kind: str = Field(pattern="^(credit|debit)$")
    amount: str = Field(min_length=1, max_length=40)
    currency: str = Field(default="", max_length=8)
    counterparty: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=2000)
    occurred_on: str | None = Field(default=None, max_length=32)


@router.get("/transactions")
async def transactions(kind: str = "", currency: str = "", q: str = "", db: AsyncSession = Depends(get_db),
                       user: User = Depends(current_user)):
    stmt = select(Transaction).where(Transaction.user_id == user.id)
    if kind in ("credit", "debit"):
        stmt = stmt.where(Transaction.kind == kind)
    if currency:
        stmt = stmt.where(Transaction.currency == currency.upper()[:8])
    rows = list((await db.execute(stmt.order_by(Transaction.occurred_on.desc(), Transaction.txn_no.desc()))).scalars().all())
    if q.strip():  # counterparty / description are encrypted -> filter after decryption
        ql = q.strip().lower()
        rows = [t for t in rows if ql in (t.counterparty or "").lower() or ql in (t.description or "").lower()]
    all_rows = rows if not (kind or currency or q) else list(
        (await db.execute(select(Transaction).where(Transaction.user_id == user.id))).scalars().all())
    return {"transactions": [biz.txn_view(t) for t in rows], "totals": biz.totals(rows), "all_totals": biz.totals(all_rows),
            "currencies": sorted({t.currency for t in all_rows if t.currency})}


@router.post("/transactions")
async def create_transaction(body: TxnIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    amount = biz.parse_amount(body.amount)
    if amount is None:
        raise HTTPException(422, {"field": "amount", "message": "Amount must be a number"})
    currency = biz.detect_currency(body.currency, body.amount, default=biz.default_currency(user))
    t = await biz.add_transaction(db, user, kind=body.kind, amount=amount, currency=currency, counterparty=body.counterparty,
                                  description=body.description, occurred_on=biz.parse_day(body.occurred_on, user.timezone))
    return {"transaction": biz.txn_view(t)}


@router.delete("/transactions/{no}")
async def delete_transaction(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    res = await db.execute(delete(Transaction).where(Transaction.user_id == user.id, Transaction.txn_no == no))
    await db.commit()
    if not res.rowcount:
        raise HTTPException(404, "Transaction not found")
    await ulog(user.id, f"txn#{no} deleted", src="business")
    await emit_user(user.id, "txn:changed", {"txn_no": no, "action": "deleted"})
    return {"ok": True}


# =========================================================================== sent inbox
class EmailIn(BaseModel):
    to_email: str = Field(default="", max_length=254)
    to_name: str = Field(default="", max_length=160)
    subject: str = Field(default="", max_length=255)
    body: str = Field(default="", max_length=20000)


class EmailPatch(BaseModel):
    to_email: str | None = Field(default=None, max_length=254)
    to_name: str | None = Field(default=None, max_length=160)
    subject: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=20000)


async def _email(db: AsyncSession, user: User, no: int) -> Email:
    e = await biz.get_email(db, user.id, no)
    if e is None:
        raise HTTPException(404, "E-mail not found")
    return e


@router.get("/emails")
async def emails(status: str = "", db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(Email).where(Email.user_id == user.id, Email.section == "business")
    if status in ("draft", "sent", "failed"):
        stmt = stmt.where(Email.status == status)
    rows = (await db.execute(stmt.order_by(Email.created_at.desc()).limit(300))).scalars().all()
    return {"emails": [biz.email_view(e, user.business_name, full=False) for e in rows],
            "smtp_configured": bool(user.settings and user.settings.smtp_host and user.settings.smtp_from_email)}


@router.post("/emails")
async def create_email(body: EmailIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    if body.to_email.strip() and "@" not in body.to_email:
        raise HTTPException(422, {"field": "to_email", "message": "Invalid e-mail address"})
    e = await biz.create_email(db, user, kind="general", to_email=body.to_email, to_name=body.to_name,
                               subject=body.subject or "(no subject)", body=body.body)
    return {"email": biz.email_view(e, user.business_name)}


@router.get("/emails/{no}")
async def email_detail(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return {"email": biz.email_view(await _email(db, user, no), user.business_name)}


@router.patch("/emails/{no}")
async def update_email(no: int, body: EmailPatch, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    e = await _email(db, user, no)
    if e.status == "sent":
        raise HTTPException(409, "Sent e-mails can't be edited")
    data = body.model_dump(exclude_none=True)
    if data.get("to_email", "").strip() and "@" not in data["to_email"]:
        raise HTTPException(422, {"field": "to_email", "message": "Invalid e-mail address"})
    for k, v in data.items():
        setattr(e, k, v.strip() if k != "body" else v)
    if e.status == "failed":
        e.status, e.error = "draft", ""
    await db.commit()
    await emit_user(user.id, "email:changed", {"email_no": no, "action": "updated", "section": "business"})
    return {"email": biz.email_view(e, user.business_name)}


@router.post("/emails/{no}/send")
async def send_email(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    e = await _email(db, user, no)
    ok, msg = await biz.send_email(db, user, e)
    if not ok and e.status != "failed":
        raise HTTPException(400, msg)
    return {"ok": ok, "message": msg, "email": biz.email_view(e, user.business_name)}


@router.delete("/emails/{no}")
async def delete_email(no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    res = await db.execute(delete(Email).where(Email.user_id == user.id, Email.email_no == no, Email.section == "business"))
    await db.commit()
    if not res.rowcount:
        raise HTTPException(404, "E-mail not found")
    await emit_user(user.id, "email:changed", {"email_no": no, "action": "deleted", "section": "business"})
    return {"ok": True}


# =========================================================================== news
@router.get("/news")
async def news_today(day: str = "", db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    if day:
        try:
            d = date.fromisoformat(day)
        except ValueError as e:
            raise HTTPException(422, "day must be YYYY-MM-DD") from e
        row = await news_svc.get_digest(db, user.id, d)
        if row is None:
            raise HTTPException(404, "No digest for that day")
        return {"status": "ready", "digest": news_svc.digest_view(row, user.business_name)}
    return await news_svc.ensure_today(user)


@router.post("/news/refresh")
async def news_refresh(user: User = Depends(current_user)):
    """Never creates a second digest for the same day - returns today's one (or the running job)."""
    return await news_svc.ensure_today(user)


@router.get("/news/history")
async def news_history(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return {"days": await news_svc.history(db, user.id)}


# =========================================================================== simulations
class SimIn(BaseModel):
    scenario: str = Field(min_length=3, max_length=1500)


@router.get("/simulations")
async def simulations(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = (await db.execute(select(Simulation).where(Simulation.user_id == user.id)
                             .order_by(Simulation.created_at.desc()).limit(50))).scalars().all()
    running = [j for j in jobs.list_for(user.id) if j["kind"] == "simulation" and j["status"] == "running"]
    return {"simulations": [sim_svc.sim_view(s, user.business_name) for s in rows],
            "running": [{"request_id": j["request_id"], "scenario": j["meta"].get("scenario", "")} for j in running]}


@router.post("/simulations")
async def start_simulation(body: SimIn, user: User = Depends(current_user)):
    scenario = body.scenario.strip()

    async def _job(ctx: JobContext):
        return await sim_svc.run_job(ctx, scenario)

    try:
        h = await jobs.spawn(user.id, "simulation", f"what-if: {clip(scenario, 80)}", _job, cancellable=True, retries=1,
                             timeout_s=300, dedupe_key=f"sim:{user.id}:{scenario.lower()[:120]}",
                             meta={"scenario": clip(scenario, 200)})
    except DuplicateJob as e:
        raise HTTPException(409, "That simulation is already running") from e
    return {"request_id": h.id}


@router.get("/simulations/{sim_id}")
async def simulation_detail(sim_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    s = (await db.execute(select(Simulation).where(Simulation.id == sim_id, Simulation.user_id == user.id))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "Simulation not found")
    return {"simulation": sim_svc.sim_view(s, user.business_name)}


@router.delete("/simulations/{sim_id}")
async def simulation_delete(sim_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    if not await sim_svc.remove(db, user.id, sim_id):
        raise HTTPException(404, "Simulation not found")
    return {"ok": True}


# =========================================================================== AI business summary
@router.get("/summary")
async def business_summary(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = (await db.execute(select(Summary).where(Summary.user_id == user.id, Summary.type == "business")
                             .order_by(Summary.created_at.desc()).limit(12))).scalars().all()
    cur = next((r for r in rows if r.is_current), None)

    def view(s):  # noqa: ANN001, ANN202
        d = summaries.as_dict(s)
        if d:
            d["content"] = render_business_vars(d["content"], user.business_name)
        return d

    running = [j["request_id"] for j in jobs.list_for(user.id) if j["kind"] == "summary" and j["status"] == "running"
               and j["meta"].get("type") == "business"]
    return {"current": view(cur), "history": [view(r) for r in rows if not r.is_current],
            "business_version": user.business_version, "business_name": user.business_name,
            "regenerating": running[0] if running else None}


@router.post("/summary/regenerate")
async def regenerate_summary(user: User = Depends(current_user)):
    uid = user.id

    async def _job(ctx: JobContext):
        async with SessionLocal() as db:
            u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
            # ONE query: recent business chat histories of the CURRENT business context
            rows = (await db.execute(
                select(Chat.chat_no, ChatHistory.messages).join(ChatHistory, ChatHistory.chat_id == Chat.id)
                .where(Chat.user_id == uid, Chat.section == "business", Chat.business_version == u.business_version)
                .order_by(Chat.updated_at.desc()).limit(10))).all()
            prompts = []
            for chat_no, msgs in rows:
                for m in (msgs or [])[-8:]:
                    if m.get("prompt"):
                        prompts.append((m.get("created_at") or "", f"chat#{chat_no}.{m.get('chatno')}: {clip(m['prompt'], 300)}"))
            prompts.sort(reverse=True)
            await ctx.progress(0.3, f"{len(prompts)} recent business messages")
            ov = await biz.overview(db, u)
            info = "\n".join(p for _, p in prompts[:25]) or "(no business conversations yet)"
            info += "\n\nRECORDS: " + "; ".join(f"{v['currency'] or ''} credits {v['credits_fmt']} debits {v['debits_fmt']} net {v['net_fmt']}"
                                                 for v in ov["totals"].values())
            info += f"; clients {ov['clients']}; pending payments {ov['pending_reminders']} ({', '.join(ov['pending_totals'].values())})"
            row = await summaries.update_summary(db, u, "business", info)
            if row is None:
                raise RuntimeError("the AI could not produce a summary")
            return {"summary_id": row.id}

    try:
        h = await jobs.spawn(uid, "summary", "regenerate business AI summary", _job, cancellable=True, retries=1,
                             timeout_s=300, dedupe_key=f"bizsummary:{uid}", meta={"type": "business"})
    except DuplicateJob as e:
        return {"request_id": e.existing_id, "message": "already regenerating"}
    return {"request_id": h.id}


# =========================================================================== startup / maintenance hooks
from app.core import hooks  # noqa: E402


@hooks.on_startup("business-reminders")
async def _reschedule_reminders() -> None:
    n = await biz.reschedule_all()
    if n:
        import logging

        logging.getLogger("deminic.business").info("re-scheduled %d pending payment reminder(s)", n)


_news_state = {"last": 0.0}


@hooks.every_maintenance_cycle("business-daily-news")
async def _daily_news() -> None:
    """Once per day per active business user: prepare the digest after 06:00 local time
    (at most one new digest job per maintenance cycle to protect the local GPU)."""
    import time
    from datetime import datetime, timedelta, timezone

    from app.core.utils import local_now

    if time.monotonic() - _news_state["last"] < 600:
        return
    _news_state["last"] = time.monotonic()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    active = select(Chat.user_id).where(Chat.section == "business", Chat.updated_at >= since).distinct()
    async with SessionLocal() as db:
        users = (await db.execute(select(User).where(User.owns_business.is_(True), User.id.in_(active)))).scalars().all()
        if not users:
            return
        have = {(uid, d) for uid, d in (await db.execute(select(NewsDigest.user_id, NewsDigest.digest_date).where(
            NewsDigest.user_id.in_([u.id for u in users]), NewsDigest.digest_date >= date.today() - timedelta(days=2)))).all()}
    for u in users:
        now = local_now(u.timezone)
        if now.hour < 6 or (u.id, now.date()) in have:
            continue
        res = await news_svc.ensure_today(u)
        if res["status"] == "generating":
            break  # one per cycle
