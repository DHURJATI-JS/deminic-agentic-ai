"""Business desk services.

* Clients and payment reminders are plain relational rows (never vectors).
  Removing a client deletes its reminders (FK ON DELETE CASCADE) and
  force-cancels their scheduled reminder jobs.
* Payment reminders are scheduled background jobs (kind="reminder") that can
  be cancelled until 3 minutes before they are due.
* Credit/debit statements: amounts are encrypted decimal strings, so totals
  are computed in Python.
* Outbox ("sent inbox"): cold sales / general e-mails sent over the user's SMTP.
"""
from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs import JobContext, deadline_before, jobs
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.core.utils import clip, fmt_local, local_now, next_no, parse_iso, parse_when, render_business_vars, tz_for
from app.db import SessionLocal
from app.models import BackgroundJob, Client, Email, Reminder, Transaction, User
from app.services.mailer import MailError, send_mail, user_smtp_configured

CANCEL_WINDOW = timedelta(minutes=3)

# --------------------------------------------------------------------------- money parsing (exact syntax)
_NUM = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<suf>thousand|million|billion|lakhs?|lacs?|crores?|mn|bn|cr|k|m|b)?(?![a-z])",
    re.I,
)
_MULT = {"k": 10**3, "thousand": 10**3, "m": 10**6, "mn": 10**6, "million": 10**6, "b": 10**9, "bn": 10**9,
         "billion": 10**9, "lakh": 10**5, "lakhs": 10**5, "lac": 10**5, "lacs": 10**5, "crore": 10**7,
         "crores": 10**7, "cr": 10**7}
_CURRENCY_HINTS = [
    ("₹", "INR"), ("rs.", "INR"), ("rs ", "INR"), ("inr", "INR"), ("rupee", "INR"), ("lakh", "INR"), ("lac", "INR"),
    ("crore", "INR"), ("us$", "USD"), ("usd", "USD"), ("dollar", "USD"), ("$", "USD"), ("€", "EUR"), ("eur", "EUR"),
    ("euro", "EUR"), ("£", "GBP"), ("gbp", "GBP"), ("pound", "GBP"), ("¥", "JPY"), ("jpy", "JPY"), ("yen", "JPY"),
    ("aed", "AED"), ("dirham", "AED"), ("sgd", "SGD"), ("aud", "AUD"), ("cad", "CAD"), ("chf", "CHF"),
]
_TZ_CURRENCY = {"Asia/Kolkata": "INR", "Asia/Calcutta": "INR", "Europe/London": "GBP", "Asia/Tokyo": "JPY",
                "Asia/Dubai": "AED", "Asia/Singapore": "SGD", "America/Toronto": "CAD", "America/Vancouver": "CAD"}


