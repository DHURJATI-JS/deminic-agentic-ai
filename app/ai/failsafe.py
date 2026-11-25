"""AI failsafe: provider chains, retries, circuit breakers, context shrinking
and the strict ``BEYOND MY LIMITS`` escalation to online providers.

Chains
------
personal / business : gateway(gemma) -> docker ollama(gemma3) -> online (last resort)
coding              : [preferred online] -> gateway(qwen coder, if served) ->
                      docker ollama(qwen2.5-coder) -> online.  Gemma is never used.
Escalation          : when a local model answers ``BEYOND MY LIMITS`` the request
                      is re-run on the user's online providers (Claude / ChatGPT /
                      GitHub Models) in their preferred order.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncIterator

import httpx

from app.ai import providers
from app.ai.providers import Provider, ProviderError
from app.config import settings
from app.core.logs import ulog
from app.models import UserSettings
from app.security.crypto import decrypt

BEYOND_RE = re.compile(r"\bBEYOND\s+(MY|ITS|THE)\s+LIMITS?\b", re.I)
RESET = "\x00RESET\x00"

# Background (insights / summaries) LLM work is throttled so foreground chat
# replies keep priority on the limited local GPU slots.
BG_SEM = asyncio.Semaphore(2)


class AIUnavailable(Exception):
    pass


class NoOnlineProvider(Exception):
    pass


# --------------------------------------------------------------------------- circuit breaker
_circuit: dict[str, float] = {}


def circuit_open(p: Provider) -> bool:
    until = _circuit.get(p.key)
    return bool(until and until > time.monotonic())


def trip(p: Provider, seconds: float = 30.0) -> None:
    _circuit[p.key] = time.monotonic() + seconds


def reset(p: Provider) -> None:
    _circuit.pop(p.key, None)


# --------------------------------------------------------------------------- model discovery
_models_cache: dict[str, tuple[float, set[str] | None]] = {}


async def served_models(base_url: str, api_key: str) -> set[str] | None:
    hit = _models_cache.get(base_url)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        names: set[str] | None = set(await providers.list_models(base_url, api_key))
        ttl = 60.0
    except Exception:  # noqa: BLE001
        names, ttl = None, 15.0
    _models_cache[base_url] = (time.monotonic() + ttl, names)
    return names


# --------------------------------------------------------------------------- providers
_LOCAL_IPS: set[str] | None = None


def _local_ips() -> set[str]:
    global _LOCAL_IPS
    if _LOCAL_IPS is None:
        import socket

        ips = {"127.0.0.1", "localhost"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.connect(("10.255.255.255", 1))
            ips.add(u.getsockname()[0])
            u.close()
        except OSError:
            pass
        _LOCAL_IPS = ips
    return _LOCAL_IPS


def loopback_if_local(url: str) -> str:
    """If the gateway URL points at THIS machine's own LAN IP, talk to it over
    loopback instead: same server, but immune to firewall rules on the LAN
    interface. Not done inside Docker (loopback is the container there)."""
    import os
    from urllib.parse import urlsplit, urlunsplit

    if os.path.exists("/.dockerenv"):
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.hostname or ""
    if host and host not in ("127.0.0.1", "localhost") and host in _local_ips():
        netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def loopback_twin(p: Provider) -> Provider | None:
    """Same gateway on 127.0.0.1 - used when the configured LAN IP went stale
    (e.g. DHCP gave this PC a new address) but the gateway runs on this machine."""
    import os
    from urllib.parse import urlsplit, urlunsplit

    if os.path.exists("/.dockerenv"):
        return None
    parts = urlsplit(p.base_url)
    if (parts.hostname or "") in ("127.0.0.1", "localhost", ""):
        return None
    netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
    url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return Provider("gateway", p.kind, url, p.api_key, p.model, f"Local gateway via loopback ({p.model})",
                    images=p.images)


def gateway(s: UserSettings, model: str | None = None) -> Provider | None:
    url = loopback_if_local((s.gateway_url or "").strip())
    if not url:
        return None
    m = model or s.local_model or settings.default_local_model
    return Provider("gateway", "openai", url, decrypt(s.gateway_key_enc), m, f"Local gateway ({m})")


def ollama(coder: bool = False) -> Provider:
    m = settings.fallback_coder_model if coder else settings.fallback_chat_model
    return Provider("ollama", "openai", settings.ollama_url, "", m, f"Docker fallback ({m})", images=not coder)


def online(s: UserSettings, only: str | None = None) -> list[Provider]:
    table = {
        "claude": (decrypt(s.claude_key_enc), lambda k: Provider("claude", "anthropic", providers.ANTHROPIC_URL, k,
                                                               s.claude_model or "claude-opus-5-5",
                                                               f"Claude ({s.claude_model})", online=True, json_schema=False)),
        "openai": (decrypt(s.openai_key_enc), lambda k: Provider("openai", "openai", providers.OPENAI_URL, k,
                                                               s.openai_model or "gpt-4.1",
                                                               f"ChatGPT ({s.openai_model})", online=True)),
        "github": (decrypt(s.github_models_key_enc), lambda k: Provider("github", "openai", providers.GITHUB_MODELS_URL, k,
                                                                      s.github_models_model or "openai/gpt-4.1",
                                                                      f"GitHub Models ({s.github_models_model})", online=True)),
    }
    order = list(s.provider_order or ["claude", "openai", "github"])
    for name in ("claude", "openai", "github"):
        if name not in order:
            order.append(name)
    out = []
    for name in order:
        if only and name != only:
            continue
        key, mk = table.get(name, ("", None))
        if key and mk:
            out.append(mk(key))
    return out


async def chain_for(s: UserSettings, section: str, *, allow_online: bool = True,
                    override: str | None = None) -> list[Provider]:
    chain: list[Provider] = []
    if section == "coding":
        pref = override or s.coding_provider or "auto"
        if pref in ("claude", "openai", "github"):
            chain += online(s, only=pref)
        if pref in ("auto", "local") or not chain:
            gw = gateway(s, s.coder_model or settings.default_coder_model)
            if gw:
                gw.images = False  # coder models are text-only
                lb = loopback_twin(gw)
                served_lb = (await served_models(lb.base_url, lb.api_key)) if lb else None
                if lb and served_lb is not None and lb.model in served_lb:
                    chain.append(lb)
                served = await served_models(gw.base_url, gw.api_key)
                if served is not None and gw.model in served:
                    chain.append(gw)
            if s.use_docker_fallback:
                chain.append(ollama(coder=True))
        if allow_online:
            chain += [p for p in online(s) if p.key not in {c.key for c in chain}]
        return chain
    gw = gateway(s)
    if gw:
        chain.append(gw)
        lb = loopback_twin(gw)
        if lb:
            chain.append(lb)
    if s.use_docker_fallback:
        chain.append(ollama(coder=False))
    if allow_online:
        chain += online(s)
    return chain


def shrink(messages: list[dict]) -> list[dict]:
    """Halve the largest system block - used after a context overflow."""
    out = [dict(m) for m in messages]
    idx = max(range(len(out)), key=lambda i: len(out[i]["content"]) if isinstance(out[i]["content"], str) else 0)
    c = out[idx]["content"]
    if isinstance(c, str) and len(c) > 1200:
        keep = len(c) // 2
        out[idx]["content"] = c[:600] + "\n...[trimmed for context]...\n" + c[-(keep - 600):]
    return out


async def _note(uid: int | None, msg: str, level: str = "warn") -> None:
    await ulog(uid, msg, level=level, src="failsafe")


# --------------------------------------------------------------------------- complete
async def complete(
    s: UserSettings,
    section: str,
    messages: list[dict],
    *,
    uid: int | None = None,
    json_schema: dict | None = None,
    max_tokens: int = 800,
    temperature: float = 0.3,
    background: bool = False,
    allow_online: bool = True,
    chain: list[Provider] | None = None,
    override: str | None = None,
) -> tuple[str, Provider]:
    chain = chain if chain is not None else await chain_for(s, section, allow_online=allow_online, override=override)
    if not chain:
        raise AIUnavailable("no AI provider configured")
    errors: list[str] = []
    sem = BG_SEM if background else contextlib.nullcontext()
    for p in chain:
        if circuit_open(p):
            errors.append(f"{p.label}: circuit open")
            continue
        msgs = messages
        for attempt in range(3 if not p.online else 2):
            try:
                async with sem:  # type: ignore[attr-defined]
                    text = await providers.complete(p, msgs, max_tokens=max_tokens, temperature=temperature,
                                                    json_schema=json_schema, timeout=settings.llm_timeout_s)
                reset(p)
                return text, p
            except ProviderError as e:
                if e.context_overflow and attempt == 0:
                    msgs = shrink(msgs)
                    await _note(uid, f"{p.label}: context overflow - shrinking prompt and retrying")
                    continue
                errors.append(str(e))
                if not e.retryable:
                    break
                await asyncio.sleep(0.8 * (attempt + 1))
            except (httpx.TransportError, httpx.TimeoutException, OSError) as e:
                errors.append(f"{p.label}: {type(e).__name__}")
                trip(p, 60 if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)) else 8)
                break
        await _note(uid, f"{p.label} unavailable - failing over to the next provider")
    raise AIUnavailable("; ".join(errors)[-600:])


# --------------------------------------------------------------------------- stream
async def stream(
    s: UserSettings,
    section: str,
    messages: list[dict],
    *,
    uid: int | None = None,
    max_tokens: int = 1500,
    temperature: float = 0.5,
    allow_online: bool = True,
    chain: list[Provider] | None = None,
    override: str | None = None,
) -> AsyncIterator[tuple[str, Provider]]:
    """Yield (delta, provider). Yields (RESET, provider) if a provider died
    mid-answer and the next provider restarts the answer from scratch."""
    chain = chain if chain is not None else await chain_for(s, section, allow_online=allow_online, override=override)
    if not chain:
        raise AIUnavailable("no AI provider configured")
    errors: list[str] = []
    for p in chain:
        if circuit_open(p):
            errors.append(f"{p.label}: circuit open")
            continue
        msgs = messages
        for attempt in range(2):
            emitted = False
            try:
                async for delta in providers.stream(p, msgs, max_tokens=max_tokens, temperature=temperature,
                                                    timeout=settings.llm_timeout_s):
                    if not emitted:
                        emitted = True
                    yield delta, p
                reset(p)
                return
            except ProviderError as e:
                errors.append(str(e))
                if emitted:
                    yield RESET, p
                if e.context_overflow and attempt == 0:
                    msgs = shrink(msgs)
                    await _note(uid, f"{p.label}: context overflow - shrinking prompt and retrying")
                    continue
                if e.retryable and attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                break
            except (httpx.TransportError, httpx.TimeoutException, OSError) as e:
                errors.append(f"{p.label}: {type(e).__name__}")
                if emitted:
                    yield RESET, p
                trip(p, 60 if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)) else 8)
                break
        await _note(uid, f"{p.label} failed mid-request - failing over to the next provider")
    raise AIUnavailable("; ".join(errors)[-600:])


def is_beyond(text: str) -> bool:
    return bool(BEYOND_RE.search(text or ""))


async def health(s: UserSettings) -> dict:
    """Quick status of each configured provider (used by the settings page)."""
    out = {}
    gw = gateway(s)
    if gw:
        out["gateway"] = {"label": gw.label, "ok": (await served_models(gw.base_url, gw.api_key)) is not None,
                          "models": sorted(await served_models(gw.base_url, gw.api_key) or [])}
    ol = ollama()
    served = await served_models(ol.base_url, "")
    out["ollama"] = {"label": "Docker fallback (Ollama)", "ok": served is not None, "models": sorted(served or [])}
    for p in online(s):
        out[p.name] = {"label": p.label, "ok": None, "models": [p.model]}
    return out
