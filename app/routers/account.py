"""Settings, global memory, UI state (Redis), restore points and global reset."""
from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import failsafe
from app.config import SECTIONS, settings
from app.core import vectors
from app.core.jobs import DuplicateJob, JobContext, jobs
from app.core.logs import clear as clear_logs
from app.core.logs import ulog
from app.core.memory import memory_cache
from app.core.state import redis
from app.core.utils import tz_for
from app.db import SessionLocal, get_db
from app.models import (
    BackgroundJob,
    CalendarEvent,
    Chat,
    Client,
    Email,
    FileVersion,
    LocustRun,
    Memory,
    MemoryVector,
    NewsDigest,
    Reminder,
    Repo,
    RestorePoint,
    SecurityEvent,
    Simulation,
    Summary,
    Task,
    Transaction,
    User,
    UserCounter,
    VulnScan,
)
from app.routers.auth import USERNAME_RE, _password_ok, _valid_email, public_user
from app.security import sentinel
from app.security.crypto import decrypt, encrypt, hash_password, mask, verify_password
from app.security.sessions import current_user, destroy_all_sessions, get_ui_state, set_device_prefs, set_device_theme, set_ui_state
from app.services import memory_service
from app.services.avatars import avatar_url, delete_avatar, save_avatar
from app.services.mailer import MailError, send_mail
from app.services.workspace import user_root

router = APIRouter(prefix="/api", tags=["settings"])


def settings_view(user: User) -> dict:
    s = user.settings
    return {
        "profile": {
            "name": user.name, "username": user.username, "email": user.email, "ai_name": user.ai_name,
            "about": user.about, "timezone": user.timezone, "theme": user.theme, "avatar_url": avatar_url(user.avatar_path),
            "public_id": user.public_id,
        },
        "business": {
            "owns_business": user.owns_business, "business_name": user.business_name,
            "business_description": user.business_description, "business_version": user.business_version,
        },
        "providers": {
            "gateway_url": s.gateway_url, "gateway_key": mask(decrypt(s.gateway_key_enc)), "local_model": s.local_model,
            "coder_model": s.coder_model, "use_docker_fallback": s.use_docker_fallback,
            "fallback_chat_model": settings.fallback_chat_model, "fallback_coder_model": settings.fallback_coder_model,
            "claude_key": mask(decrypt(s.claude_key_enc)), "claude_model": s.claude_model,
            "openai_key": mask(decrypt(s.openai_key_enc)), "openai_model": s.openai_model,
            "github_models_key": mask(decrypt(s.github_models_key_enc)), "github_models_model": s.github_models_model,
            "provider_order": list(s.provider_order or []), "coding_provider": s.coding_provider,
            "youtube_api_key": mask(decrypt(s.youtube_api_key_enc)), "tmdb_api_key": mask(decrypt(s.tmdb_api_key_enc)),
            "watch_region": s.watch_region, "news_region": s.news_region, "share_location": s.share_location,
        },
        "smtp": {
            "smtp_host": s.smtp_host, "smtp_port": s.smtp_port, "smtp_username": s.smtp_username,
            "smtp_password": mask(decrypt(s.smtp_password_enc)), "smtp_from_email": s.smtp_from_email,
            "smtp_security": s.smtp_security,
        },
        "github": {"linked": bool(s.github_token_enc), "login": s.github_login},
    }


@router.get("/settings")
async def get_settings(user: User = Depends(current_user)):
    return settings_view(user)


@router.get("/me")
async def me(user: User = Depends(current_user)):
    view = settings_view(user)
    return {"user": public_user(user) | {"email": user.email, "business_name": user.business_name,
                                         "owns_business": user.owns_business, "timezone": user.timezone,
                                         "theme": user.theme, "business_version": user.business_version},
            "providers": {"has_online": bool(failsafe.online(user.settings)), "coding_provider": user.settings.coding_provider,
                          "share_location": user.settings.share_location},
            "github": view["github"], "ui": await get_ui_state(user.id)}


class ProfileIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    username: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    ai_name: str | None = Field(default=None, max_length=40)
    about: str | None = Field(default=None, max_length=4000)
    timezone: str | None = Field(default=None, max_length=64)
    theme: str | None = Field(default=None, pattern="^(system|light|dark)$")