def parse_amount(value) -> Decimal | None:  # noqa: ANN001
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            return None
        return abs(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    m = _NUM.search(str(value).replace(" ", "").replace("\xa0", ""))
    if not m:
        return None
    try:
        n = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None
    suf = (m.group("suf") or "").lower()
    if suf:
        n *= _MULT.get(suf, 1)
    return abs(n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def default_currency(user: User) -> str:
    tz = user.timezone or ""
    if tz in _TZ_CURRENCY:
        return _TZ_CURRENCY[tz]
    if tz.startswith("America/"):
        return "USD"
    if tz.startswith("Europe/"):
        return "EUR"
    if tz.startswith("Australia/"):
        return "AUD"
    return ""


def detect_currency(*texts: str, default: str = "") -> str:
    for t in texts:
        if not t:
            continue
        s = str(t).strip()
        if len(s) == 3 and s.isalpha():
            return s.upper()
        low = f" {s.lower()} "
        for hint, code in _CURRENCY_HINTS:
            if hint in low:
                return code
    return default


def money_str(d: Decimal | None) -> str:
    return "" if d is None else f"{d:.2f}"


def to_dec(s: str | None) -> Decimal:
    try:
        return Decimal(str(s or "0"))
    except InvalidOperation:
        return Decimal("0")


def fmt_money(amount: str | Decimal | None, currency: str = "") -> str:
    if amount in (None, ""):
        return ""
    d = to_dec(amount) if not isinstance(amount, Decimal) else amount
    txt = f"{d:,.2f}" if d % 1 else f"{d:,.0f}"
    return f"{currency} {txt}".strip()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_due(value: str | None, tzname: str | None, fallback_text: str = "") -> datetime | None:
    """ISO from the LLM first, natural language fallback (both in the user's time zone)."""
    dt = parse_iso(value, tzname) if value else None
    if dt is None and value:
        dt = parse_when(str(value), tzname)
    if dt is None and fallback_text:
        dt = parse_when(fallback_text, tzname)
    return dt


def parse_day(value: str | None, tzname: str | None) -> date:
    today = local_now(tzname).date()
    if not value:
        return today
    v = str(value).strip().lower()
    if v in ("today", "now"):
        return today
    if v == "yesterday":
        return today - timedelta(days=1)
    dt = parse_iso(value, tzname)
    if dt:
        return dt.astimezone(tz_for(tzname)).date()
    dt = parse_when(value, tzname)
    return dt.astimezone(tz_for(tzname)).date() if dt else today


# --------------------------------------------------------------------------- clients
_SUFFIXES = {"inc", "ltd", "llc", "pvt", "private", "limited", "corp", "corporation", "co", "company", "plc", "gmbh", "the"}


def _norm(s: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in (s or "")).split())


def _core(s: str) -> str:
    return " ".join(w for w in _norm(s).split() if w not in _SUFFIXES)


def parse_no(ref: str, prefix: str) -> int | None:
    s = (ref or "").strip().lower().replace(" ", "")
    if s.startswith(prefix):
        s = s[len(prefix):]
    s = s.lstrip("#")
    return int(s) if s.isdigit() else None


def client_view(c: Client, *, pending: int = 0, pending_totals: dict | None = None, reminders: int = 0) -> dict:
    return {
        "client_no": c.client_no, "ref": f"client#{c.client_no}", "name": c.name, "company": c.company,
        "email": c.email, "phone": c.phone, "notes": c.notes, "reason_added": c.reason_added,
        "source": f"chat#{c.source_chat_no}.{c.source_msg_no}" if c.source_chat_no else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "pending_reminders": pending, "reminders": reminders,
        "pending_totals": {k: fmt_money(v, k) for k, v in (pending_totals or {}).items()},
    }


async def find_client(db: AsyncSession, user_id: int, query: str) -> Client | None:
    q = (query or "").strip()
    if not q:
        return None
    no = parse_no(q, "client")
    if no is not None:
        return (await db.execute(select(Client).where(Client.user_id == user_id, Client.client_no == no))).scalar_one_or_none()
    rows = (await db.execute(select(Client).where(Client.user_id == user_id))).scalars().all()
    if not rows:
        return None
    nq, cq = _norm(q), _core(q)
    for c in rows:  # exact
        if nq in (_norm(c.name), _norm(c.company)) or (cq and cq in (_core(c.name), _core(c.company))):
            return c
    for c in rows:  # containment either way
        for field in (_core(c.name), _core(c.company)):
            if field and cq and (cq in field or field in cq):
                return c
    best, score = None, 0.0
    for c in rows:  # fuzzy
        for field in (_core(c.name), _core(c.company)):
            if field and cq:
                r = difflib.SequenceMatcher(None, cq, field).ratio()
                if r > score:
                    best, score = c, r
    return best if score >= 0.74 else None


async def add_client(db: AsyncSession, user: User, *, name: str, company: str = "", email: str = "", phone: str = "",
                     notes: str = "", reason: str = "", chat_no: int | None = None,
                     msg_no: int | None = None) -> tuple[Client, bool]:
    name = clip((name or "").strip(), 160)
    if not name:
        raise ValueError("client name is required")
    existing = await find_client(db, user.id, name)
    if existing is not None and _core(existing.name) == _core(name):
        changed = False
        for attr, val in (("company", company), ("email", email), ("phone", phone)):
            if val and not getattr(existing, attr):
                setattr(existing, attr, clip(val.strip(), 254 if attr == "email" else 160))
                changed = True
        if notes:
            existing.notes = ((existing.notes + "\n") if existing.notes else "") + clip(notes, 2000)
            changed = True
        if changed:
            await db.commit()
        return existing, False
    no = await next_no(db, user.id, "client")
    c = Client(user_id=user.id, client_no=no, name=name, company=clip(company.strip(), 160), email=clip(email.strip(), 254),
               phone=clip(phone.strip(), 40), notes=clip(notes.strip(), 4000), reason_added=clip(reason.strip(), 2000),
               source_chat_no=chat_no, source_msg_no=msg_no)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    src = f" from chat#{chat_no}.{msg_no}" if chat_no else ""
    await ulog(user.id, f"client#{no} added: {name}{src} - reason: {clip(reason, 120) or '-'}", src="business")
    await emit_user(user.id, "client:changed", {"what": "client", "client_no": no, "action": "added"})
    return c, True


def _force_cancel(user_id: int, job_id: str | None, reason: str) -> bool:
    """Cancel a reminder job even inside its 3-minute window (used when the
    client - and therefore the reminder itself - is deleted)."""
    if not job_id:
        return False
    h = jobs.get(user_id, job_id)
    if h and h.status in ("running", "scheduled") and h.task and not h.task.done():
        h.cancel_reason = reason
        h.task.cancel()
        return True
    return False


async def await_job_end(user_id: int, job_id: str | None, timeout: float = 3.0) -> None:
    """jobs.cancel() only *requests* cancellation; wait until the task has
    really finished so its dedupe key is released before re-scheduling."""
    import asyncio

    if not job_id:
        return
    h = jobs.get(user_id, job_id)
    if h and h.task and not h.task.done():
        await asyncio.wait([h.task], timeout=timeout)


async def remove_client(db: AsyncSession, user_id: int, client: Client) -> dict:
    rem = (await db.execute(select(Reminder.reminder_no, Reminder.job_id).where(
        Reminder.user_id == user_id, Reminder.client_id == client.id))).all()
    cancelled = sum(1 for _, jid in rem if _force_cancel(user_id, jid, "client removed"))
    no, name = client.client_no, client.name
    await db.execute(delete(Client).where(Client.id == client.id, Client.user_id == user_id))  # FK cascade -> reminders
    await db.commit()
    await ulog(user_id, f"client#{no} {name} removed - {len(rem)} reminder(s) deleted with it, {cancelled} scheduled "
                        f"reminder job(s) cancelled", src="business")
    await emit_user(user_id, "client:changed", {"what": "client", "client_no": no, "action": "removed"})
    return {"client_no": no, "name": name, "reminders_removed": len(rem), "jobs_cancelled": cancelled,
            "reminder_nos": [r for r, _ in rem]}


async def list_clients(db: AsyncSession, user_id: int) -> list[dict]:
    clients = (await db.execute(select(Client).where(Client.user_id == user_id).order_by(Client.client_no.desc()))).scalars().all()
    # ONE query for all reminders of this user (amounts are encrypted -> aggregate in Python)
    rows = (await db.execute(select(Reminder.client_id, Reminder.status, Reminder.amount, Reminder.currency).where(
        Reminder.user_id == user_id, Reminder.client_id.is_not(None)))).all()
    agg: dict[int, dict] = {}
    for cid, status, amount, cur in rows:
        a = agg.setdefault(cid, {"total": 0, "pending": 0, "totals": {}})
        a["total"] += 1
        if status == "pending":
            a["pending"] += 1
            if amount:
                a["totals"][cur or ""] = a["totals"].get(cur or "", Decimal("0")) + to_dec(amount)
    out = []
    for c in clients:
        a = agg.get(c.id, {"total": 0, "pending": 0, "totals": {}})
        out.append(client_view(c, pending=a["pending"], pending_totals=a["totals"], reminders=a["total"]))
    return out


# --------------------------------------------------------------------------- reminders
def reminder_view(r: Reminder, client_name: str | None, client_no: int | None, tzname: str | None,
                  job: dict | None = None) -> dict:
    now = utcnow()
    due = r.due_at
    can_cancel = bool(r.status == "pending" and (due is None or now < due - CANCEL_WINDOW))
    return {
        "reminder_no": r.reminder_no, "ref": f"reminder#{r.reminder_no}", "title": r.title, "amount": r.amount or "",
        "amount_fmt": fmt_money(r.amount, r.currency), "currency": r.currency, "status": r.status, "stale": r.stale,
        "due_at": due.isoformat() if due else None, "due_local": fmt_local(due, tzname) if due else "",
        "overdue": bool(due and due < now and r.status == "pending"),
        "client_no": client_no, "client_name": client_name or "", "client_ref": f"client#{client_no}" if client_no else None,
        "job_id": r.job_id, "job": job, "can_cancel": can_cancel,
        "cancel_reason": "" if can_cancel or r.status != "pending" else "less than 3 minutes before it is due",
        "source": f"chat#{r.source_chat_no}.{r.source_msg_no}" if r.source_chat_no else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _job_state(user_id: int, job_id: str | None) -> dict | None:
    if not job_id:
        return None
    h = jobs.get(user_id, job_id)
    if h is None:
        return None
    return {"status": h.status, "cancel_reason": h.cancel_reason, "start_at": h.start_at.isoformat() if h.start_at else None}


async def _sync_user_cancels(db: AsyncSession, user_id: int, rows: list[Reminder]) -> None:
    """A reminder job cancelled by the user from Background tasks cancels the reminder."""
    changed = []
    for r in rows:
        if r.status == "pending" and r.job_id:
            h = jobs.get(user_id, r.job_id)
            if h and h.status == "cancelled" and (h.cancel_reason or "").startswith("cancelled by user"):
                r.status = "cancelled"
                changed.append(r.reminder_no)
    if changed:
        await db.commit()
        await ulog(user_id, f"reminder(s) {', '.join(f'#{n}' for n in changed)} cancelled from Background tasks", src="business")


async def list_reminders(db: AsyncSession, user: User, status: str = "all", client_id: int | None = None) -> list[dict]:
    stmt = (select(Reminder, Client.name, Client.client_no).outerjoin(Client, Client.id == Reminder.client_id)
            .where(Reminder.user_id == user.id, Reminder.section == "business"))
    if status in ("pending", "done", "cancelled"):
        stmt = stmt.where(Reminder.status == status)
    if client_id is not None:
        stmt = stmt.where(Reminder.client_id == client_id)
    rows = (await db.execute(stmt.order_by(Reminder.due_at.asc().nulls_last(), Reminder.reminder_no.desc()))).all()
    await _sync_user_cancels(db, user.id, [r for r, _, _ in rows])
    return [reminder_view(r, cname, cno, user.timezone, _job_state(user.id, r.job_id)) for r, cname, cno in rows]


async def schedule_reminder(user_id: int, reminder_id: int) -> str | None:
    async with SessionLocal() as db:
        row = (await db.execute(
            select(Reminder, Client.name, User.timezone).outerjoin(Client, Client.id == Reminder.client_id)
            .join(User, User.id == Reminder.user_id).where(Reminder.id == reminder_id, Reminder.user_id == user_id))).first()
        if row is None:
            return None
        r, cname, tzname = row
        if r.status != "pending" or r.stale or not r.due_at or r.due_at <= utcnow():
            return None
        no, title, due = r.reminder_no, r.title, r.due_at
        amount = fmt_money(r.amount, r.currency)

    async def _fire(ctx: JobContext):
        async with SessionLocal() as db2:
            cur = (await db2.execute(select(Reminder, Client.name).outerjoin(Client, Client.id == Reminder.client_id)
                                     .where(Reminder.id == reminder_id, Reminder.user_id == user_id))).first()
        if cur is None:
            await ctx.log("reminder no longer exists (client removed) - nothing to notify")
            return {"skipped": "deleted"}
        rr, cn = cur
        if rr.status != "pending":
            await ctx.log(f"reminder#{no} is {rr.status} - notification skipped")
            return {"skipped": rr.status}
        body = " · ".join(x for x in (cn, fmt_money(rr.amount, rr.currency)) if x)
        await emit_user(user_id, "reminder", {"title": f"\U0001F4B0 {rr.title}", "when": fmt_local(rr.due_at, tzname),
                                              "body": body, "ref": f"reminder#{no}", "section": "business"})
        await ulog(user_id, f"reminder#{no} due now: {rr.title} {('(' + body + ')') if body else ''}", level="warn", src="business")
        await emit_user(user_id, "client:changed", {"what": "reminder", "reminder_no": no, "action": "fired"})
        return {"fired": True}

    h = await jobs.spawn(
        user_id, "reminder", f"payment reminder#{no}: {clip(title, 70)}" + (f" ({cname})" if cname else ""), _fire,
        cancellable=True, start_at=due, cancel_deadline=deadline_before(due), dedupe_key=f"bizrem:{user_id}:{reminder_id}",
        retries=0, meta={"reminder_no": no, "section": "business", "due_at": due.isoformat(), "amount": amount},
    )
    async with SessionLocal() as db:
        await db.execute(update(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id).values(job_id=h.id))
        await db.commit()
    return h.id


async def add_reminder(db: AsyncSession, user: User, *, client: Client | None, title: str, amount: Decimal | None,
                       currency: str, due_at: datetime | None, chat_no: int | None = None,
                       msg_no: int | None = None) -> Reminder:
    no = await next_no(db, user.id, "reminder")
    r = Reminder(user_id=user.id, reminder_no=no, section="business", client_id=client.id if client else None,
                 title=clip(title.strip() or "Payment due", 200), amount=money_str(amount), currency=(currency or "")[:8],
                 due_at=due_at, status="pending", source_chat_no=chat_no, source_msg_no=msg_no)
    db.add(r)
    await db.commit()
    await db.refresh(r)
    job_id = await schedule_reminder(user.id, r.id)
    r.job_id = job_id
    await ulog(user.id, f"reminder#{no} created: {r.title} {fmt_money(r.amount, r.currency)} due "
                        f"{fmt_local(due_at, user.timezone) or 'no date'}" + (f" for client#{client.client_no}" if client else "")
               + (" - scheduled (cancellable until 3 min before)" if job_id else ""), src="business")
    await emit_user(user.id, "client:changed", {"what": "reminder", "reminder_no": no, "action": "added"})
    return r


async def get_reminder(db: AsyncSession, user_id: int, no: int) -> Reminder | None:
    return (await db.execute(select(Reminder).where(Reminder.user_id == user_id, Reminder.reminder_no == no,
                                                    Reminder.section == "business"))).scalar_one_or_none()


def inside_window(r: Reminder) -> bool:
    return bool(r.due_at and utcnow() >= r.due_at - CANCEL_WINDOW)


async def cancel_reminder(db: AsyncSession, user_id: int, r: Reminder) -> tuple[bool, str]:
    if r.status != "pending":
        return False, f"reminder is already {r.status}"
    if inside_window(r):
        return False, "too late - reminders can only be cancelled up to 3 minutes before they are due"
    if r.job_id:
        await jobs.cancel(user_id, r.job_id, reason="cancelled by user (reminders tab)")
    r.status = "cancelled"
    await db.commit()
    await ulog(user_id, f"reminder#{r.reminder_no} cancelled", src="business")
    await emit_user(user_id, "client:changed", {"what": "reminder", "reminder_no": r.reminder_no, "action": "cancelled"})
    return True, "cancelled"


async def complete_reminder(db: AsyncSession, user_id: int, r: Reminder) -> None:
    r.status = "done"
    await db.commit()
    if r.job_id and not inside_window(r):
        await jobs.cancel(user_id, r.job_id, reason="reminder marked done")
    await ulog(user_id, f"reminder#{r.reminder_no} marked done (payment received)", src="business")
    await emit_user(user_id, "client:changed", {"what": "reminder", "reminder_no": r.reminder_no, "action": "done"})


async def reschedule_all() -> int:
    """Startup: re-create reminder jobs for pending future reminders (jobs do not
    survive a restart); reminders whose job the user cancelled stay cancelled."""
    now = utcnow()
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Reminder.id, Reminder.user_id, BackgroundJob.status, BackgroundJob.detail)
            .outerjoin(BackgroundJob, BackgroundJob.id == Reminder.job_id)
            .where(Reminder.section == "business", Reminder.status == "pending", Reminder.stale.is_(False),
                   Reminder.due_at.is_not(None), Reminder.due_at > now))).all()
        user_cancelled = [rid for rid, _, st, det in rows if st == "cancelled" and (det or "").startswith("cancelled by user")]
        if user_cancelled:
            await db.execute(update(Reminder).where(Reminder.id.in_(user_cancelled)).values(status="cancelled"))
            await db.commit()
    n = 0
    for rid, uid, st, det in rows:
        if rid in user_cancelled:
            continue
        if await schedule_reminder(uid, rid):
            n += 1
    return n


