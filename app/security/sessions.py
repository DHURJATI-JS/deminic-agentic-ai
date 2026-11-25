"""Redis-backed server-side sessions and the remembered-account (account chooser) tokens."""
from __future__ import annotations

import json
import time

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.state import redis
from app.db import get_db
from app.models import User
from app.security.crypto import random_token, sha256_hex

SESSION_COOKIE = "dm_session"
ANON_COOKIE = "dm_device"  # HttpOnly device id (CSRF binding before login + saved accounts)
SESSION_TTL = 7 * 24 * 3600


def _skey(token_hash: str) -> str:
    return f"sess:{token_hash}"


async def create_session(user_id: int, user_agent: str = "") -> tuple[str, str]:
    token = random_token(32)
    th = sha256_hex(token)
    payload = {"uid": user_id, "created": time.time(), "ua": sha256_hex(user_agent)[:16]}
    await redis.set(_skey(th), json.dumps(payload), ex=SESSION_TTL)
    await redis.sadd(f"usess:{user_id}", th)
    await redis.expire(f"usess:{user_id}", SESSION_TTL)
    return token, th


async def load_session(token: str | None) -> tuple[dict, str] | None:
    if not token or len(token) > 128:
        return None
    th = sha256_hex(token)
    raw = await redis.get(_skey(th))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not data.get("replaced"):
        await redis.expire(_skey(th), SESSION_TTL)  # sliding expiry
    return data, th


async def destroy_session(token_hash: str, user_id: int | None = None) -> None:
    await redis.delete(_skey(token_hash))
    if user_id is not None:
        await redis.srem(f"usess:{user_id}", token_hash)


# ---------------------------------------------------------------------------
# ONE LOGIN PER ACCOUNT. Every login stores the hash of its session token in
# users.session_token_hash (the DB is the source of truth); the middleware
# rejects any other session token for that account on every request.
# ---------------------------------------------------------------------------
async def activate_single_login(user_id: int, token_hash: str) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.db import SessionLocal

    async with SessionLocal() as db:
        await db.execute(update(User).where(User.id == user_id)
                         .values(session_token_hash=token_hash, session_issued_at=datetime.now(timezone.utc)))
        await db.commit()
    # Older sessions become tombstones (so their next request is told *why*), then expire.
    for th in await redis.smembers(f"usess:{user_id}"):
        if th != token_hash:
            await redis.set(_skey(th), json.dumps({"uid": user_id, "replaced": True}), ex=24 * 3600)
            await redis.srem(f"usess:{user_id}", th)
    from app.core.sockets import kick_other_sessions

    await kick_other_sessions(user_id, token_hash)


async def mark_replaced(token_hash: str, user_id: int) -> None:
    """Turn a stale session into a tombstone (kept 24 h) so every tab learns *why* it was signed out."""
    await redis.set(_skey(token_hash), json.dumps({"uid": user_id, "replaced": True}), ex=24 * 3600)
    await redis.srem(f"usess:{user_id}", token_hash)


async def clear_single_login(user_id: int, token_hash: str | None = None) -> None:
    from sqlalchemy import update

    from app.db import SessionLocal

    async with SessionLocal() as db:
        stmt = update(User).where(User.id == user_id)
        if token_hash:
            stmt = stmt.where(User.session_token_hash == token_hash)
        await db.execute(stmt.values(session_token_hash=None))
        await db.commit()


async def login_token_valid(user_id: int, token_hash: str) -> bool:
    """The token checker: is this session's token THE login token stored in the DB?"""
    from app.db import SessionLocal

    async with SessionLocal() as db:
        stored = (await db.execute(select(User.session_token_hash).where(User.id == user_id))).scalar_one_or_none()
    return bool(stored) and stored == token_hash


async def destroy_all_sessions(user_id: int, keep_hash: str | None = None) -> None:
    hashes = await redis.smembers(f"usess:{user_id}")
    for th in hashes:
        if th != keep_hash:
            await redis.delete(_skey(th))
            await redis.srem(f"usess:{user_id}", th)


# ---------------------------------------------------------------------------
# Saved accounts ("Google-like" account chooser) - stored in REDIS, keyed by the
# HttpOnly device cookie (nothing in localStorage). The login page asks the
# server for this device's accounts and always receives the *latest* user
# objects (name, username, avatar) straight from the database.
# ---------------------------------------------------------------------------
DEVICE_ACCOUNTS_TTL = 180 * 24 * 3600


def _dkey(device_id: str) -> str:
    return f"device:{sha256_hex(device_id)}:accounts"


async def remember_on_device(device_id: str, user_id: int) -> None:
    if not device_id:
        return
    await redis.zadd(_dkey(device_id), {str(user_id): time.time()})
    await redis.expire(_dkey(device_id), DEVICE_ACCOUNTS_TTL)


async def device_account_ids(device_id: str, limit: int = 8) -> list[int]:
    if not device_id or len(device_id) > 64:
        return []
    ids = await redis.zrevrange(_dkey(device_id), 0, limit - 1)
    return [int(i) for i in ids if str(i).isdigit()]


async def forget_on_device(device_id: str, user_id: int) -> None:
    if device_id:
        await redis.zrem(_dkey(device_id), str(user_id))


# ---------------------------------------------------------------------------
# Small per-user / per-device UI state (theme, last section...) kept in Redis
# instead of browser localStorage.
# ---------------------------------------------------------------------------
UI_FIELDS = {"theme", "section", "chat_personal", "chat_business", "chat_coding", "tab_personal", "tab_business",
             "tab_coding", "terminal_open", "sidebar_collapsed", "editor_file", "immersive", "vr_comfort",
             "editor_scope"}


async def get_ui_state(user_id: int) -> dict:
    return await redis.hgetall(f"ui:{user_id}")


async def set_ui_state(user_id: int, values: dict) -> dict:
    clean = {k: str(v)[:200] for k, v in values.items() if k in UI_FIELDS}
    if clean:
        await redis.hset(f"ui:{user_id}", mapping=clean)
    return await get_ui_state(user_id)


async def device_prefs(device_id: str) -> dict:
    if not device_id:
        return {}
    return await redis.hgetall(f"devprefs:{sha256_hex(device_id)}")


async def set_device_prefs(device_id: str, values: dict) -> dict:
    clean = {k: str(v)[:20] for k, v in values.items() if k in ("immersive", "vr_comfort")}
    if device_id and clean:
        key = f"devprefs:{sha256_hex(device_id)}"
        await redis.hset(key, mapping=clean)
        await redis.expire(key, DEVICE_ACCOUNTS_TTL)
    return await device_prefs(device_id)


async def device_theme(device_id: str) -> str:
    if not device_id:
        return "system"
    return await redis.get(f"devtheme:{sha256_hex(device_id)}") or "system"


async def set_device_theme(device_id: str, theme: str) -> None:
    if device_id and theme in ("system", "light", "dark"):
        await redis.set(f"devtheme:{sha256_hex(device_id)}", theme, ex=DEVICE_ACCOUNTS_TTL)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
async def optional_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    uid = getattr(request.state, "uid", None)
    if not uid:
        return None
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    return user


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await optional_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user
