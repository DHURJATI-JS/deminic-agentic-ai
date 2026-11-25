"""Global memory CRUD, semantic auto-extraction and the non-cancellable
section wipe (restore-point protected)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import failsafe
from app.ai.providers import parse_json_loose
from app.core import intents, vectors
from app.core.crypto_types import key_hash
from app.core.jobs import JobContext
from app.core.logs import ulog
from app.core.memory import describe, memory_cache
from app.core.sockets import emit_user
from app.core.utils import clip
from app.db import SessionLocal
from app.models import CalendarEvent, Memory, MemoryVector, Reminder, Task, User

# "instruction" = a standing rule for how the assistant behaves (tone, format, things to stop / always do).
# Instructions are injected into EVERY reply of that mode, not only when semantically relevant.
CATEGORIES = ("health", "finance", "contact", "preference", "work", "legal", "instruction", "general")
SENSITIVE = {"health", "finance"}


def norm_key(text: str) -> str:
    return " ".join((text or "").lower().replace("_", " ").split())[:160]


def entry_of(m: Memory) -> dict:
    return {"id": m.id, "category": m.category, "key": m.key, "value": m.value or "", "data": dict(m.data or {}),
            "sensitive": m.is_sensitive, "created_at": m.created_at.isoformat() if m.created_at else None,
            "source_chat_no": m.source_chat_no, "source_msg_no": m.source_msg_no}


async def upsert_memory(db: AsyncSession, user_id: int, section: str, category: str, key: str, value: str = "",
                        data: dict | None = None, *, source_chat_no: int | None = None,
                        source_msg_no: int | None = None) -> Memory:
    category = category if category in CATEGORIES else "general"
    key = norm_key(key) or "note"
    kh = key_hash(category, key)
    m = (
        await db.execute(
            select(Memory).where(Memory.user_id == user_id, Memory.section == section, Memory.category == category,
                                 Memory.key_hash == kh, Memory.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    sensitive = category in SENSITIVE
    if m is None:
        m = Memory(user_id=user_id, section=section, category=category, key=key, key_hash=kh, value=value or "",
                   data=data or {}, is_sensitive=sensitive, source_chat_no=source_chat_no, source_msg_no=source_msg_no)
        db.add(m)
    else:
        m.value = value or m.value
        if data:
            m.data = {**(m.data or {}), **data}
        m.is_sensitive = sensitive
        m.updated_at = datetime.now(timezone.utc)
    await db.flush()
    entry = entry_of(m)
    # Sensitive (health / finance) values are never embedded - only category + key.
    embed_text = f"{category} {key}" if sensitive else describe(entry)
    await vectors.upsert(db, user_id, section, "memory", m.id, describe(entry), embed_text=embed_text)
    memory_cache.invalidate(user_id, section)
    return m


async def delete_memory(db: AsyncSession, user_id: int, section: str, mem_id: int) -> bool:
    m = (await db.execute(select(Memory).where(Memory.id == mem_id, Memory.user_id == user_id,
                                               Memory.section == section))).scalar_one_or_none()
    if m is None:
        return False
    await vectors.remove(db, user_id, "memory", m.id)
    await db.execute(delete(Memory).where(Memory.id == m.id, Memory.user_id == user_id))
    memory_cache.invalidate(user_id, section)
    return True


async def standing_instructions(db: AsyncSession, user_id: int, section: str, limit: int = 12) -> list[str]:
    """The user's saved instructions for this mode (O(1) from the memory cache), newest first."""
    table = await memory_cache.load(db, user_id, section)
    entries = sorted(table["by_cat"].get("instruction", {}).values(), key=lambda e: e.get("created_at") or "", reverse=True)
    return [clip(e.get("value") or e.get("key") or "", 300) for e in entries[:limit] if (e.get("value") or e.get("key"))]


SAVE_SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}, "key": {"type": "string"},
                   "value": {"type": "string"}},
    "required": ["category", "key", "value"],
}


async def extract_requested(user: User, text: str, context: str = "") -> dict | None:
    """The user asked to save something but no remember action was planned: work out exactly
    WHAT to save (typos fixed, 'this'/'that' resolved from the conversation)."""
    try:
        raw, _ = await failsafe.complete(
            user.settings, "personal",
            [{"role": "system", "content": (
                "The user asked you to save something to their global memory; their message may contain typos. "
                "Work out exactly WHAT they want saved, using the recent conversation when they say 'this' or 'that'. "
                "category: 'instruction' when it is about how the assistant should behave from now on (tone, format, "
                "things to stop or always do); otherwise health, finance, preference, work, legal or general. "
                "key: a 2-5 word label. value: one clear sentence with the typos fixed, written as a rule for an "
                "instruction (e.g. 'Do not reassure the user.'). If nothing concrete can be saved, return empty key "
                "and value. JSON only.")},
             {"role": "user", "content": f"RECENT CONVERSATION:\n{clip(context, 1200) or '(none)'}\n\nMESSAGE:\n{clip(text, 1200)}"}],
            uid=user.id, json_schema=SAVE_SCHEMA, max_tokens=200, temperature=0.0, allow_online=False)
    except failsafe.AIUnavailable:
        return None
    d = parse_json_loose(raw) or {}
    key, value = str(d.get("key") or "").strip(), str(d.get("value") or "").strip()
    if not key or not value:
        return None
    cat = str(d.get("category") or "general").lower()
    return {"category": "general" if cat in ("contact",) or cat not in CATEGORIES else cat, "key": key, "value": value}


async def list_memories(db: AsyncSession, user_id: int, section: str) -> list[dict]:
    rows = (await db.execute(select(Memory).where(Memory.user_id == user_id, Memory.section == section,
                                                  Memory.deleted_at.is_(None)).order_by(Memory.created_at.desc()))).scalars().all()
    return [entry_of(m) for m in rows]


