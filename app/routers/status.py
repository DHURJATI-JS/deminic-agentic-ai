"""System status - live pings of everything the AI depends on, plus an
end-to-end AI response test. Powers the status dialog next to the theme toggle.

GET  /api/status/ping     every component probed concurrently (2.5 s cap each,
                          never cached, so the dialog shows real-time latency);
                          ?deep=1 also checks online-provider reachability and versions
POST /api/status/ai-test  a real "pong" generation through the LOCAL chain that
                          chats (tier=chat) or coding (tier=coding) would use

Signed-out visitors get the public subset (no provider URLs, keys or online
providers, no AI test). Online providers are never called with a key here -
the deep check only measures whether their API host is reachable.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import failsafe, providers
from app.ai.providers import Provider
from app.config import settings
from app.core import embeddings
from app.core.jobs import jobs
from app.core.state import STARTED_AT, redis
from app.db import get_db
from app.models import User
from app.security import clamav
from app.security.ratelimit import limiter
from app.security.sessions import current_user, optional_user

router = APIRouter(prefix="/api/status", tags=["status"])

PROBE_TIMEOUT = 2.5
TEST_TIMEOUT = 45.0  # a cold Docker model can take a while to load
ONLINE_HOSTS = {"claude": "https://api.anthropic.com", "openai": "https://api.openai.com",
                "github": "https://models.github.ai"}
TEST_MESSAGES = [
    {"role": "system", "content": "You are a health check. Reply with exactly one word: pong"},
    {"role": "user", "content": "ping"},
]


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _short(e: BaseException) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, providers.ProviderError):
        return f"HTTP {e.status}"
    if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timed out"
    return type(e).__name__


async def _timed(coro, timeout: float = PROBE_TIMEOUT) -> tuple[bool, float, object, str]:
    """(ok, ms, value, error) - never raises."""
    t0 = time.perf_counter()
    try:
        value = await asyncio.wait_for(coro, timeout)
        return True, _ms(t0), value, ""
    except Exception as e:  # noqa: BLE001 - a status probe must never fail the request
        return False, _ms(t0), None, _short(e)


def _has(models: set[str] | None, name: str) -> bool:
    if not models or not name:
        return False
    if name in models or f"{name}:latest" in models:
        return True
    return ":" not in name and any(m.split(":", 1)[0] == name for m in models)


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


def _row(key: str, group: str, label: str, state: str, detail: str = "", ms: float | None = None,
         note: str = "") -> dict:
    """state: ok | info (working, on a fallback) | warn | down | off (not configured)."""
    return {"key": key, "group": group, "label": label, "state": state, "detail": detail, "ms": ms, "note": note}


def _config(user: User | None) -> SimpleNamespace:
    s = user.settings if user else None
    if s is not None:
        gw = failsafe.gateway(s)
    else:
        url = failsafe.loopback_if_local(settings.default_gateway_url or "")
        gw = Provider("gateway", "openai", url, settings.default_gateway_key, settings.default_local_model,
                      f"Local gateway ({settings.default_local_model})") if url else None
    return SimpleNamespace(
        s=s, gw=gw,
        gw_display=_host((s.gateway_url if s else settings.default_gateway_url) or ""),
        chat_model=(s.local_model if s else None) or settings.default_local_model,
        coder_model=(s.coder_model if s else None) or settings.default_coder_model,
        use_fallback=bool(s.use_docker_fallback) if s else True,
        online=failsafe.online(s) if s else [],
    )


async def _models(url: str, key: str) -> set[str]:
    return set(await providers.list_models(url, key, timeout=PROBE_TIMEOUT))


async def _postgres(db: AsyncSession, deep: bool) -> str | None:
    await db.execute(text("SELECT 1"))
    if deep:
        return (await db.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))).scalar_one_or_none()
    return None


async def _clamav() -> bool:
    if not await clamav.ping():
        raise ConnectionError("no PONG")
    return True


async def _reachable(url: str) -> int:
    """Any HTTP answer means the API host is reachable (no key is sent)."""
    r = await providers.http().head(url, timeout=3.0)
    return r.status_code


@router.get("/ping")
@limiter.limit("240/minute")
async def ping(request: Request, deep: bool = False, user: User | None = Depends(optional_user),
               db: AsyncSession = Depends(get_db)):
    c = _config(user)
    signed_in = user is not None
    probes: dict[str, object] = {
        "postgres": _postgres(db, deep), "redis": redis.ping(), "clamav": _clamav(),
        "ollama": _models(settings.ollama_url, ""),
    }
    if c.gw:
        probes["gateway"] = _models(c.gw.base_url, c.gw.api_key)
    if deep and signed_in:
        for p in c.online:
            probes[f"online:{p.name}"] = _reachable(ONLINE_HOSTS.get(p.name, p.base_url))
    names = list(probes)
    results = dict(zip(names, await asyncio.gather(*(_timed(probes[n]) for n in names))))

    rows: list[dict] = []
    # ------------------------------------------------------------------ AI
    gw_ok = gw_chat = gw_coder = False
    if c.gw:
        ok, ms, served, err = results["gateway"]
        gw_ok = ok
        where = f" · {c.gw_display}" if signed_in else ""
        if not ok:
            rows.append(_row("gateway", "ai", "AI gateway (chat)", "down", f"{c.chat_model}{where}", ms,
                             f"Not reachable ({err})"))
        else:
            gw_chat, gw_coder = _has(served, c.chat_model), _has(served, c.coder_model)
            if not gw_chat:
                rows.append(_row("gateway", "ai", "AI gateway (chat)", "warn", f"{c.chat_model}{where}", ms,
                                 f"Reachable, but '{c.chat_model}' is not loaded"))
            elif failsafe.circuit_open(c.gw):
                rows.append(_row("gateway", "ai", "AI gateway (chat)", "warn", f"{c.chat_model}{where}", ms,
                                 "Cooling down after errors - chats skip it for a few seconds"))
            else:
                rows.append(_row("gateway", "ai", "AI gateway (chat)", "ok", f"{c.chat_model}{where}", ms,
                                 f"'{c.chat_model}' loaded"))
    else:
        rows.append(_row("gateway", "ai", "AI gateway (chat)", "off", "", None, "No gateway configured"))

    ok, ms, olm, err = results["ollama"]
    fb_chat = ok and _has(olm, settings.fallback_chat_model)
    fb_coder = ok and _has(olm, settings.fallback_coder_model)
    if gw_coder:
        rows.append(_row("coder", "ai", "Coding model", "ok", f"{c.coder_model} on your gateway", results["gateway"][1],
                         "Loaded"))
    elif c.use_fallback and fb_coder:
        rows.append(_row("coder", "ai", "Coding model", "info", f"{settings.fallback_coder_model} · Docker", ms,
                         f"Using the Docker fallback - your gateway does not serve '{c.coder_model}'"))
    elif c.online:
        rows.append(_row("coder", "ai", "Coding model", "warn", "Online providers only", None,
                         "No local coding model is available"))
    else:
        rows.append(_row("coder", "ai", "Coding model", "down", c.coder_model, None, "No coding model available"))

    fb_detail = f"{settings.fallback_chat_model} · {settings.fallback_coder_model}"
    if not c.use_fallback:
        rows.append(_row("fallback", "ai", "Docker fallback models", "off", fb_detail, ms if ok else None,
                         "Turned off in Settings"))
    elif not ok:
        rows.append(_row("fallback", "ai", "Docker fallback models", "down", fb_detail, ms, f"Ollama not reachable ({err})"))
    elif not (fb_chat and fb_coder):
        missing = [m for m, have in ((settings.fallback_chat_model, fb_chat), (settings.fallback_coder_model, fb_coder))
                   if not have]
        rows.append(_row("fallback", "ai", "Docker fallback models", "warn", fb_detail, ms,
                         f"Missing {', '.join(missing)} - re-run the installer to pull it"))
    else:
        rows.append(_row("fallback", "ai", "Docker fallback models", "ok", fb_detail, ms, "Both models ready"))

    if ok and _has(olm, settings.embed_model) and embeddings.primary_available():
        rows.append(_row("embeddings", "ai", "Embeddings (vector search)", "ok", f"{settings.embed_model} · 384-d", None,
                         "Semantic routing and memory search"))
    else:
        rows.append(_row("embeddings", "ai", "Embeddings (vector search)", "warn", "hash-v1 fallback", None,
                         "Search still works, with lower accuracy, until the embedding model is back"))

    if signed_in:
        if not c.online:
            rows.append(_row("online", "ai", "Online escalation", "off", "Claude · ChatGPT · GitHub Models", None,
                             "Not configured - add a key in Settings for BEYOND MY LIMITS tasks"))
        for p in c.online:
            r = results.get(f"online:{p.name}")
            if r is None:
                rows.append(_row(f"online:{p.name}", "ai", p.label, "ok", "Key saved", None, "Ready for escalation"))
            elif r[0]:
                rows.append(_row(f"online:{p.name}", "ai", p.label, "ok", "Key saved · API reachable", r[1],
                                 "Ready for escalation"))
            else:
                rows.append(_row(f"online:{p.name}", "ai", p.label, "warn", "Key saved", r[1],
                                 f"API host not reachable ({r[3]})"))

    # ------------------------------------------------------------------ services
    pok, pms, pver, perr = results["postgres"]
    rows.append(_row("postgres", "services", "PostgreSQL + pgvector", "ok" if pok else "down",
                     f"pgvector {pver}" if pver else "Database and vector search", pms, "" if pok else perr))
    rok, rms, _, rerr = results["redis"]
    rows.append(_row("redis", "services", "Redis", "ok" if rok else "down", "Sessions, live state and logs", rms,
                     "" if rok else rerr))
    cok, cms, _, cerr = results["clamav"]
    rows.append(_row("clamav", "services", "ClamAV", "ok" if cok else "down", "Virus scanning of uploads and repos", cms,
                     "" if cok else f"Uploads cannot be scanned ({cerr})"))
    if signed_in:
        running = len(jobs.running_for(user.id))
        rows.append(_row("jobs", "services", "Background tasks", "ok", f"{running} running", None, ""))

    # ------------------------------------------------------------------ verdict
    gw_usable = gw_ok and gw_chat and not (c.gw and failsafe.circuit_open(c.gw))
    fb_usable = c.use_fallback and fb_chat
    if gw_usable:
        route = c.gw.label if signed_in else f"Local gateway ({c.chat_model})"
    elif fb_usable:
        route = f"Docker fallback ({settings.fallback_chat_model})"
    elif c.online:
        route = c.online[0].label
    else:
        route = ""
    core_ok = pok and rok
    if not core_ok:
        overall, headline = "down", "Core services are down"
    elif not route:
        overall, headline = "down", "AI unavailable - no model can answer right now"
    elif any(r["state"] in ("warn", "down") for r in rows):
        overall = "degraded"
        headline = ("Gateway offline - chats are answered by the fallback" if not gw_usable
                    else "Running with reduced capacity")
    else:
        overall, headline = "ok", "All systems operational"
    return {
        "signed_in": signed_in, "overall": overall, "headline": headline, "chat_route": route,
        "server_time": time.time(), "uptime_s": int(time.time() - STARTED_AT), "components": rows,
    }


class AITestIn(BaseModel):
    tier: str = Field(default="chat", pattern="^(chat|coding)$")


@router.post("/ai-test")
@limiter.limit("20/minute")
async def ai_test(request: Request, body: AITestIn, user: User = Depends(current_user)):
    """Real generation through the local chain (online providers are never
    called, so the test costs nothing). Reports which model answered, time to
    first token and total time; a successful answer also closes that
    provider's circuit breaker."""
    lock = f"status:aitest:{user.id}:{body.tier}"
    if not await redis.set(lock, "1", nx=True, ex=int(TEST_TIMEOUT) + 15):
        raise HTTPException(status_code=409, detail="This test is already running")
    try:
        s = user.settings
        chain = await failsafe.chain_for(s, "coding" if body.tier == "coding" else "personal", allow_online=False)
        attempts: list[dict] = []
        for p in chain:
            t0 = time.perf_counter()
            first: float | None = None
            parts: list[str] = []
            try:
                async with asyncio.timeout(TEST_TIMEOUT):
                    async for delta in providers.stream(p, TEST_MESSAGES, max_tokens=12, temperature=0.0,
                                                        timeout=TEST_TIMEOUT):
                        if first is None:
                            first = _ms(t0)
                        parts.append(delta)
            except Exception as e:  # noqa: BLE001 - report and try the next provider
                attempts.append({"provider": p.label, "ok": False, "error": _short(e), "ms": _ms(t0)})
                continue
            total = _ms(t0)
            reply = "".join(parts).strip()
            failsafe.reset(p)
            attempts.append({"provider": p.label, "ok": True, "ms": total})
            return {"ok": True, "tier": body.tier, "provider": p.label, "kind": p.name, "model": p.model,
                    "ttft_ms": first if first is not None else total, "total_ms": total, "reply": reply[:80],
                    "correct": "pong" in reply.lower(), "fallback_used": len(attempts) > 1, "attempts": attempts}
        return {"ok": False, "tier": body.tier, "attempts": attempts,
                "online_ready": [p.label for p in failsafe.online(s)],
                "message": "No local model answered" if chain else "No local model is configured"}
    finally:
        await redis.delete(lock)
