"""Accounts: registration (first-time setup), login, logout, saved accounts
(Redis, per device), password reset by e-mail."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logs import ulog
from app.core.utils import tz_for
from app.db import get_db
from app.models import User, UserSettings
from app.security import sentinel
from app.security.crypto import encrypt, hash_password, verify_password
from app.security.middleware import is_https
from app.security.ratelimit import limiter
from app.security.sessions import (
    ANON_COOKIE,
    activate_single_login,
    clear_single_login,
    SESSION_COOKIE,
    SESSION_TTL,
    create_session,
    current_user,
    destroy_all_sessions,
    destroy_session,
    device_account_ids,
    forget_on_device,
    remember_on_device,
)
from app.services.avatars import avatar_url, save_avatar
from app.services.mailer import MailError, send_mail

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("deminic.auth")
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
_reset = URLSafeTimedSerializer(settings.secret_key, salt="deminic-password-reset")


def _set_session_cookie(request: Request, resp: JSONResponse, token: str) -> None:
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=is_https(request),
                    max_age=SESSION_TTL, path="/")


def _valid_email(email: str) -> str:
    try:
        return validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as e:
        raise HTTPException(422, {"field": "email", "message": str(e)}) from e


def _password_ok(pw: str) -> None:
    if len(pw) < 8 or len(pw) > 128:
        raise HTTPException(422, {"field": "password", "message": "Password must be 8-128 characters"})
    if pw.lower() == pw or not re.search(r"\d", pw):
        raise HTTPException(422, {"field": "password", "message": "Use upper- and lower-case letters and a number"})


def public_user(u: User) -> dict:
    return {"public_id": u.public_id, "name": u.name, "username": u.username, "avatar_url": avatar_url(u.avatar_path),
            "ai_name": u.ai_name}


@router.get("/check-username")
@limiter.limit("60/minute")
async def check_username(request: Request, u: str, db: AsyncSession = Depends(get_db)):
    u = (u or "").strip().lower()
    if not USERNAME_RE.match(u):
        return {"ok": False, "message": "3-32 chars: a-z 0-9 . _ -"}
    taken = (await db.execute(select(func.count()).select_from(User).where(User.username == u))).scalar_one()
    return {"ok": not taken, "message": "taken" if taken else "available"}


@router.post("/register")
@limiter.limit(settings.rate_limit_auth)
async def register(
    request: Request,
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    ai_name: str = Form("Deminic"),
    about: str = Form(""),
    owns_business: bool = Form(False),
    business_name: str = Form(""),
    business_description: str = Form(""),
    gateway_url: str = Form(""),
    gateway_key: str = Form(""),
    local_model: str = Form("gemma"),
    coder_model: str = Form("qwen"),
    claude_key: str = Form(""),
    openai_key: str = Form(""),
    github_models_key: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_security: str = Form("starttls"),
    timezone_name: str = Form("UTC", alias="timezone"),
    avatar: UploadFile | None = File(None),
):
    name = name.strip()[:80]
    username = username.strip().lower()
    if not name:
        raise HTTPException(422, {"field": "name", "message": "Your name is required"})
    if not USERNAME_RE.match(username):
        raise HTTPException(422, {"field": "username", "message": "Username: 3-32 chars, a-z 0-9 . _ -"})
    email = _valid_email(email)
    _password_ok(password)
    if smtp_from_email.strip():
        smtp_from_email = _valid_email(smtp_from_email)
    exists = (await db.execute(select(User).where(or_(User.username == username, User.email == email)))).scalars().all()
    for u in exists:
        if u.username == username:
            raise HTTPException(409, {"field": "username", "message": "That username is taken"})
        raise HTTPException(409, {"field": "email", "message": "An account with that e-mail already exists"})
    avatar_name = None
    if avatar is not None and avatar.filename:
        avatar_name = await save_avatar(avatar)
    tzname = timezone_name if tz_for(timezone_name).key == timezone_name else "UTC"
    user = User(
        username=username, name=name, email=email, password_hash=await hash_password(password),
        ai_name=(ai_name or "Deminic").strip()[:40] or "Deminic", about=about.strip()[:4000],
        owns_business=bool(owns_business), business_name=business_name.strip()[:120],
        business_description=business_description.strip()[:4000], avatar_path=avatar_name, timezone=tzname,
    )
    user.settings = UserSettings(
        gateway_url=(gateway_url or settings.default_gateway_url).strip()[:255],
        gateway_key_enc=encrypt(gateway_key or settings.default_gateway_key),
        local_model=(local_model or settings.default_local_model).strip()[:80],
        coder_model=(coder_model or settings.default_coder_model).strip()[:80],
        claude_key_enc=encrypt(claude_key.strip()), openai_key_enc=encrypt(openai_key.strip()),
        github_models_key_enc=encrypt(github_models_key.strip()),
        smtp_host=smtp_host.strip()[:255], smtp_port=int(smtp_port or 587), smtp_username=smtp_username.strip()[:255],
        smtp_password_enc=encrypt(smtp_password), smtp_from_email=smtp_from_email.strip()[:254],
        smtp_security=smtp_security if smtp_security in ("starttls", "ssl", "none") else "starttls",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await sentinel.register_account(user.email, user.public_id)
    token, th = await create_session(user.id, request.headers.get("user-agent", ""))
    await activate_single_login(user.id, th)
    request.state.new_binding = f"s:{th}"
    await remember_on_device(request.state.anon_id, user.id)
    await ulog(user.id, f"account created - welcome {user.name}! Your AI is called {user.ai_name}.", src="auth")
    resp = JSONResponse({"ok": True, "redirect": "/app", "user": public_user(user)})
    _set_session_cookie(request, resp, token)
    return resp


_DUMMY: list[str] = []


async def _dummy_hash() -> str:
    """Real Argon2 hash so unknown users cost the same time as wrong passwords."""
    if not _DUMMY:
        _DUMMY.append(await hash_password("timing-equaliser-Password1"))
    return _DUMMY[0]


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = True


LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW = 300


@router.post("/login")
@limiter.limit(settings.rate_limit_login)
async def login(request: Request, body: LoginIn, db: AsyncSession = Depends(get_db)):
    from app.core.state import redis
    from app.security.crypto import sha256_hex

    ident = body.login.strip().lower()
    # Per-account brute-force throttle (works even when every LAN device shares one IP).
    fkey = f"loginfail:{sha256_hex(ident)}"
    if int(await redis.get(fkey) or 0) >= LOGIN_FAIL_LIMIT:
        raise HTTPException(429, "Too many failed sign-in attempts for this account - wait 5 minutes and try again")
    user = (await db.execute(select(User).where(or_(User.username == ident, User.email == ident)))).scalar_one_or_none()
    ok = await verify_password(user.password_hash if user else await _dummy_hash(), body.password)
    if not user or not ok:
        n = await redis.incr(fkey)
        if n == 1:
            await redis.expire(fkey, LOGIN_FAIL_WINDOW)
        raise HTTPException(401, "Invalid username/e-mail or password")
    await redis.delete(fkey)
    token, th = await create_session(user.id, request.headers.get("user-agent", ""))
    await activate_single_login(user.id, th)  # one login per account: older sessions die now
    request.state.new_binding = f"s:{th}"
    if body.remember:
        await remember_on_device(request.state.anon_id, user.id)
    await ulog(user.id, f"signed in from {request.client.host if request.client else '?'}", src="auth")
    resp = JSONResponse({"ok": True, "redirect": "/app", "user": public_user(user)})
    _set_session_cookie(request, resp, token)
    return resp


@router.post("/logout")
async def logout(request: Request):
    th = getattr(request.state, "session_hash", None)
    uid = getattr(request.state, "uid", None)
    if th:
        await destroy_session(th, uid)
        await clear_single_login(uid, th)
        await ulog(uid, "signed out", src="auth")
    request.state.new_binding = f"a:{request.state.anon_id}"
    resp = JSONResponse({"ok": True, "redirect": "/login"})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/accounts")
async def saved_accounts(request: Request, db: AsyncSession = Depends(get_db)):
    """Accounts saved on THIS device (Redis, keyed by the HttpOnly device cookie),
    always returned with the latest data from the database."""
    ids = await device_account_ids(request.state.anon_id)
    if not ids:
        return {"accounts": []}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()}
    current = getattr(request.state, "uid", None)
    out = []
    for i in ids:
        u = users.get(i)
        if u:
            masked = u.email[0] + "•••" + u.email[u.email.index("@"):] if "@" in u.email else ""
            out.append({**public_user(u), "email_masked": masked, "signed_in": u.id == current})
    return {"accounts": out}


@router.delete("/accounts/{public_id}")
async def remove_saved_account(public_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    u = (await db.execute(select(User).where(User.public_id == public_id))).scalar_one_or_none()
    if u:
        await forget_on_device(request.state.anon_id, u.id)
    return {"ok": True}


class ForgotIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


@router.post("/forgot")
@limiter.limit("5/minute")
async def forgot(request: Request, body: ForgotIn, db: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    generic = {"ok": True, "message": "If that e-mail belongs to an account, a reset link has been sent."}
    if not user:
        return generic
    token = _reset.dumps({"uid": user.id, "ph": user.password_hash[-16:]})
    base = str(request.base_url).rstrip("/")
    link = f"{base}/reset?token={token}"
    try:
        await send_mail(user.settings, user.email, "Reset your Deminic password",
                        f"Hi {user.name},\n\nUse this link within 1 hour to reset your password:\n{link}\n\n"
                        "If you did not request this, ignore this e-mail.", to_name=user.name, allow_system=True)
        await ulog(user.id, "password reset link e-mailed", src="auth")
    except MailError as e:
        # No SMTP available: surface the link on the server console only (operator access).
        log.warning("password reset for user %s could not be mailed (%s). Reset link: %s", user.id, e, link)
    return generic


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=600)
    password: str = Field(min_length=8, max_length=128)


@router.post("/reset")
@limiter.limit("10/minute")
async def reset_password(request: Request, body: ResetIn, db: AsyncSession = Depends(get_db)):
    try:
        data = _reset.loads(body.token, max_age=3600)
    except SignatureExpired as e:
        raise HTTPException(400, "This reset link has expired") from e
    except BadSignature as e:
        raise HTTPException(400, "Invalid reset link") from e
    user = (await db.execute(select(User).where(User.id == int(data.get("uid", 0))))).scalar_one_or_none()
    if not user or user.password_hash[-16:] != data.get("ph"):
        raise HTTPException(400, "This reset link was already used")
    _password_ok(body.password)
    user.password_hash = await hash_password(body.password)
    user.password_changed_at = datetime.now(timezone.utc)
    await db.commit()
    await destroy_all_sessions(user.id)
    await clear_single_login(user.id)
    await ulog(user.id, "password was reset - all sessions signed out", src="auth")
    return {"ok": True, "redirect": "/login"}


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"user": public_user(user) | {"email": user.email}}