# --------------------------------------------------------------------------- auto extraction
AUTO_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["category", "key", "value"],
            },
        }
    },
    "required": ["memories"],
}


async def auto_extract(db: AsyncSession, user: User, section: str, chat_no: int, msg_no: int, prompt: str,
                       vec: list[float], model: str) -> int:
    """Remember durable personal facts the user states (semantic trigger, no regex)."""
    if not await intents.matches(db, vec, model, "auto_memory"):
        return 0
    s = user.settings
    try:
        raw, _ = await failsafe.complete(
            s, "personal",
            [{"role": "system", "content": "Extract durable facts the USER states about themselves (health, finance, "
                                           "family contacts, preferences, work, legal matters) and standing "
                                           "instructions for how the assistant must behave from now on (category "
                                           "'instruction', value written as a rule). Only explicit facts, no guesses. "
                                           "key = short label (e.g. 'peanut allergy', 'monthly salary', 'no reassurance'). "
                                           "Return {\"memories\": []} if nothing durable. JSON only."},
             {"role": "user", "content": clip(prompt, 2000)}],
            uid=user.id, json_schema=AUTO_SCHEMA, max_tokens=300, temperature=0.0, background=True, allow_online=False)
    except failsafe.AIUnavailable:
        return 0
    items = (parse_json_loose(raw) or {}).get("memories") or []
    n = 0
    for it in items[:4]:
        if not isinstance(it, dict) or not it.get("key") or not it.get("value"):
            continue
        if it.get("category") == "contact":
            continue  # contacts are created explicitly (family profiles)
        await upsert_memory(db, user.id, section, it.get("category", "general"), str(it["key"]), clip(str(it["value"]), 500),
                            source_chat_no=chat_no, source_msg_no=msg_no)
        n += 1
    if n:
        await db.commit()
        await ulog(user.id, f"memory: auto-saved {n} fact(s) from chat#{chat_no}.{msg_no} (encrypted at rest)", src="memory")
        await emit_user(user.id, "memory:changed", {"section": section})
    return n


# --------------------------------------------------------------------------- section wipe
async def wipe_section_job(ctx: JobContext, section: str) -> dict:
    """NON-cancellable. Restore point is recorded before each step; on failure or
    app shutdown the job manager replays it (back to the job's timestamp)."""
    uid, rp = ctx.user_id, ctx.restore
    assert rp is not None
    slow = float(os.environ.get("DEMINIC_TEST_SLOW_WIPE", "0") or 0)
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        # 1. soft-delete memories
        ids = list((await db.execute(select(Memory.id).where(Memory.user_id == uid, Memory.section == section,
                                                             Memory.deleted_at.is_(None)))).scalars().all())
        await rp.record({"type": "undelete_memories", "ids": ids})
        if ids:
            await db.execute(update(Memory).where(Memory.user_id == uid, Memory.id.in_(ids))
                             .values(deleted_at=now, deleted_by_job=ctx.id))
            await db.commit()
        memory_cache.invalidate(uid, section)
        await ctx.progress(0.25, f"{len(ids)} memories removed")
        if slow:
            await asyncio.sleep(slow)
        # 2. mark reminders / tasks / events of the section stale (they stay in Postgres)
        stale_counts = {}
        for model, name in ((Task, "tasks"), (Reminder, "reminders"), (CalendarEvent, "calendar_events")):
            rows = list((await db.execute(select(model.id).where(model.user_id == uid, model.section == section,
                                                                 model.stale.is_(False)))).scalars().all())
            await rp.record({"type": "restore_rows", "model": name,
                             "rows": [{"id": i, "fields": {"stale": False}} for i in rows]})
            if rows:
                await db.execute(update(model).where(model.user_id == uid, model.id.in_(rows)).values(stale=True))
            stale_counts[name] = len(rows)
        await db.commit()
        await ctx.progress(0.5, f"marked stale: {stale_counts}")
        if slow:
            await asyncio.sleep(slow)
        # 3. remove the section's vectors (memories, tasks, reminders, events, chats)
        vrows = (await db.execute(select(MemoryVector).where(MemoryVector.user_id == uid,
                                                             MemoryVector.section == section))).scalars().all()
        snapshot = []
        mem_rows = {m.id: m for m in (await db.execute(select(Memory).where(Memory.user_id == uid, Memory.id.in_(ids)))).scalars().all()} if ids else {}
        for v in vrows:
            embed_text = None
            if v.source_type == "memory" and v.source_id in mem_rows and mem_rows[v.source_id].is_sensitive:
                m = mem_rows[v.source_id]
                embed_text = f"{m.category} {m.key}"
            snapshot.append({"section": v.section, "source_type": v.source_type, "source_id": v.source_id,
                             "content": v.content, "embed_text": embed_text})
        await rp.record({"type": "reinsert_vectors", "rows": snapshot})
        await db.execute(delete(MemoryVector).where(MemoryVector.user_id == uid, MemoryVector.section == section))
        await db.commit()
        await ctx.progress(0.85, f"{len(snapshot)} vectors removed from pgvector")
        if slow:
            await asyncio.sleep(slow)
    await emit_user(uid, "memory:changed", {"section": section})
    await ctx.progress(1.0, "memory wiped - tasks/reminders kept but marked stale")
    return {"memories": len(ids), "stale": stale_counts, "vectors": len(snapshot)}


async def purge_soft_deleted(older_than_minutes: int = 30) -> int:
    """Hard-delete memories whose wipe job committed (restore no longer needed)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    async with SessionLocal() as db:
        res = await db.execute(delete(Memory).where(Memory.deleted_at.is_not(None), Memory.deleted_at < cutoff))
        await db.commit()
        return res.rowcount or 0