# --------------------------------------------------------------------------- transactions
def txn_view(t: Transaction) -> dict:
    return {
        "txn_no": t.txn_no, "ref": f"txn#{t.txn_no}", "kind": t.kind, "amount": t.amount, "amount_fmt": fmt_money(t.amount, t.currency),
        "currency": t.currency, "counterparty": t.counterparty, "description": t.description,
        "occurred_on": t.occurred_on.isoformat() if t.occurred_on else None,
        "source": f"chat#{t.source_chat_no}.{t.source_msg_no}" if t.source_chat_no else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def totals(rows: list[Transaction]) -> dict:
    out: dict[str, dict] = {}
    for t in rows:
        cur = t.currency or ""
        a = out.setdefault(cur, {"credits": Decimal("0"), "debits": Decimal("0"), "count": 0})
        a["count"] += 1
        if t.kind == "credit":
            a["credits"] += to_dec(t.amount)
        else:
            a["debits"] += to_dec(t.amount)
    return {cur: {"currency": cur, "credits": money_str(v["credits"]), "debits": money_str(v["debits"]),
                  "net": money_str(v["credits"] - v["debits"]), "count": v["count"],
                  "credits_fmt": fmt_money(v["credits"], cur), "debits_fmt": fmt_money(v["debits"], cur),
                  "net_fmt": fmt_money(v["credits"] - v["debits"], cur)} for cur, v in out.items()}


async def add_transaction(db: AsyncSession, user: User, *, kind: str, amount: Decimal, currency: str, counterparty: str = "",
                          description: str = "", occurred_on: date | None = None, chat_no: int | None = None,
                          msg_no: int | None = None) -> Transaction:
    kind = "credit" if kind == "credit" else "debit"
    no = await next_no(db, user.id, "txn")
    t = Transaction(user_id=user.id, txn_no=no, kind=kind, amount=money_str(amount), currency=(currency or "")[:8],
                    counterparty=clip(counterparty.strip(), 160), description=clip(description.strip(), 2000),
                    occurred_on=occurred_on or local_now(user.timezone).date(), source_chat_no=chat_no, source_msg_no=msg_no)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    await ulog(user.id, f"txn#{no} recorded: {kind} {fmt_money(t.amount, t.currency)} {('- ' + t.counterparty) if t.counterparty else ''}"
                        + (f" (chat#{chat_no}.{msg_no})" if chat_no else ""), src="business")
    await emit_user(user.id, "txn:changed", {"txn_no": no, "action": "added"})
    return t


async def settle_matching_reminder(db: AsyncSession, user: User, counterparty: str, amount: Decimal) -> Reminder | None:
    """A credit from a client with a pending reminder of (about) that amount closes the reminder."""
    client = await find_client(db, user.id, counterparty) if counterparty else None
    if client is None:
        return None
    rows = (await db.execute(select(Reminder).where(Reminder.user_id == user.id, Reminder.client_id == client.id,
                                                    Reminder.status == "pending"))).scalars().all()
    best = None
    for r in rows:
        if r.amount and abs(to_dec(r.amount) - amount) <= max(Decimal("1"), amount * Decimal("0.01")):
            best = r
            break
    if best is None and len(rows) == 1 and not rows[0].amount:
        best = rows[0]
    if best is not None:
        await complete_reminder(db, user.id, best)
    return best


# --------------------------------------------------------------------------- e-mails (business outbox)
def email_view(e: Email, business_name: str, *, full: bool = True) -> dict:
    d = {
        "email_no": e.email_no, "ref": f"email#{e.email_no}", "kind": e.kind, "to_email": e.to_email, "to_name": e.to_name,
        "subject": e.subject, "subject_rendered": render_business_vars(e.subject, business_name),
        "status": e.status, "error": e.error, "sent_at": e.sent_at.isoformat() if e.sent_at else None,
        "source": f"chat#{e.source_chat_no}.{e.source_msg_no}" if e.source_chat_no else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }
    if full:
        d["body"] = e.body
        d["body_rendered"] = render_business_vars(e.body, business_name)
    else:
        d["preview"] = clip(render_business_vars(e.body, business_name).replace("\n", " "), 160)
    return d


async def get_email(db: AsyncSession, user_id: int, no: int) -> Email | None:
    return (await db.execute(select(Email).where(Email.user_id == user_id, Email.email_no == no,
                                                 Email.section == "business"))).scalar_one_or_none()


async def create_email(db: AsyncSession, user: User, *, kind: str, to_email: str, to_name: str, subject: str, body: str,
                       chat_no: int | None = None, msg_no: int | None = None) -> Email:
    no = await next_no(db, user.id, "email")
    e = Email(user_id=user.id, email_no=no, section="business", kind=kind, to_email=clip(to_email.strip(), 254),
              to_name=clip(to_name.strip(), 160), subject=clip(subject.strip(), 255), body=body.strip()[:20000],
              status="draft", source_chat_no=chat_no, source_msg_no=msg_no)
    db.add(e)
    await db.commit()
    await db.refresh(e)
    await ulog(user.id, f"email#{no} saved to the sent inbox as a draft ({kind})" + (f" - from chat#{chat_no}.{msg_no}" if chat_no else ""),
               src="business")
    await emit_user(user.id, "email:changed", {"email_no": no, "action": "draft", "section": "business"})
    return e


async def send_email(db: AsyncSession, user: User, e: Email) -> tuple[bool, str]:
    if e.status == "sent":
        return False, "already sent"
    if not e.to_email or "@" not in e.to_email:
        return False, "add a recipient e-mail address first"
    if not user_smtp_configured(user.settings):
        return False, "SMTP is not configured - add it in Settings → E-mail (SMTP)"
    subject = render_business_vars(e.subject, user.business_name)
    body = render_business_vars(e.body, user.business_name)
    try:
        await send_mail(user.settings, e.to_email, subject, body, to_name=e.to_name,
                        from_name=user.business_name or user.name)
    except MailError as ex:
        e.status, e.error = "failed", str(ex)[:500]
        await db.commit()
        await ulog(user.id, f"email#{e.email_no} failed: {ex}", level="error", src="business")
        await emit_user(user.id, "email:changed", {"email_no": e.email_no, "action": "failed", "section": "business"})
        return False, str(ex)
    e.status, e.error, e.sent_at = "sent", "", utcnow()
    await db.commit()
    await ulog(user.id, f"email#{e.email_no} sent to {e.to_email}", src="business")
    await emit_user(user.id, "email:changed", {"email_no": e.email_no, "action": "sent", "section": "business"})
    return True, f"sent to {e.to_email}"


async def overview(db: AsyncSession, user: User) -> dict:
    """Small dashboard header: one query per table, no N+1."""
    clients = (await db.execute(select(func.count()).select_from(Client).where(Client.user_id == user.id))).scalar_one()
    rem = (await db.execute(select(Reminder.amount, Reminder.currency, Reminder.due_at).where(
        Reminder.user_id == user.id, Reminder.section == "business", Reminder.status == "pending"))).all()
    pending: dict[str, Decimal] = {}
    overdue = 0
    for amount, cur, due in rem:
        pending[cur or ""] = pending.get(cur or "", Decimal("0")) + to_dec(amount)
        if due and due < utcnow():
            overdue += 1
    txns = (await db.execute(select(Transaction).where(Transaction.user_id == user.id))).scalars().all()
    drafts = (await db.execute(select(func.count()).select_from(Email).where(
        Email.user_id == user.id, Email.section == "business", Email.status == "draft"))).scalar_one()
    return {"clients": clients, "pending_reminders": len(rem), "overdue": overdue,
            "pending_totals": {k: fmt_money(v, k) for k, v in pending.items()}, "totals": totals(list(txns)),
            "drafts": drafts, "business_name": user.business_name, "business_version": user.business_version}
