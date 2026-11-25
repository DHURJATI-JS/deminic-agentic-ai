"""HTML pages (Jinja2) + health and LAN CA download."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import failsafe
from app.config import settings
from app.core.state import redis
from app.db import get_db
from app.models import User
from app.security import clamav
from app.security.sessions import device_prefs, device_theme, get_ui_state, optional_user
from app.services.avatars import avatar_url
from app.templating import render as _render

router = APIRouter(include_in_schema=False)


async def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = dict(context or {})
    ctx.setdefault("prefs", await _prefs(request))
    return _render(request, name, ctx, status_code=status_code)


async def _theme(request: Request, user: User | None) -> str:
    if user:
        return user.theme or "system"
    return await device_theme(getattr(request.state, "anon_id", ""))


async def _prefs(request: Request) -> dict:
    """Immersive (VR) + comfort prefs: per user in Redis UI state, per device when signed out."""
    uid = getattr(request.state, "uid", None)
    if uid:
        ui = await get_ui_state(uid)
    else:
        ui = await device_prefs(getattr(request.state, "anon_id", ""))
    return {"immersive": ui.get("immersive") == "1", "vr_comfort": ui.get("vr_comfort", "")}


def _user_ctx(user: User | None) -> dict | None:
    if not user:
        return None
    return {"name": user.name, "username": user.username, "ai_name": user.ai_name, "email": user.email,
            "avatar_url": avatar_url(user.avatar_path), "business_name": user.business_name,
            "public_id": user.public_id}


VIDEO_DIR = Path(__file__).resolve().parents[2] / "static" / "video"
DEMO_CLIPS = (("chat", "Live chat", "message"), ("business", "Business desk", "briefcase"),
              ("coding", "Coding studio", "code"), ("status", "System status", "radio"), ("hero", "Immersive VR", "vr"))
_captions: tuple[float, dict] = (0.0, {})


def _demo_clips() -> list[dict]:
    """Recorded demo videos (static/video/<key>.mp4) with the captions the local AI wrote for them."""
    global _captions
    f = VIDEO_DIR / "captions.json"
    try:
        mtime = f.stat().st_mtime
        if mtime != _captions[0]:
            _captions = (mtime, json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        _captions = (0.0, {})
    return [{"key": k, "title": t, "icon": i, "caption": (_captions[1].get(k) or {}).get("caption", "")}
            for k, t, i in DEMO_CLIPS if (VIDEO_DIR / f"{k}.mp4").exists()]


@router.get("/")
async def home(request: Request, user: User | None = Depends(optional_user)):
    return await render(request, "home.html", {"theme": await _theme(request, user), "user": _user_ctx(user),
                                               "demo_clips": _demo_clips()})


@router.get("/login")
async def login_page(request: Request, user: User | None = Depends(optional_user)):
    if user:  # already signed in: never show sign-in / sign-up screens
        return RedirectResponse("/app", status_code=303)
    return await render(request, "login.html", {"theme": await _theme(request, user), "user": None})


@router.get("/register")
async def register_page(request: Request, user: User | None = Depends(optional_user)):
    if user:
        return RedirectResponse("/app", status_code=303)
    return await render(request, "register.html", {
        "theme": await _theme(request, user), "user": None,
        "default_gateway_url": settings.default_gateway_url, "default_gateway_key": settings.default_gateway_key,
        "default_local_model": settings.default_local_model, "default_coder_model": settings.default_coder_model,
    })


@router.get("/forgot")
async def forgot_page(request: Request, user: User | None = Depends(optional_user)):
    return await render(request, "forgot.html", {"theme": await _theme(request, user), "user": _user_ctx(user)})


@router.get("/reset")
async def reset_page(request: Request, token: str = "", user: User | None = Depends(optional_user)):
    return await render(request, "reset.html", {"theme": await _theme(request, user), "token": token, "user": _user_ctx(user)})


@router.get("/app")
async def app_page(request: Request, user: User | None = Depends(optional_user)):
    if not user:
        why = "?reason=elsewhere" if getattr(request.state, "session_replaced", False) else ""
        return RedirectResponse(f"/login{why}", status_code=303)
    return await render(request, "app.html", {"theme": await _theme(request, user), "user": _user_ctx(user)})


@router.get("/settings")
async def settings_page(request: Request, user: User | None = Depends(optional_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    return await render(request, "settings.html", {"theme": await _theme(request, user), "user": _user_ctx(user)})


@router.get("/locust")
async def locust_page(request: Request, user: User | None = Depends(optional_user)):
    report = None
    f = settings.reports_dir / "latest.json"
    if f.exists():
        try:
            report = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            report = None
    return await render(request, "locust.html", {"theme": await _theme(request, user), "user": _user_ctx(user), "report": report})


@router.get("/disclaimer")
async def disclaimer_page(request: Request, user: User | None = Depends(optional_user)):
    return await render(request, "disclaimer.html", {"theme": await _theme(request, user), "user": _user_ctx(user)})


@router.get("/technical")
async def technical_page(request: Request, user: User | None = Depends(optional_user)):
    """Public technical overview: how the system works end to end."""
    return await render(request, "technical.html", {"theme": await _theme(request, user), "user": _user_ctx(user)})


@router.get("/ca.crt")
async def ca_cert():
    f = settings.tls_dir / "deminic-local-ca.crt"
    if not f.exists():
        return JSONResponse({"detail": "LAN HTTPS is not enabled"}, status_code=404)
    return FileResponse(f, media_type="application/x-x509-ca-cert", filename="deminic-local-ca.crt")


@router.get("/healthz")
async def healthz(db: AsyncSession = Depends(get_db)):
    """Fast liveness/readiness: every probe runs concurrently with a tight timeout."""
    import asyncio

    async def pg() -> bool:
        await db.execute(text("SELECT 1"))
        return True

    async def rd() -> bool:
        return bool(await redis.ping())

    async def models(url: str, key: str) -> bool:
        return (await failsafe.served_models(url, key)) is not None

    probes = {
        "postgres": pg(), "redis": rd(), "clamav": clamav.ping(),
        "ollama_fallback": models(settings.ollama_url, ""),
        "gateway": models(failsafe.loopback_if_local(settings.default_gateway_url), settings.default_gateway_key),
    }

    async def guard(coro):
        try:
            return bool(await asyncio.wait_for(coro, timeout=2.5))
        except Exception:  # noqa: BLE001
            return False

    results = await asyncio.gather(*(guard(c) for c in probes.values()))
    return {"app": True, **dict(zip(probes.keys(), results))}
