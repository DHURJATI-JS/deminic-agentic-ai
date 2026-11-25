"""Chats: list / create / open / rename / delete, send messages (background
generation), main points and reference resolution (chat#N, chat#N.M ...)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent import is_outdated, start_generation
from app.config import SECTIONS, settings
from app.core.jobs import jobs
from app.core.logs import ulog
from app.core.utils import REF_RE, next_no, render_business_vars
from app.db import get_db
from app.models import Chat, ChatHistory, MemoryVector, User
from app.security.ratelimit import limiter
from app.security.sessions import current_user

router = APIRouter(prefix="/api", tags=["chats"])


def _section(section: str) -> str:
    if section not in SECTIONS:
        raise HTTPException(400, "Unknown section")
    return section


async def get_chat(db: AsyncSession, user: User, chat_no: int) -> Chat:
    chat = (await db.execute(select(Chat).where(Chat.user_id == user.id, Chat.chat_no == chat_no))).scalar_one_or_none()
    if chat is None:
        raise HTTPException(404, "Chat not found")
    return chat


def generating_request(user_id: int, chat: Chat) -> str | None:
    owner = jobs.dedupe_owner(f"gen:{user_id}:{chat.id}")
    return owner


def chat_summary(chat: Chat, user: User) -> dict:
    return {
        "chat_no": chat.chat_no,
        "ref": f"chat#{chat.chat_no}",
        "section": chat.section,
        "title": render_business_vars(chat.title or "New chat", user.business_name),
        "message_count": chat.message_count,
        "lawyer_mode": chat.lawyer_mode,
        "outdated": is_outdated(chat, user),
        "generating": generating_request(user.id, chat),
        "created_at": chat.created_at.isoformat() if chat.created_at else None,
        "updated_at": chat.updated_at.isoformat() if chat.updated_at else None,
    }


@router.get("/chats")
async def list_chats(section: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    _section(section)
    rows = (
        await db.execute(select(Chat).where(Chat.user_id == user.id, Chat.section == section)
                         .order_by(Chat.updated_at.desc()).limit(300))
    ).scalars().all()
    return {"chats": [chat_summary(c, user) for c in rows]}


class NewChatIn(BaseModel):
    section: str


@router.post("/chats")
async def create_chat(body: NewChatIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    section = _section(body.section)
    no = await next_no(db, user.id, "chat")
    chat = Chat(user_id=user.id, chat_no=no, section=section, title="New chat", main_points=[],
                business_version=user.business_version if section == "business" else None)
    db.add(chat)
    await db.flush()
    db.add(ChatHistory(chat_id=chat.id, user_id=user.id, messages=[], compacted_history=""))
    await db.commit()
    await ulog(user.id, f"new {section} chat created: chat#{no}", src="chats")
    return {"chat": chat_summary(chat, user)}


@router.get("/chats/{chat_no}")
async def open_chat(chat_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    chat = await get_chat(db, user, chat_no)
    hist = (await db.execute(select(ChatHistory).where(ChatHistory.chat_id == chat.id,
                                                       ChatHistory.user_id == user.id))).scalar_one_or_none()
    messages = []
    for m in (hist.messages if hist else []):
        messages.append({
            "chatno": m.get("chatno"),
            "ref": f"chat#{chat.chat_no}.{m.get('chatno')}",
            "prompt": m.get("prompt", ""),
            "reply": render_business_vars(m.get("reply", ""), user.business_name),
            "status": m.get("status"),
            "request_id": m.get("request_id"),
            "cards": m.get("cards") or [],
            "refs": m.get("refs") or [],
            "attachments": m.get("attachments") or [],
            "provider": m.get("provider"),
            "escalated": m.get("escalated", False),
            "created_at": m.get("created_at"),
        })
    running = jobs.get(user.id, generating_request(user.id, chat) or "")
    return {
        "chat": chat_summary(chat, user),
        "messages": messages,
        "main_points": [{**p, "text": render_business_vars(p.get("text", ""), user.business_name)} for p in chat.main_points],
        "partial": running.meta.get("partial", "") if running else "",
    }


class PatchChatIn(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    lawyer_mode: bool | None = None


@router.patch("/chats/{chat_no}")
async def patch_chat(chat_no: int, body: PatchChatIn, db: AsyncSession = Depends(get_db),
                     user: User = Depends(current_user)):
    chat = await get_chat(db, user, chat_no)
    if body.title is not None:
        chat.title = body.title.strip() or chat.title
    if body.lawyer_mode is not None:
        chat.lawyer_mode = body.lawyer_mode
        await ulog(user.id, f"chat#{chat_no}: lawyer mode {'ON' if body.lawyer_mode else 'OFF'}", src="chats")
    await db.commit()
    return {"chat": chat_summary(chat, user)}


@router.delete("/chats/{chat_no}")
async def delete_chat(chat_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    chat = await get_chat(db, user, chat_no)
    rid = generating_request(user.id, chat)
    if rid:
        await jobs.cancel(user.id, rid, reason="chat deleted")
    await db.execute(delete(MemoryVector).where(MemoryVector.user_id == user.id, MemoryVector.source_type == "chat",
                                                MemoryVector.source_id == chat.id))
    await db.execute(delete(Chat).where(Chat.id == chat.id, Chat.user_id == user.id))
    await db.commit()
    await ulog(user.id, f"chat#{chat_no} deleted", src="chats")
    return {"ok": True}


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    location: dict | None = None
    lawyer_mode: bool | None = None
    provider: str | None = Field(default=None, pattern="^(auto|local|claude|openai|github)$")
    request_id: str | None = Field(default=None, max_length=36)
    attachments: list[str] = Field(default_factory=list, max_length=3)


@router.post("/chats/{chat_no}/messages")
@limiter.limit(settings.rate_limit_chat)
async def send_message(request: Request, chat_no: int, body: SendIn, db: AsyncSession = Depends(get_db),
                       user: User = Depends(current_user)):
    chat = await get_chat(db, user, chat_no)
    loc = None
    if isinstance(body.location, dict):
        try:
            loc = {"lat": float(body.location["lat"]), "lon": float(body.location["lon"]),
                   "accuracy": float(body.location.get("accuracy") or 0)}
        except (KeyError, TypeError, ValueError):
            loc = None
    return await start_generation(db, user, chat, body.text, location=loc, lawyer=body.lawyer_mode,
                                  request_id=body.request_id, provider=body.provider, attachments=body.attachments)


@router.post("/attachments")
@limiter.limit("30/minute")
async def upload_attachment(request: Request, file: UploadFile = File(...), user: User = Depends(current_user)):
    """Attach a file for the AI to analyse (ClamAV scanned; images go to the vision model)."""
    from app.services import attachments

    meta = await attachments.save(user.id, file)
    from app.core.logs import ulog as _ulog

    await _ulog(user.id, f"attachment '{meta['name']}' uploaded ({meta['kind']}, ClamAV clean)", src="chats")
    return {"attachment": meta}


@router.get("/chats/{chat_no}/points")
async def main_points(chat_no: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    chat = await get_chat(db, user, chat_no)
    return {"chat_no": chat_no, "points": [{**p, "text": render_business_vars(p.get("text", ""), user.business_name)}
                                           for p in chat.main_points]}


@router.get("/points")
async def all_points(section: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    _section(section)
    rows = (await db.execute(select(Chat).where(Chat.user_id == user.id, Chat.section == section)
                             .order_by(Chat.updated_at.desc()).limit(100))).scalars().all()
    return {"chats": [{"chat_no": c.chat_no, "title": render_business_vars(c.title, user.business_name),
                       "outdated": is_outdated(c, user),
                       "points": [{**p, "text": render_business_vars(p.get("text", ""), user.business_name)}
                                  for p in c.main_points]} for c in rows if c.main_points]}


REF_TABLES = {
    "task": ("app.models", "Task", "task_no", "personal"),
    "client": ("app.models", "Client", "client_no", "business"),
    "email": ("app.models", "Email", "email_no", None),
    "event": ("app.models", "CalendarEvent", "event_no", None),
    "reminder": ("app.models", "Reminder", "reminder_no", "business"),
    "txn": ("app.models", "Transaction", "txn_no", "business"),
}


@router.get("/refs/resolve")
async def resolve_ref(ref: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    m = REF_RE.fullmatch(ref.strip())
    if not m:
        raise HTTPException(400, "Unknown reference format")
    kind, no, sub = m.group(1).lower(), int(m.group(2)), m.group(3)
    if kind == "chat":
        chat = await get_chat(db, user, no)
        return {"type": "chat", "section": chat.section, "chat_no": chat.chat_no, "msg_no": int(sub) if sub else None}
    import importlib

    mod, cls, col, default_section = REF_TABLES[kind]
    model = getattr(importlib.import_module(mod), cls)
    row = (await db.execute(select(model).where(model.user_id == user.id, getattr(model, col) == no))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Reference not found")
    # Tasks / events live in the Personal tabs (the only place those tabs exist).
    section = "personal" if kind in ("task", "event") else (getattr(row, "section", None) or default_section or "personal")
    return {"type": kind, "section": section, "no": no,
            "source": f"chat#{row.source_chat_no}.{row.source_msg_no}" if getattr(row, "source_chat_no", None) else None}
