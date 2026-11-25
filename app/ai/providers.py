"""LLM provider adapters.

* openai-compatible: local AI gateway (llama.cpp - gemma / qwen), dockerised
  Ollama fallback, OpenAI (ChatGPT) and GitHub Models (Copilot models).
* anthropic: Claude Messages API.

Messages use the OpenAI shape: {"role": ..., "content": str | [parts]} where a
part is {"type": "text", "text": ...} or {"type": "image_url", "image_url": {"url": data-url}}.
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.core.state import http

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1"
GITHUB_MODELS_URL = "https://models.github.ai/inference"


@dataclass
class Provider:
    name: str  # gateway | ollama | claude | openai | github
    kind: str  # openai | anthropic
    base_url: str
    api_key: str
    model: str
    label: str
    online: bool = False
    json_schema: bool = True
    images: bool = True

    @property
    def key(self) -> str:
        return f"{self.name}|{self.base_url}|{self.model}"


class ProviderError(Exception):
    def __init__(self, provider: str, status: int, message: str):
        super().__init__(f"{provider}: HTTP {status}: {message[:300]}")
        self.status = status
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.status in (408, 409, 425, 429) or self.status >= 500

    @property
    def context_overflow(self) -> bool:
        m = self.message.lower()
        return self.status == 400 and ("context" in m or "too long" in m or "exceed" in m or "n_ctx" in m)


def _headers(p: Provider) -> dict:
    if p.kind == "anthropic":
        return {"x-api-key": p.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    h = {"Content-Type": "application/json"}
    if p.api_key:
        h["Authorization"] = f"Bearer {p.api_key}"
    if p.name == "github":
        h["Accept"] = "application/vnd.github+json"
        h["X-GitHub-Api-Version"] = "2022-11-28"
    return h


def _openai_body(p: Provider, messages: list[dict], max_tokens: int, temperature: float,
                 json_schema: dict | None, stream: bool) -> dict:
    msgs = messages
    if not p.images:
        msgs = [_strip_images(m) for m in messages]
    body: dict = {"model": p.model, "messages": msgs, "stream": stream}
    if p.name == "openai":
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = temperature
    if p.name == "openai" and not p.model.startswith(("o1", "o3", "o4", "gpt-5")):
        body["temperature"] = temperature
    if json_schema is not None:
        if p.json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": json_schema},
            }
        else:
            body["response_format"] = {"type": "json_object"}
    return body


def _strip_images(m: dict) -> dict:
    c = m.get("content")
    if isinstance(c, list):
        return {**m, "content": "\n".join(p.get("text", "") for p in c if p.get("type") == "text")}
    return m


def _anthropic_body(p: Provider, messages: list[dict], max_tokens: int, temperature: float,
                    json_schema: dict | None, stream: bool) -> dict:
    system_parts: list[str] = []
    conv: list[dict] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "system":
            system_parts.append(content if isinstance(content, str) else json.dumps(content))
            continue
        parts: list[dict] = []
        if isinstance(content, str):
            parts.append({"type": "text", "text": content or " "})
        else:
            for c in content:
                if c.get("type") == "text":
                    parts.append({"type": "text", "text": c.get("text") or " "})
                elif c.get("type") == "image_url":
                    url = c["image_url"]["url"]
                    mm = re.match(r"data:(image/[a-z+]+);base64,(.*)", url, re.S)
                    if mm:
                        parts.append({"type": "image", "source": {"type": "base64", "media_type": mm.group(1), "data": mm.group(2)}})
        if conv and conv[-1]["role"] == role:
            conv[-1]["content"].extend(parts)
        else:
            conv.append({"role": role, "content": parts})
    if not conv or conv[0]["role"] != "user":
        conv.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start)"}]})
    system = "\n\n".join(system_parts)
    if json_schema is not None:
        system += "\n\nRespond with a single JSON object only (no prose, no code fences) matching this JSON schema:\n" + json.dumps(json_schema)
    body = {"model": p.model, "max_tokens": max_tokens, "messages": conv, "stream": stream, "temperature": temperature}
    if system:
        body["system"] = system
    return body


async def complete(p: Provider, messages: list[dict], *, max_tokens: int = 800, temperature: float = 0.4,
                   json_schema: dict | None = None, timeout: float = 120.0) -> str:
    if p.name == "ollama":
        # CPU-only fallback: long structured outputs (news digests, simulations) need minutes, not seconds.
        timeout = max(timeout, 360.0)
    client = http()
    if p.kind == "anthropic":
        r = await client.post(ANTHROPIC_URL, headers=_headers(p),
                              json=_anthropic_body(p, messages, max_tokens, temperature, json_schema, False),
                              timeout=httpx.Timeout(timeout, connect=6.0))
        if r.status_code >= 400:
            raise ProviderError(p.label, r.status_code, r.text)
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    r = await client.post(f"{p.base_url.rstrip('/')}/chat/completions", headers=_headers(p),
                          json=_openai_body(p, messages, max_tokens, temperature, json_schema, False),
                          timeout=httpx.Timeout(timeout, connect=4.0 if not p.online else 8.0))
    if r.status_code >= 400:
        raise ProviderError(p.label, r.status_code, r.text)
    data = r.json()
    try:
        return data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError) as e:
        raise ProviderError(p.label, 502, f"malformed response: {str(data)[:200]}") from e


async def stream(p: Provider, messages: list[dict], *, max_tokens: int = 1500, temperature: float = 0.5,
                 timeout: float = 180.0) -> AsyncIterator[str]:
    client = http()
    if p.kind == "anthropic":
        url, body = ANTHROPIC_URL, _anthropic_body(p, messages, max_tokens, temperature, None, True)
    else:
        url, body = f"{p.base_url.rstrip('/')}/chat/completions", _openai_body(p, messages, max_tokens, temperature, None, True)
    async with client.stream("POST", url, headers=_headers(p), json=body,
                             timeout=httpx.Timeout(timeout, connect=4.0 if not p.online else 8.0, read=90.0)) as r:
        if r.status_code >= 400:
            raw = await r.aread()
            raise ProviderError(p.label, r.status_code, raw.decode(errors="replace"))
        async for line in r.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if p.kind == "anthropic":
                t = j.get("type")
                if t == "content_block_delta" and j.get("delta", {}).get("type") == "text_delta":
                    yield j["delta"].get("text", "")
                elif t == "error":
                    raise ProviderError(p.label, 500, json.dumps(j.get("error")))
                elif t == "message_stop":
                    break
            else:
                if j.get("error"):
                    raise ProviderError(p.label, 500, json.dumps(j["error"]))
                choices = j.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta


async def list_models(base_url: str, api_key: str, timeout: float = 6.0) -> list[str]:
    r = await http().get(f"{base_url.rstrip('/')}/models",
                         headers={"Authorization": f"Bearer {api_key}"} if api_key else {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    names = [m.get("id") for m in data.get("data", []) if m.get("id")]
    for m in data.get("models", []) or []:
        for k in ("name", "model"):
            if m.get(k):
                names.append(m[k])
    for m in data.get("data", []):
        names.extend(m.get("aliases") or [])
    return sorted(set(names))


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_json_loose(text: str) -> dict | None:
    if not text:
        return None
    t = _FENCE.sub("", text.strip())
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"items": v}
    except ValueError:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start : i + 1])
                        if isinstance(v, dict):
                            return v
                    except ValueError:
                        break
        start = t.find("{", start + 1)
    return None
