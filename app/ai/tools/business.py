"""Business-section agent tools (selected by pgvector similarity to the examples)."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.ai import failsafe
from app.ai.providers import parse_json_loose
from app.ai.tools import ToolCtx, ToolResult, tool
from app.core.jobs import jobs
from app.core.utils import clip, fmt_local, render_business_vars
from app.models import Client
from app.services import business as biz
from app.services import news as news_svc
from app.services import simulation as sim_svc
from app.services import summaries

B = ("business",)


def _s(args: dict, key: str) -> str:
    v = args.get(key)
    return "" if v is None else str(v).strip()


def _email_ok(v: str) -> str:
    v = (v or "").strip()
    return v if ("@" in v and "." in v.split("@")[-1] and " " not in v) else ""


# --------------------------------------------------------------------------- clients
@tool("add_client", "Add a new business client and record WHY they are being added",
      args={"name": "client person or company name", "company": "company name if different (optional)",
            "email": "e-mail (optional)", "phone": "phone (optional)", "reason": "why the client is added / what they need"},
      sections=B,
      examples=["add Acme Corp as a new client", "we just signed Zenith Ventures as a client, their email is ops@zenith.com",
                "new client: Priya Sharma from BlueOrbit, she wants a website redesign",
                "save Rahul Mehta as a client, he is interested in our premium plan",
                "onboard TechNova as a client because they need monthly analytics reports",
                "add a client called Green Leaf Cafe, phone 98765 43210",
                "we onboarded a new customer, Nimbus Retail, because they need sales forecasting"])
async def add_client(ctx: ToolCtx, args: dict) -> ToolResult:
    name = _s(args, "name") or _s(args, "company")
    if not name:
        return ToolResult(ok=False, text="No client name was given - ask for it.")
    reason = _s(args, "reason") or clip(ctx.text, 300)
    c, created = await biz.add_client(ctx.db, ctx.user, name=name, company=_s(args, "company") if _s(args, "company") != name else "",
                                      email=_email_ok(_s(args, "email")), phone=_s(args, "phone"), reason=reason,
                                      chat_no=ctx.chat_no, msg_no=ctx.msg_no)
    await ctx.log(f"client#{c.client_no} {'added' if created else 'already existed'}")
    status = "added" if created else "already existed (details merged)"
    return ToolResult(
        text=f"Client {status}: client#{c.client_no} {c.name}{(' (' + c.company + ')') if c.company else ''}. "
             f"Reason recorded: {clip(c.reason_added, 160)}. Linked to {ctx.ref}.",
        cards=[{"type": "client_added", "client_no": c.client_no, "name": c.name, "company": c.company, "email": c.email,
                "phone": c.phone, "reason": c.reason_added, "existed": not created, "source": ctx.ref}],
        refs=[f"client#{c.client_no}"])


@tool("remove_client", "Remove a client (their payment reminders are removed with them)",
      args={"client": "client name or client#N"},
      sections=B,
      examples=["remove Acme Corp from my clients", "delete client#3", "we lost Zenith Ventures, remove them from the list",
                "drop BlueOrbit as a client", "Priya Sharma is no longer our client, delete her"])
async def remove_client(ctx: ToolCtx, args: dict) -> ToolResult:
    q = _s(args, "client") or _s(args, "name")
    c = await biz.find_client(ctx.db, ctx.uid, q)
    if c is None:
        return ToolResult(ok=False, text=f"No client matching '{q}' was found - nothing removed.")
    res = await biz.remove_client(ctx.db, ctx.uid, c)
    return ToolResult(
        text=f"Removed client#{res['client_no']} {res['name']}; {res['reminders_removed']} payment reminder(s) were removed with it "
             f"and {res['jobs_cancelled']} scheduled reminder job(s) cancelled.",
        cards=[{"type": "client_removed", **res}])


@tool("list_clients", "List the user's clients with pending payments",
      args={}, sections=B,
      examples=["show my clients", "who are my current clients?", "list all clients and what they owe",
                "how many clients do we have", "which clients have pending payments"])
async def list_clients(ctx: ToolCtx, args: dict) -> ToolResult:
    rows = await biz.list_clients(ctx.db, ctx.uid)
    if not rows:
        return ToolResult(text="There are no clients yet.", cards=[{"type": "client_list", "clients": []}])
    lines = [f"client#{r['client_no']} {r['name']}" + (f" ({r['company']})" if r["company"] else "")
             + (f" - {r['pending_reminders']} pending: {', '.join(r['pending_totals'].values())}" if r["pending_reminders"] else "")
             for r in rows[:40]]
    return ToolResult(text=f"{len(rows)} client(s):\n" + "\n".join(lines),
                      cards=[{"type": "client_list", "clients": rows[:25]}])


# --------------------------------------------------------------------------- reminders
@tool("add_payment_reminder", "Create a payment reminder for a client (amount + due date); it is scheduled as a background task",
      args={"client": "client name or client#N", "amount": "number e.g. 2000", "currency": "ISO code e.g. USD/INR or empty",
            "due": "ISO-8601 due date/time", "title": "short reminder text"},
      sections=B,
      examples=["Acme has to pay 2000 USD on 5 December", "remind me that Zenith owes us 50,000 rupees by Friday",
                "set a payment reminder for BlueOrbit: 1200 euros due next Monday",
                "client#2 must pay the 300 dollar invoice tomorrow at 10am",
                "Rahul will pay the remaining 15k on the 20th, remind me",
                "TechNova's invoice of $4,500 is due at the end of the month"])
async def add_payment_reminder(ctx: ToolCtx, args: dict) -> ToolResult:
    cq = _s(args, "client")
    client = await biz.find_client(ctx.db, ctx.uid, cq) if cq else None
    created_client = False
    if client is None and cq and not cq.lower().startswith("client#"):
        client, created_client = await biz.add_client(
            ctx.db, ctx.user, name=cq, reason=f"has a payment due (added automatically from a payment reminder in {ctx.ref})",
            chat_no=ctx.chat_no, msg_no=ctx.msg_no)
    amount = biz.parse_amount(args.get("amount"))
    currency = biz.detect_currency(_s(args, "currency"), _s(args, "amount"), ctx.text, default=biz.default_currency(ctx.user))
    due = biz.parse_due(_s(args, "due"), ctx.tz, fallback_text=ctx.text)
    title = _s(args, "title") or (f"{client.name} payment" if client else "Payment due")
    r = await biz.add_reminder(ctx.db, ctx.user, client=client, title=title, amount=amount, currency=currency, due_at=due,
                               chat_no=ctx.chat_no, msg_no=ctx.msg_no)
    when = fmt_local(due, ctx.tz) if due else "no due date"
    sched = ("scheduled as a background task (cancellable until 3 minutes before it is due)" if r.job_id
             else "not scheduled (no future due date)")
    extra = f" New client#{client.client_no} {client.name} was created for it." if created_client else ""
    return ToolResult(
        text=f"Payment reminder#{r.reminder_no} saved: {r.title} - {biz.fmt_money(r.amount, r.currency) or 'amount not given'}, due {when}; "
             f"{sched}.{extra} Linked to {ctx.ref}" + (f" and client#{client.client_no}" if client else "") + ".",
        cards=[{"type": "reminder", "reminder_no": r.reminder_no, "title": r.title, "amount_fmt": biz.fmt_money(r.amount, r.currency),
                "due_local": when, "due_at": due.isoformat() if due else None, "client_no": client.client_no if client else None,
                "client_name": client.name if client else "", "scheduled": bool(r.job_id), "job_id": r.job_id, "source": ctx.ref}],
        refs=[f"reminder#{r.reminder_no}"] + ([f"client#{client.client_no}"] if client else []))


@tool("list_reminders", "List pending / upcoming / overdue client payment reminders",
      args={}, sections=B,
      examples=["what payments are due?", "show pending payment reminders", "who owes us money this week?",
                "list overdue invoices", "which payment reminders are coming up"])
async def list_reminders(ctx: ToolCtx, args: dict) -> ToolResult:
    rows = await biz.list_reminders(ctx.db, ctx.user, status="pending")
    if not rows:
        return ToolResult(text="No pending payment reminders.", cards=[{"type": "reminder_list", "reminders": []}])
    lines = [f"reminder#{r['reminder_no']} {r['title']} {r['amount_fmt']} due {r['due_local'] or 'n/a'}"
             + (" (OVERDUE)" if r["overdue"] else "") + (f" - client#{r['client_no']} {r['client_name']}" if r["client_no"] else "")
             for r in rows[:40]]
    return ToolResult(text=f"{len(rows)} pending reminder(s):\n" + "\n".join(lines),
                      cards=[{"type": "reminder_list", "reminders": rows[:20]}])


# --------------------------------------------------------------------------- statements
_CREDIT_WORDS = ("received", "got ", "earned", "income", "credited", "paid us", "funding", "invested", "deposit", "sold", "revenue")
_DEBIT_WORDS = ("spent", "paid ", "bought", "debited", "expense", "rent", "salary", "salaries", "refund", "purchase", "fee", "bill")


@tool("record_transaction", "Record money in (credit) or money out (debit) in the credit/debit statements",
      args={"kind": "credit (money in) or debit (money out)", "amount": "number", "currency": "ISO code or empty",
            "counterparty": "who paid us / whom we paid", "description": "what it was for", "date": "ISO date or empty for today"},
      sections=B,
      examples=["we received 50,000 from Zenith Ventures today", "paid 1,200 rent for the office",
                "I got 2 lakh funding from Sequoia", "spent 300 dollars on Facebook ads",
                "Acme paid their invoice of 2000 USD", "we paid salaries of 3.5 lakh this month",
                "sent a refund of 150 euros to a customer"])
async def record_transaction(ctx: ToolCtx, args: dict) -> ToolResult:
    amount = biz.parse_amount(args.get("amount"))
    if amount is None:
        return ToolResult(ok=False, text="No amount was given for the transaction - ask for it.")
    kind = _s(args, "kind").lower()
    if kind not in ("credit", "debit"):
        low = f" {ctx.text.lower()} "
        kind = "credit" if any(w in low for w in _CREDIT_WORDS) and not any(w in low for w in ("spent", "bought", "rent")) else "debit"
    currency = biz.detect_currency(_s(args, "currency"), _s(args, "amount"), ctx.text, default=biz.default_currency(ctx.user))
    t = await biz.add_transaction(ctx.db, ctx.user, kind=kind, amount=amount, currency=currency,
                                  counterparty=_s(args, "counterparty"), description=_s(args, "description") or clip(ctx.text, 200),
                                  occurred_on=biz.parse_day(_s(args, "date"), ctx.tz), chat_no=ctx.chat_no, msg_no=ctx.msg_no)
    settled = None
    if kind == "credit" and t.counterparty:
        settled = await biz.settle_matching_reminder(ctx.db, ctx.user, t.counterparty, amount)
    text = (f"Recorded txn#{t.txn_no}: {kind} {biz.fmt_money(t.amount, t.currency)}"
            + (f" {'from' if kind == 'credit' else 'to'} {t.counterparty}" if t.counterparty else "")
            + f" on {t.occurred_on.isoformat()} (linked to {ctx.ref}).")
    if settled is not None:
        text += f" It also closed payment reminder#{settled.reminder_no} ({settled.title})."
    return ToolResult(text=text, cards=[{"type": "transaction", **biz.txn_view(t),
                                         "settled_reminder": settled.reminder_no if settled else None}],
                      refs=[f"txn#{t.txn_no}"] + ([f"reminder#{settled.reminder_no}"] if settled else []))


@tool("business_status", "Summarise how the business is doing: finances, statements, pending payments and the AI business summary",
      args={}, sections=B,
      examples=["how is my business doing?", "give me a summary of our finances", "are we profitable this month?",
                "what's our cash position", "summarise the state of the business"])
async def business_status(ctx: ToolCtx, args: dict) -> ToolResult:
    ov = await biz.overview(ctx.db, ctx.user)
    cur = summaries.as_dict(await summaries.current(ctx.db, ctx.uid, "business"))
    parts = [f"Clients: {ov['clients']}. Pending payment reminders: {ov['pending_reminders']} "
             f"({', '.join(ov['pending_totals'].values()) or 'no amounts'}), overdue: {ov['overdue']}. Draft e-mails: {ov['drafts']}."]
    for v in ov["totals"].values():
        parts.append(f"Statements {v['currency'] or ''}: credits {v['credits_fmt']}, debits {v['debits_fmt']}, net {v['net_fmt']} ({v['count']} entries).")
    if cur:
        parts.append(f"AI business summary: {cur['content']} (financial health: {cur['data'].get('financial_health', 'unknown')}).")
    return ToolResult(text=" ".join(parts), cards=[{"type": "business_status", "overview": ov,
                                                   "health": (cur or {}).get("data", {}).get("financial_health")}])


# --------------------------------------------------------------------------- e-mails
COLD_SCHEMA = {"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"},
                                                "to_name": {"type": "string"}}, "required": ["subject", "body"]}


@tool("draft_cold_email", "Write a cold sales e-mail for a prospect or segment and save it in the sent inbox as a draft",
      args={"prospect": "who it is for (company / segment / person)", "to_email": "recipient e-mail if given",
            "to_name": "recipient name if given", "offer": "what we are offering / the angle", "tone": "e.g. friendly, formal, bold"},
      sections=B,
      examples=["draft a cold sales email to fintech startups", "write a cold email pitching our analytics service to Acme's CTO at cto@acme.com",
                "prepare an outreach email for restaurant owners", "create a sales email for hospitals about our software",
                "write a cold mail to Priya at BlueOrbit offering a free audit", "compose a short cold pitch email for e-commerce brands"])
async def draft_cold_email(ctx: ToolCtx, args: dict) -> ToolResult:
    prospect = _s(args, "prospect") or _s(args, "to_name") or "a promising prospect"
    to_email, to_name = _email_ok(_s(args, "to_email")), _s(args, "to_name")
    client = await biz.find_client(ctx.db, ctx.uid, prospect) if prospect else None
    if client is not None:
        to_email = to_email or client.email
        to_name = to_name or client.name
    system = (
        "You write honest, concise, high-converting cold sales e-mails (90-160 words) for {business_name}. "
        f"What {{business_name}} does: {clip(ctx.user.business_description, 600) or 'not described - keep the offer generic'}. "
        "Personalise to the prospect, lead with their pain point, one specific benefit, one clear call to action, no "
        "fake statistics or claims. Write the business name ONLY as the placeholder {business_name}. "
        f"Sign off as {ctx.user.name}, {{business_name}}. JSON only."
    )
    usr = (f"Prospect: {prospect}\nRecipient name: {to_name or 'unknown'}\nOffer/angle: {_s(args, 'offer') or 'our core offer'}\n"
           f"Tone: {_s(args, 'tone') or 'friendly and professional'}\nUser request: {clip(ctx.text, 600)}")
    try:
        raw, _ = await failsafe.complete(ctx.settings, "business", [{"role": "system", "content": system},
                                                                   {"role": "user", "content": usr}],
                                         uid=ctx.uid, json_schema=COLD_SCHEMA, max_tokens=700, temperature=0.6,
                                         allow_online=False)
    except failsafe.AIUnavailable as e:
        return ToolResult(ok=False, text=f"Could not draft the e-mail: {e}")
    data = parse_json_loose(raw) or {}
    subject, body = _s(data, "subject"), _s(data, "body")
    if not body:
        return ToolResult(ok=False, text="The model returned an empty e-mail - try again with more detail.")
    e = await biz.create_email(ctx.db, ctx.user, kind="cold_sales", to_email=to_email, to_name=to_name or _s(data, "to_name"),
                               subject=subject or f"Quick idea for {prospect}", body=body, chat_no=ctx.chat_no, msg_no=ctx.msg_no)
    view = biz.email_view(e, ctx.user.business_name)
    return ToolResult(
        text=f"Cold sales e-mail drafted and saved in the sent inbox as email#{e.email_no} (linked to {ctx.ref}); "
             f"recipient: {e.to_email or 'not set yet'}. Subject: {view['subject_rendered']}",
        system_addendum="Tell the user the draft is saved in the Sent inbox as email#N, give the subject and a one-line gist "
                        "(do NOT repeat the whole e-mail - it is shown in the card), and say they can edit it or press Send.",
        cards=[{"type": "cold_email", **view, "source": ctx.ref}], refs=[f"email#{e.email_no}"])


@tool("send_business_email", "Send an existing draft e-mail from the sent inbox over SMTP",
      args={"email": "email#N reference or the recipient / subject to identify the draft"},
      sections=B,
      examples=["send email#4 now", "send the cold email to Acme", "yes, send that draft", "go ahead and send the outreach email"])
async def send_business_email(ctx: ToolCtx, args: dict) -> ToolResult:
    from app.models import Email

    ref = _s(args, "email")
    no = biz.parse_no(ref, "email")
    e = await biz.get_email(ctx.db, ctx.uid, no) if no is not None else None
    if e is None:
        rows = (await ctx.db.execute(select(Email).where(Email.user_id == ctx.uid, Email.section == "business",
                                                         Email.status.in_(("draft", "failed")))
                                     .order_by(Email.created_at.desc()).limit(10))).scalars().all()
        low = ref.lower()
        e = next((x for x in rows if low and (low in (x.to_email or "").lower() or low in (x.to_name or "").lower()
                                               or low in (x.subject or "").lower())), rows[0] if rows else None)
    if e is None:
        return ToolResult(ok=False, text="There is no draft e-mail to send.")
    ok, msg = await biz.send_email(ctx.db, ctx.user, e)
    return ToolResult(ok=ok, text=f"email#{e.email_no}: {msg}",
                      cards=[{"type": "cold_email", **biz.email_view(e, ctx.user.business_name), "source": ctx.ref}],
                      refs=[f"email#{e.email_no}"])


# --------------------------------------------------------------------------- news / simulation
@tool("market_news", "Today's world & market news digest with opportunities and risks for the business (generated once per day)",
      args={}, sections=B,
      examples=["what's happening in the markets today?", "show me today's business news", "any opportunities in the news for us?",
                "what's going on in the world economy", "give me the daily market briefing",
                "how is the world looking for our business this week"])
async def market_news(ctx: ToolCtx, args: dict) -> ToolResult:
    res = await news_svc.ensure_today(ctx.user)
    if res["status"] == "generating":
        await ctx.log(f"news digest generating (request {res['request_id'][:8]}) - waiting up to 90s")
        h = jobs.get(ctx.uid, res["request_id"])
        if h and h.task:
            await asyncio.wait([h.task], timeout=90)
        res = await news_svc.ensure_today(ctx.user)
    if res["status"] != "ready":
        return ToolResult(text="Today's news digest is still being prepared in the background; it will appear in the News tab "
                               f"(background task {res.get('request_id', '')[:8]}).",
                          cards=[{"type": "news", "status": "generating", "request_id": res.get("request_id")}])
    d = res["digest"]
    opps = "; ".join(f"{o['title']}: {o.get('action', '')}" for o in d["opportunities"][:4])
    risks = "; ".join(r["title"] for r in d["risks"][:3])
    return ToolResult(
        text=f"Digest for {d['date']} (sentiment {d['sentiment']}): {d['market_summary']}\nOpportunities: {opps}\nRisks: {risks}",
        cards=[{"type": "news", "status": "ready", "date": d["date"], "sentiment": d["sentiment"], "market_summary": d["market_summary"],
                "opportunities": d["opportunities"][:3], "world": d["world"][:4]}])


@tool("simulate_scenario", "Run a what-if business simulation (best / base / worst case with impacts, KPIs and recommendations)",
      args={"scenario": "the what-if to simulate"}, sections=B,
      examples=["what if we raise prices by 15%?", "simulate losing our biggest client", "what happens if we hire 3 more engineers?",
                "what if sales drop 30% next quarter", "run a what-if: we open a second store in Pune",
                "what would happen if our main supplier doubles prices"])
async def simulate_scenario(ctx: ToolCtx, args: dict) -> ToolResult:
    scenario = _s(args, "scenario") or ctx.text
    try:
        row = await sim_svc.simulate(ctx.db, ctx.user, scenario, chat_no=ctx.chat_no, msg_no=ctx.msg_no)
    except failsafe.AIUnavailable as e:
        return ToolResult(ok=False, text=f"The simulation could not be produced: {e}")
    v = sim_svc.sim_view(row, ctx.user.business_name)
    sc = "; ".join(f"{s['name']} ({s['probability']:.0f}%): revenue {s['revenue_impact_pct']:+.0f}%, costs {s['cost_impact_pct']:+.0f}%, "
                   f"{s['cash_impact']}" for s in v["scenarios"])
    return ToolResult(
        text=f"Simulation '{v['title']}': {v['summary']} Scenarios: {sc}. Recommendations: {'; '.join(v['recommendations'][:4])}",
        system_addendum="Present the simulation briefly: one line per scenario and the top recommendations. Details are in the card.",
        cards=[{"type": "simulation", **v}])


@tool("client_details", "Show one client's details: contact info, why they were added and their payment reminders",
      args={"client": "client name or client#N"}, sections=B,
      examples=["tell me about Acme Corp", "show client#2", "what do we know about Zenith Ventures", "why did we add BlueOrbit as a client"])
async def client_details(ctx: ToolCtx, args: dict) -> ToolResult:
    c = await biz.find_client(ctx.db, ctx.uid, _s(args, "client"))
    if c is None:
        return ToolResult(ok=False, text="No matching client found.")
    rems = await biz.list_reminders(ctx.db, ctx.user, client_id=c.id)
    view = biz.client_view(c, reminders=len(rems), pending=sum(1 for r in rems if r["status"] == "pending"))
    lines = [f"client#{c.client_no} {c.name}" + (f" ({c.company})" if c.company else ""),
             f"email {c.email or '-'}, phone {c.phone or '-'}", f"added because: {c.reason_added or '-'} ({view['source'] or 'manually'})"]
    lines += [f"reminder#{r['reminder_no']} {r['title']} {r['amount_fmt']} due {r['due_local'] or '-'} [{r['status']}]" for r in rems[:10]]
    return ToolResult(text="\n".join(lines), cards=[{"type": "client_added", "client_no": c.client_no, "name": c.name,
                                                     "company": c.company, "email": c.email, "phone": c.phone,
                                                     "reason": c.reason_added, "existed": True, "source": view["source"]}],
                      refs=[f"client#{c.client_no}"])


def _unused() -> None:  # keep linters quiet about imports used only in type positions
    _ = (Client, render_business_vars)