@router.put("/settings/profile")
async def update_profile(body: ProfileIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    changes = []
    if body.name is not None and body.name.strip() and body.name.strip() != user.name:
        user.name = body.name.strip()
        changes.append("name")
    if body.username is not None and body.username.strip().lower() != user.username:
        u = body.username.strip().lower()
        if not USERNAME_RE.match(u):
            raise HTTPException(422, {"field": "username", "message": "Username: 3-32 chars, a-z 0-9 . _ -"})
        if (await db.execute(select(func.count()).select_from(User).where(User.username == u))).scalar_one():
            raise HTTPException(409, {"field": "username", "message": "That username is taken"})
        user.username = u
        changes.append("username")
    if body.email is not None and body.email.strip().lower() != user.email:
        e = _valid_email(body.email)
        if (await db.execute(select(func.count()).select_from(User).where(User.email == e))).scalar_one():
            raise HTTPException(409, {"field": "email", "message": "That e-mail is already used"})
        await sentinel.unregister_account(user.email, user.public_id)
        user.email = e
        await sentinel.register_account(user.email, user.public_id)
        changes.append("email")
    if body.ai_name is not None and body.ai_name.strip():
        user.ai_name = body.ai_name.strip()
        changes.append("ai_name")
    if body.about is not None:
        user.about = body.about.strip()
        changes.append("about")
    if body.timezone is not None and tz_for(body.timezone).key == body.timezone:
        user.timezone = body.timezone
    if body.theme is not None:
        user.theme = body.theme
        await set_ui_state(user.id, {"theme": body.theme})
    await db.commit()
    if changes:
        await ulog(user.id, f"profile updated: {', '.join(changes)}", src="settings")
    return settings_view(user)


@router.post("/settings/avatar")
async def upload_avatar(avatar: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                        user: User = Depends(current_user)):
    name = await save_avatar(avatar)
    old = user.avatar_path
    user.avatar_path = name
    await db.commit()
    delete_avatar(old)
    await ulog(user.id, "profile photo updated (ClamAV scanned, metadata stripped)", src="settings")
    return {"avatar_url": avatar_url(name)}


@router.delete("/settings/avatar")
async def remove_avatar(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    delete_avatar(user.avatar_path)
    user.avatar_path = None
    await db.commit()
    return {"avatar_url": None}


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


@router.put("/settings/password")
async def change_password(request: Request, body: PasswordIn, db: AsyncSession = Depends(get_db),
                          user: User = Depends(current_user)):
    if not await verify_password(user.password_hash, body.current_password):
        raise HTTPException(403, {"field": "current_password", "message": "Current password is wrong"})
    _password_ok(body.new_password)
    user.password_hash = await hash_password(body.new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    await db.commit()
    await destroy_all_sessions(user.id, keep_hash=request.state.session_hash)
    await ulog(user.id, "password changed - other sessions signed out", src="settings")
    return {"ok": True}


class BusinessIn(BaseModel):
    owns_business: bool | None = None
    business_name: str | None = Field(default=None, max_length=120)
    business_description: str | None = Field(default=None, max_length=4000)
    confirm_new_business: bool = False


@router.put("/settings/business")
async def update_business(body: BusinessIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    desc_changed = body.business_description is not None and body.business_description.strip() != (user.business_description or "").strip()
    affected = (await db.execute(select(func.count()).select_from(Chat).where(
        Chat.user_id == user.id, Chat.section == "business", Chat.business_version == user.business_version))).scalar_one()
    if desc_changed and not body.confirm_new_business:
        raise HTTPException(409, {
            "code": "confirm_new_business",
            "message": "Changing what your business does starts a NEW business context: all business AI summaries "
                       "will be wiped, every old business chat linked to the previous description will be flagged with "
                       "a red warning, and you will have to start a new chat.",
            "affected_chats": affected,
        })
    if body.owns_business is not None:
        user.owns_business = body.owns_business
    if body.business_name is not None:
        # Renaming alone changes nothing else - replies use the {business_name} variable.
        user.business_name = body.business_name.strip()
    wiped = 0
    if desc_changed:
        user.business_description = body.business_description.strip()
        user.business_version = (user.business_version or 1) + 1
        res = await db.execute(delete(Summary).where(Summary.user_id == user.id, Summary.type == "business"))
        wiped = res.rowcount or 0
    await db.commit()
    if desc_changed:
        await ulog(user.id, f"business description changed -> version {user.business_version}; {wiped} business "
                            f"summaries wiped; {affected} old chat(s) flagged as outdated", level="warn", src="settings")
    else:
        await ulog(user.id, "business details updated", src="settings")
    return {"ok": True, "business": settings_view(user)["business"], "outdated_chats": affected if desc_changed else 0,
            "summaries_wiped": wiped}


class ProvidersIn(BaseModel):
    gateway_url: str | None = Field(default=None, max_length=255)
    gateway_key: str | None = Field(default=None, max_length=500)
    local_model: str | None = Field(default=None, max_length=80)
    coder_model: str | None = Field(default=None, max_length=80)
    use_docker_fallback: bool | None = None
    claude_key: str | None = Field(default=None, max_length=500)
    claude_model: str | None = Field(default=None, max_length=80)
    openai_key: str | None = Field(default=None, max_length=500)
    openai_model: str | None = Field(default=None, max_length=80)
    github_models_key: str | None = Field(default=None, max_length=500)
    github_models_model: str | None = Field(default=None, max_length=80)
    provider_order: list[str] | None = None
    coding_provider: str | None = Field(default=None, pattern="^(auto|local|claude|openai|github)$")
    youtube_api_key: str | None = Field(default=None, max_length=200)
    tmdb_api_key: str | None = Field(default=None, max_length=200)
    watch_region: str | None = Field(default=None, max_length=4)
    news_region: str | None = Field(default=None, max_length=8)
    share_location: bool | None = None


def _key_update(value: str | None, current_enc: str) -> str:
    """None/blank -> keep; '__clear__' -> remove; masked value -> keep."""
    if value is None or value.strip() == "" or value.startswith("•"):
        return current_enc
    if value.strip() == "__clear__":
        return ""
    return encrypt(value.strip())


@router.put("/settings/providers")
async def update_providers(body: ProvidersIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    s = user.settings
    for f in ("gateway_url", "local_model", "coder_model", "claude_model", "openai_model", "github_models_model",
              "coding_provider", "watch_region", "news_region"):
        v = getattr(body, f)
        if v is not None and v.strip():
            setattr(s, f, v.strip())
    for f in ("use_docker_fallback", "share_location"):
        v = getattr(body, f)
        if v is not None:
            setattr(s, f, v)
    s.gateway_key_enc = _key_update(body.gateway_key, s.gateway_key_enc)
    s.claude_key_enc = _key_update(body.claude_key, s.claude_key_enc)
    s.openai_key_enc = _key_update(body.openai_key, s.openai_key_enc)
    s.github_models_key_enc = _key_update(body.github_models_key, s.github_models_key_enc)
    s.youtube_api_key_enc = _key_update(body.youtube_api_key, s.youtube_api_key_enc)
    s.tmdb_api_key_enc = _key_update(body.tmdb_api_key, s.tmdb_api_key_enc)
    if body.provider_order is not None:
        order = [p for p in body.provider_order if p in ("claude", "openai", "github")]
        s.provider_order = list(dict.fromkeys(order + ["claude", "openai", "github"]))
    await db.commit()
    await ulog(user.id, "AI provider settings updated (keys encrypted at rest)", src="settings")
    return settings_view(user)


@router.get("/settings/providers/health")
async def providers_health(user: User = Depends(current_user)):
    return await failsafe.health(user.settings)


class ProviderTestIn(BaseModel):
    provider: str = Field(pattern="^(gateway|ollama|claude|openai|github|coder)$")


@router.post("/settings/providers/test")
async def provider_test(body: ProviderTestIn, user: User = Depends(current_user)):
    s = user.settings
    if body.provider == "gateway":
        chain = [p for p in [failsafe.gateway(s)] if p]
    elif body.provider == "coder":
        chain = [p for p in [failsafe.gateway(s, s.coder_model)] if p]
    elif body.provider == "ollama":
        chain = [failsafe.ollama()]
    else:
        chain = failsafe.online(s, only=body.provider)
    if not chain:
        return {"ok": False, "message": "not configured"}
    t0 = time.monotonic()
    try:
        text, p = await failsafe.complete(s, "personal", [{"role": "user", "content": "Reply with the single word: pong"}],
                                          uid=user.id, max_tokens=10, chain=chain)
        return {"ok": True, "message": f"{p.label} replied '{text.strip()[:40]}' in {int((time.monotonic() - t0) * 1000)} ms"}
    except failsafe.AIUnavailable as e:
        return {"ok": False, "message": str(e)[:300]}


class SmtpIn(BaseModel):
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_username: str | None = Field(default=None, max_length=255)
    smtp_password: str | None = Field(default=None, max_length=500)
    smtp_from_email: str | None = Field(default=None, max_length=254)
    smtp_security: str | None = Field(default=None, pattern="^(starttls|ssl|none)$")


@router.put("/settings/smtp")
async def update_smtp(body: SmtpIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    s = user.settings
    if body.smtp_host is not None:
        s.smtp_host = body.smtp_host.strip()
    if body.smtp_port is not None:
        s.smtp_port = body.smtp_port
    if body.smtp_username is not None:
        s.smtp_username = body.smtp_username.strip()
    if body.smtp_from_email is not None:
        s.smtp_from_email = _valid_email(body.smtp_from_email) if body.smtp_from_email.strip() else ""
    if body.smtp_security is not None:
        s.smtp_security = body.smtp_security
    s.smtp_password_enc = _key_update(body.smtp_password, s.smtp_password_enc)
    await db.commit()
    await ulog(user.id, "SMTP settings updated", src="settings")
    return settings_view(user)


@router.post("/settings/smtp/test")
async def smtp_test(user: User = Depends(current_user)):
    try:
        await send_mail(user.settings, user.email, f"{user.ai_name} test e-mail",
                        f"Hi {user.name},\n\nYour SMTP settings work. - {user.ai_name}", to_name=user.name,
                        from_name=user.ai_name)
    except MailError as e:
        return {"ok": False, "message": str(e)}
    await ulog(user.id, f"SMTP test mail sent to {user.email}", src="settings")
    return {"ok": True, "message": f"Test e-mail sent to {user.email}"}


# --------------------------------------------------------------------------- UI state (Redis)
@router.get("/ui-state")
async def ui_state(user: User = Depends(current_user)):
    return await get_ui_state(user.id)


@router.put("/ui-state")
async def put_ui_state(body: dict, user: User = Depends(current_user)):
    return await set_ui_state(user.id, body if isinstance(body, dict) else {})


class DevicePrefsIn(BaseModel):
    immersive: bool | None = None
    vr_comfort: bool | None = None


@router.put("/device-prefs")
async def put_device_prefs(request: Request, body: DevicePrefsIn):
    """Immersive (VR) mode + comfort sizing - Redis per user (UI state) or per device when signed out."""
    vals = {k: ("1" if v else "0") for k, v in body.model_dump(exclude_none=True).items()}
    uid = getattr(request.state, "uid", None)
    if uid:
        await set_ui_state(uid, vals)
    await set_device_prefs(request.state.anon_id, vals)
    return vals


class ThemeIn(BaseModel):
    theme: str = Field(pattern="^(system|light|dark)$")


@router.put("/theme")
async def put_theme(request: Request, body: ThemeIn, db: AsyncSession = Depends(get_db)):
    await set_device_theme(request.state.anon_id, body.theme)
    uid = getattr(request.state, "uid", None)
    if uid:
        await db.execute(update(User).where(User.id == uid).values(theme=body.theme))
        await db.commit()
        await set_ui_state(uid, {"theme": body.theme})
    return {"theme": body.theme}


# --------------------------------------------------------------------------- global memory
def _sec(section: str) -> str:
    if section not in SECTIONS:
        raise HTTPException(400, "Unknown section")
    return section


@router.get("/memory")
async def list_memory(section: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    items = await memory_service.list_memories(db, user.id, _sec(section))
    running = [j for j in jobs.list_for(user.id) if j["kind"] == "memory_wipe" and j["status"] == "running"
               and j["meta"].get("section") == section]
    return {"section": section, "memories": items, "wipe_running": running[0]["request_id"] if running else None}


class MemoryIn(BaseModel):
    section: str
    category: str = Field(max_length=32)
    key: str = Field(min_length=1, max_length=160)
    value: str = Field(default="", max_length=2000)
    data: dict | None = None


@router.post("/memory")
async def add_memory(body: MemoryIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    m = await memory_service.upsert_memory(db, user.id, _sec(body.section), body.category, body.key, body.value, body.data)
    await db.commit()
    await ulog(user.id, f"memory saved [{m.category}] {m.key}", src="memory")
    return {"memory": memory_service.entry_of(m)}


@router.delete("/memory/{mem_id}")
async def remove_memory(mem_id: int, section: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    ok = await memory_service.delete_memory(db, user.id, _sec(section), mem_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    await db.commit()
    await ulog(user.id, f"memory #{mem_id} deleted", src="memory")
    return {"ok": True}


@router.get("/memory/search")
async def search_memory(section: str, q: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    hits = await vectors.search(db, user.id, _sec(section), q[:500], k=10, types=["memory"], kind="memory")
    table = await memory_cache.load(db, user.id, section)
    out = []
    for h in hits:
        e = table["by_id"].get(int(h["source_id"]))
        if e:
            out.append({**e, "score": h["score"]})
    return {"results": out}


class WipeIn(BaseModel):
    section: str


@router.post("/memory/wipe")
async def wipe_memory(body: WipeIn, user: User = Depends(current_user)):
    section = _sec(body.section)

    async def _job(ctx: JobContext):
        return await memory_service.wipe_section_job(ctx, section)

    try:
        h = await jobs.spawn(user.id, "memory_wipe", f"delete {section} global memory", _job, cancellable=False,
                             dedupe_key=f"memwipe:{user.id}:{section}", retries=2, meta={"section": section},
                             restore_label=f"before deleting {section} memory")
    except DuplicateJob as e:
        raise HTTPException(409, "A memory deletion for this section is already running") from e
    return {"request_id": h.id, "restore_point": h.restore_session.id if h.restore_session else None}


# --------------------------------------------------------------------------- restore points
@router.get("/restore-points")
async def restore_points(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = (await db.execute(select(RestorePoint).where(RestorePoint.user_id == user.id)
                             .order_by(RestorePoint.created_at.desc()).limit(50))).scalars().all()
    return {"restore_points": [{"id": r.id, "label": r.label, "status": r.status, "job_id": r.job_id,
                                "changes": len(r.ops or []), "created_at": r.created_at.isoformat(),
                                "restored_at": r.restored_at.isoformat() if r.restored_at else None} for r in rows]}


# --------------------------------------------------------------------------- global reset
class ResetIn(BaseModel):
    confirm: str


USER_TABLES = (MemoryVector, Memory, Task, CalendarEvent, Reminder, Client, Transaction, Email, Summary, NewsDigest,
               Simulation, FileVersion, Repo, VulnScan, LocustRun, SecurityEvent, Chat, UserCounter)


@router.post("/settings/reset")
async def global_reset(body: ResetIn, user: User = Depends(current_user)):
    if body.confirm != "RESET":
        raise HTTPException(400, "Type RESET to confirm")
    if [j for j in jobs.list_for(user.id) if not j["cancellable"] and j["status"] == "running"]:
        raise HTTPException(409, "A protected task (memory deletion) is still running - try again when it finishes")
    uid = user.id

    async def _job(ctx: JobContext):
        rp = ctx.restore
        # 1. cancel every other cancellable task of this user
        for j in jobs.list_for(uid):
            if j["request_id"] != ctx.id and j["status"] in ("running", "scheduled") and j["cancellable"]:
                await jobs.cancel(uid, j["request_id"], reason="global reset")
        await ctx.progress(0.15, "background tasks cancelled")
        # 2. move the workspace aside (restore point can move it back)
        root = user_root(uid)
        trash = settings.data_dir / "trash" / f"u{uid}-{int(time.time())}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        await rp.record({"type": "restore_dir", "from": str(trash), "to": str(root)})
        if root.exists():
            shutil.move(str(root), str(trash))
        await ctx.progress(0.35, "workspace moved to trash")
        # 3. one atomic transaction deletes all user data (rolls back if interrupted)
        async with SessionLocal() as db:
            for model in USER_TABLES:
                await db.execute(delete(model).where(model.user_id == uid))
            await db.execute(delete(BackgroundJob).where(BackgroundJob.user_id == uid, BackgroundJob.id != ctx.id))
            await db.execute(delete(RestorePoint).where(RestorePoint.user_id == uid, RestorePoint.id != rp.id))
            await db.commit()
        await ctx.progress(0.8, "database cleared")
        memory_cache.invalidate(uid)
        await clear_logs(uid)
        await redis.delete(f"ui:{uid}")
        shutil.rmtree(trash, ignore_errors=True)
        user_root(uid)
        await ctx.progress(1.0, "everything deleted - account and settings kept")
        return {"reset": True}

    try:
        h = await jobs.spawn(uid, "global_reset", "global reset - delete everything", _job, cancellable=False,
                             dedupe_key=f"reset:{uid}", retries=0, restore_label="before global reset")
    except DuplicateJob as e:
        raise HTTPException(409, "A reset is already running") from e
    await ulog(uid, "GLOBAL RESET started (non-cancellable, restore point created)", level="warn", src="settings")
    return {"request_id": h.id}
