"""AI summaries stored in the ``summaries`` table.

type="business": generated from prompts such as "I'm running low", "bankrupt",
"got funds from X" - financial health, cash position, events, risks.
type="personal": mind map, challenges, goals, mood.
Triggering is semantic (pgvector prototypes), not regex. Content is encrypted.
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import failsafe
from app.ai.providers import parse_json_loose
from app.core import intents
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.core.utils import clip
from app.models import Summary, User

BUSINESS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "financial_health": {"type": "string", "enum": ["healthy", "stable", "at_risk", "critical", "unknown"]},
        "cash_position": {"type": "string"},
        "key_events": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "opportunities": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "next_actions": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
    "required": ["summary", "financial_health", "key_events", "risks", "next_actions"],
}

PERSONAL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "mood": {"type": "string"},
        "mind_map": {
            "type": "object",
            "properties": {
                "center": {"type": "string"},
                "branches": {
                    "type": "array",
                    "maxItems": 7,
                    "items": {
                        "type": "object",
                        "properties": {"label": {"type": "string"},
                                       "children": {"type": "array", "items": {"type": "string"}, "maxItems": 5}},
                        "required": ["label", "children"],
                    },
                },
            },
            "required": ["center", "branches"],
        },
        "challenges": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "goals": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "wins": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "suggestions": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
    "required": ["summary", "mind_map", "challenges", "goals"],
}


async def current(db: AsyncSession, user_id: int, type_: str) -> Summary | None:
    return (
        await db.execute(select(Summary).where(Summary.user_id == user_id, Summary.type == type_,
                                               Summary.is_current.is_(True)).order_by(Summary.created_at.desc()).limit(1))
    ).scalar_one_or_none()


def as_dict(s: Summary | None) -> dict | None:
    if s is None:
        return None
    return {"id": s.id, "type": s.type, "content": s.content, "data": dict(s.data or {}),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "ref": f"chat#{s.source_chat_no}.{s.source_msg_no}" if s.source_chat_no else None,
            "business_version": s.business_version}


async def update_summary(db: AsyncSession, user: User, type_: str, new_info: str, *, chat_no: int | None = None,
                         msg_no: int | None = None) -> Summary | None:
    prev = await current(db, user.id, type_)
    prev_text = ""
    if prev is not None:
        prev_text = f"PREVIOUS SUMMARY:\n{prev.content}\nPREVIOUS DATA: {clip(str(prev.data), 2500)}"
    if type_ == "business":
        system = ("You maintain a running business status summary for {business_name}. Merge the new information into "
                  "the previous summary. Be factual and numeric where possible; use the placeholder {business_name} "
                  f"for the business name. Business description: {clip(user.business_description, 600)}. JSON only.")
        schema = BUSINESS_SCHEMA
    else:
        system = ("You maintain a private personal-wellbeing summary of the user: a mind map of their life areas, the "
                  "challenges they face, goals, wins and gentle suggestions. Merge new information into the previous "
                  "summary. Never invent facts. JSON only.")
        schema = PERSONAL_SCHEMA
    try:
        raw, _ = await failsafe.complete(
            user.settings, "personal",
            [{"role": "system", "content": system},
             {"role": "user", "content": f"{prev_text}\n\nNEW INFORMATION FROM THE USER:\n{clip(new_info, 3000)}"}],
            uid=user.id, json_schema=schema, max_tokens=900, temperature=0.2, background=True, allow_online=False)
    except failsafe.AIUnavailable:
        return None
    data = parse_json_loose(raw)
    if not data or not data.get("summary"):
        return None
    await db.execute(update(Summary).where(Summary.user_id == user.id, Summary.type == type_).values(is_current=False))
    row = Summary(user_id=user.id, type=type_, content=str(data.get("summary", ""))[:4000], data=data, is_current=True,
                  business_version=user.business_version if type_ == "business" else None,
                  source_chat_no=chat_no, source_msg_no=msg_no)
    db.add(row)
    await db.commit()
    await emit_user(user.id, "summary:updated", {"type": type_})
    await ulog(user.id, f"AI summary ({type_}) updated" + (f" from chat#{chat_no}.{msg_no}" if chat_no else ""), src="insights")
    return row


async def maybe_update(db: AsyncSession, user: User, section: str, chat_no: int, msg_no: int, prompt: str,
                       reply: str, vec: list[float], model: str) -> None:
    if section not in ("business", "personal"):
        return
    hits = {n for n, _ in await intents.matches(db, vec, model, "summary")}
    if section not in hits:
        return
    await update_summary(db, user, section, f"User said: {prompt}\nAssistant replied: {clip(reply, 1200)}",
                         chat_no=chat_no, msg_no=msg_no)
