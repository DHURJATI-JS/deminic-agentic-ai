"""Text embeddings for pgvector (384-d).

Primary: ``all-minilm`` (sentence-transformers MiniLM-L6) served by the
dockerised Ollama - real semantic vectors used for every routing decision
(tools, memory, profile, summaries, prompt-injection) instead of regex.
Fallback: a deterministic signed feature-hashing embedder so the app keeps
working offline. Every stored vector records its ``embed_model`` and searches
only compare vectors from the same model; ``reembed_hash_rows`` upgrades
fallback rows once the primary model is reachable again.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections import OrderedDict

import httpx

from app.config import settings
from app.core.state import http

log = logging.getLogger("deminic.embed")

DIM = 384
HASH_MODEL = "hash-v1"
PRIMARY_MODEL = settings.embed_model

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set(
    "a an the and or but if then else of to in on at by for with about as is are was were be been being "
    "i me my mine we our you your he she it they them his her its their this that these those do does did "
    "have has had will would can could should shall may might must so very just not no yes from into over "
    "under up down out off than too also please hey hi hello ok okay".split()
)

_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_CACHE_MAX = 4096
_down_until = 0.0


def _stem(w: str) -> str:
    for suf in ("ingly", "edly", "ing", "ies", "ied", "ed", "es", "ly", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf in ("ies", "ied") else "")
    return w


def _h(feature: str) -> tuple[int, float]:
    d = hashlib.blake2b(feature.encode(), digest_size=8).digest()
    n = int.from_bytes(d, "little")
    return n % DIM, (1.0 if (n >> 63) & 1 else -1.0)


def hash_embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    words = [_stem(w) for w in _WORD.findall((text or "").lower()) if w not in _STOP]
    if not words:
        words = _WORD.findall((text or "").lower())[:8] or ["empty"]
    for w in words:
        i, s = _h("w:" + w)
        vec[i] += s
        padded = f"#{w}#"
        grams = [padded[k : k + 3] for k in range(len(padded) - 2)]
        if grams:
            wt = 0.6 / math.sqrt(len(grams))
            for g in grams:
                i, s = _h("c:" + g)
                vec[i] += s * wt
    for a, b in zip(words, words[1:]):
        i, s = _h(f"b:{a}_{b}")
        vec[i] += s * 0.7
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


async def _hash_many(texts: list[str]) -> list[list[float]]:
    """CPU-bound - keep big batches off the event loop."""
    if len(texts) <= 4 and sum(len(t) for t in texts) < 4000:
        return [hash_embed(t) for t in texts]
    import asyncio

    return await asyncio.to_thread(lambda: [hash_embed(t) for t in texts])


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


async def _primary(texts: list[str]) -> list[list[float]]:
    r = await http().post(
        f"{settings.ollama_url.rstrip('/')}/embeddings",
        json={"model": PRIMARY_MODEL, "input": texts},
        timeout=httpx.Timeout(20.0, connect=3.0),
    )
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
    vecs = [_norm(d["embedding"]) for d in data]
    if len(vecs) != len(texts) or any(len(v) != DIM for v in vecs):
        raise ValueError("unexpected embedding shape")
    return vecs


def primary_available() -> bool:
    return time.monotonic() >= _down_until


async def embed_many(texts: list[str], *, model: str | None = None) -> tuple[list[list[float]], str]:
    """Embed texts; returns (vectors, model_name). ``model`` forces a model."""
    global _down_until
    texts = [(t or "").strip()[:2000] or "empty" for t in texts]
    want = model or (PRIMARY_MODEL if primary_available() else HASH_MODEL)
    if want == HASH_MODEL:
        return await _hash_many(texts), HASH_MODEL
    out: list[list[float] | None] = [_cache.get((want, t)) for t in texts]
    missing = [i for i, v in enumerate(out) if v is None]
    if missing:
        try:
            got: list[list[float]] = []
            for start in range(0, len(missing), 64):
                batch = [texts[i] for i in missing[start : start + 64]]
                got += await _primary(batch)
        except Exception as e:  # noqa: BLE001 - failsafe to the offline embedder
            if model == PRIMARY_MODEL:
                raise
            _down_until = time.monotonic() + 30
            log.warning("embedding model unavailable (%s) - using offline hash embedder", type(e).__name__)
            return await _hash_many(texts), HASH_MODEL
        for i, v in zip(missing, got):
            out[i] = v
            _cache[(want, texts[i])] = v
            if len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)
    return [v for v in out if v is not None], want


async def embed_one(text: str, *, model: str | None = None) -> tuple[list[float], str]:
    vecs, m = await embed_many([text], model=model)
    return vecs[0], m


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# Per-model similarity thresholds (MiniLM cosine vs. hashing cosine differ).
THRESHOLDS = {
    PRIMARY_MODEL: {"tool": 0.34, "memory_category": 0.38, "memory": 0.30, "profile": 0.52, "photo": 0.55,
                    "summary": 0.42, "auto_memory": 0.45, "injection": 0.62, "recall": 0.28,
                    "memory_request": 0.45, "memory_claim": 0.68},
    HASH_MODEL: {"tool": 0.22, "memory_category": 0.20, "memory": 0.18, "profile": 0.40, "photo": 0.42,
                 "summary": 0.25, "auto_memory": 0.30, "injection": 0.50, "recall": 0.15,
                 "memory_request": 0.40, "memory_claim": 0.60},
}


def threshold(model: str, kind: str) -> float:
    return THRESHOLDS.get(model, THRESHOLDS[HASH_MODEL]).get(kind, 0.4)
