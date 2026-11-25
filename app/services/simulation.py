"""What-if business simulations (best / base / worst), grounded in the user's
own statements, reminders and current business summary. Stored encrypted."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import failsafe
from app.ai.providers import parse_json_loose
from app.core.jobs import JobContext
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.core.utils import clip, local_now, render_business_vars
from app.db import SessionLocal
from app.models import Simulation, Transaction, User
from app.services import business as biz
from app.services import summaries

SIM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "assumptions": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "scenarios": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": {"type": "object", "properties": {
                "name": {"type": "string", "enum": ["best", "base", "worst"]},
                "probability": {"type": "number"},
                "revenue_impact_pct": {"type": "number"},
                "cost_impact_pct": {"type": "number"},
                "cash_impact": {"type": "string"},
                "timeline": {"type": "string"},
                "narrative": {"type": "string"},
            }, "required": ["name", "probability", "revenue_impact_pct", "cost_impact_pct", "cash_impact", "timeline", "narrative"]},
        },
        "kpis": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "current": {"type": "string"}, "projected": {"type": "string"},
            "direction": {"type": "string", "enum": ["up", "down", "flat"]}}, "required": ["name", "projected", "direction"]}},
        "recommendations": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["title", "summary", "assumptions", "scenarios", "recommendations", "confidence"],
}


SKELETON = (
    '{"title": "...", "summary": "...", "assumptions": ["..."], "scenarios": ['
    '{"name": "best", "probability": 25, "revenue_impact_pct": 12, "cost_impact_pct": 2, "cash_impact": "...", "timeline": "...", "narrative": "..."}, '
    '{"name": "base", "probability": 50, "revenue_impact_pct": 5, "cost_impact_pct": 1, "cash_impact": "...", "timeline": "...", "narrative": "..."}, '
    '{"name": "worst", "probability": 25, "revenue_impact_pct": -8, "cost_impact_pct": 3, "cash_impact": "...", "timeline": "...", "narrative": "..."}], '
    '"kpis": [{"name": "...", "current": "...", "projected": "...", "direction": "up"}], "recommendations": ["..."], "confidence": "medium"}'
)


def coerce(data: dict) -> dict:
    """Accept the shapes smaller fallback models produce (dict of scenarios,
    best_case/base_case keys, 'Best case' names) and normalise them."""
    sc = data.get("scenarios")
    if isinstance(sc, dict):
        sc = [{"name": k, **(v if isinstance(v, dict) else {"narrative": str(v)})} for k, v in sc.items()]
    if not sc:
        sc = []
        for k in ("best", "base", "worst", "best_case", "base_case", "worst_case", "optimistic", "realistic", "pessimistic"):
            v = data.get(k)
            if isinstance(v, dict):
                sc.append({"name": k, **v})
    names = {"optimistic": "best", "realistic": "base", "expected": "base", "pessimistic": "worst"}
    out = []
    for s in sc if isinstance(sc, list) else []:
        if not isinstance(s, dict):
            continue
        n = str(s.get("name") or s.get("scenario") or "").lower().replace("_case", "").replace(" case", "").strip()
        n = names.get(n, n)
        if n not in ("best", "base", "worst"):
            n = ("best", "base", "worst")[len(out)] if len(out) < 3 else n
        out.append({**s, "name": n, "cash_impact": str(s.get("cash_impact", "")), "timeline": str(s.get("timeline", "")),
                    "narrative": str(s.get("narrative") or s.get("description") or "")})
    data["scenarios"] = out[:3]
    for key in ("assumptions", "recommendations"):
        if not isinstance(data.get(key), list):
            data[key] = [str(data[key])] if data.get(key) else []
    if not isinstance(data.get("kpis"), list):
        data["kpis"] = []
    data.setdefault("confidence", "medium")
    data.setdefault("title", "")
    data.setdefault("summary", "")
    return data


async def grounding(db: AsyncSession, user: User) -> str:
    parts = []
    cur = await summaries.current(db, user.id, "business")
    if cur is not None:
        data = dict(cur.data or {})
        parts.append(f"Current business summary: {clip(cur.content, 700)} (financial health: {data.get('financial_health', 'unknown')}, "
                     f"cash: {clip(str(data.get('cash_position', '')), 160)})")
    txns = (await db.execute(select(Transaction).where(Transaction.user_id == user.id))).scalars().all()
    tot = biz.totals(list(txns))
    if tot:
        parts.append("Recorded statements: " + "; ".join(f"{v['currency'] or 'n/a'} credits {v['credits_fmt']}, debits "
                                                          f"{v['debits_fmt']}, net {v['net_fmt']}" for v in tot.values()))
    ov = await biz.overview(db, user)
    if ov["pending_reminders"]:
        parts.append(f"Pending client payments: {ov['pending_reminders']} ({', '.join(ov['pending_totals'].values()) or 'amounts n/a'})")
    return "\n".join(parts) or "No financial records yet - state your assumptions explicitly."


def _clean(data: dict) -> dict:
    sc = []
    for s in data.get("scenarios") or []:
        if not isinstance(s, dict):
            continue
        try:
            prob = max(0.0, min(100.0, float(s.get("probability", 0))))
            if prob <= 1.0:
                prob *= 100
        except (TypeError, ValueError):
            prob = 0.0
        def num(k):  # noqa: E306
            try:
                return max(-500.0, min(500.0, float(s.get(k, 0))))
            except (TypeError, ValueError):
                return 0.0
        sc.append({**s, "probability": round(prob, 1), "revenue_impact_pct": num("revenue_impact_pct"),
                   "cost_impact_pct": num("cost_impact_pct")})
    order = {"best": 0, "base": 1, "worst": 2}
    sc.sort(key=lambda x: order.get(x.get("name"), 3))
    data["scenarios"] = sc
    return data


async def simulate(db: AsyncSession, user: User, scenario: str, *, chat_no: int | None = None,
                   msg_no: int | None = None) -> Simulation:
    scenario = clip((scenario or "").strip(), 1500)
    if not scenario:
        raise ValueError("describe the what-if scenario")
    ctx_text = await grounding(db, user)
    system = (
        "You are a pragmatic business strategist running a what-if simulation for {business_name} "
        f"(what it does: {clip(user.business_description, 600) or 'not described'}). Today is "
        f"{local_now(user.timezone).strftime('%d %B %Y')}. Produce exactly three scenarios (best, base, worst) whose "
        "probabilities add up to ~100, realistic % impacts on revenue and costs, the cash impact in words, a timeline, "
        "KPIs and concrete recommendations. Use the placeholder {business_name} for the business name. "
        "Reply with ONE JSON object exactly in this shape (replace the values): " + SKELETON
    )
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": f"WHAT IF: {scenario}\n\nCONTEXT FROM THEIR RECORDS:\n{ctx_text}"}]
    data: dict = {}
    for attempt in range(2):
        raw, _ = await failsafe.complete(user.settings, "business", msgs, uid=user.id, json_schema=SIM_SCHEMA,
                                         max_tokens=1050, temperature=0.35 if attempt == 0 else 0.15, background=True,
                                         allow_online=False)
        data = coerce(parse_json_loose(raw) or {})
        if len(data["scenarios"]) >= 2:
            break
        msgs = msgs + [{"role": "assistant", "content": raw[:1500]},
                       {"role": "user", "content": "That did not follow the format. Return ONLY the JSON object with a "
                                                   "'scenarios' array of exactly 3 items named best, base and worst."}]
    if len(data.get("scenarios") or []) < 2:
        raise failsafe.AIUnavailable("simulation returned no scenarios")
    data = _clean(data)
    row = Simulation(user_id=user.id, scenario=scenario, result=data, source_chat_no=chat_no)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await ulog(user.id, f"simulation #{row.id} saved: {clip(scenario, 80)}" + (f" (chat#{chat_no}.{msg_no})" if chat_no else ""),
               src="business")
    await emit_user(user.id, "notify", {"message": f"Simulation ready: {clip(render_business_vars(data.get('title', scenario), user.business_name), 80)}",
                                        "kind": "ok", "ms": 5000})
    return row


def sim_view(s: Simulation, business_name: str) -> dict:
    data = dict(s.result or {})
    r = lambda t: render_business_vars(str(t or ""), business_name)  # noqa: E731
    return {
        "id": s.id, "scenario": s.scenario, "created_at": s.created_at.isoformat() if s.created_at else None,
        "source": f"chat#{s.source_chat_no}" if s.source_chat_no else None,
        "title": r(data.get("title") or s.scenario), "summary": r(data.get("summary")),
        "assumptions": [r(a) for a in data.get("assumptions") or []],
        "scenarios": [{**x, "narrative": r(x.get("narrative")), "cash_impact": r(x.get("cash_impact"))} for x in data.get("scenarios") or []],
        "kpis": data.get("kpis") or [], "recommendations": [r(x) for x in data.get("recommendations") or []],
        "confidence": data.get("confidence", "medium"),
    }


async def run_job(ctx: JobContext, scenario: str) -> dict:
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == ctx.user_id))).scalar_one()
        await ctx.progress(0.15, "gathering your records")
        row = await simulate(db, user, scenario)
        await ctx.progress(1.0, "simulation ready")
        return {"simulation_id": row.id}


async def remove(db: AsyncSession, user_id: int, sim_id: int) -> bool:
    res = await db.execute(delete(Simulation).where(Simulation.id == sim_id, Simulation.user_id == user_id))
    await db.commit()
    return bool(res.rowcount)
