"""Website reading: SSRF-safe fetch, readable-text extraction and semantic
selection of the chunks most relevant to the user's question."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.core.embeddings import cosine, embed_many
from app.security.ssrf import UnsafeURL, safe_get

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+", re.I)


def find_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    u = m.group(0).rstrip(".,;:!?")
    return u if u.startswith("http") else "https://" + u


async def fetch_page(url: str) -> dict:
    try:
        resp = await safe_get(url, max_bytes=2_500_000, timeout=20,
                              headers={"Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"})
    except UnsafeURL as e:
        return {"ok": False, "error": str(e), "url": url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}", "url": url}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"HTTP {resp.status_code}", "url": str(resp.url)}
    ctype = resp.headers.get("content-type", "")
    raw = resp.content.decode(resp.encoding or "utf-8", errors="replace")
    if "html" not in ctype and "<html" not in raw[:2000].lower():
        text = raw
        title = url
    else:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "header", "aside"]):
            tag.decompose()
        title = (soup.title.string or "").strip() if soup.title and soup.title.string else url
        main = soup.find("main") or soup.find("article") or soup.body or soup
        text = main.get_text("\n")
    text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", text)).strip()
    return {"ok": True, "url": str(resp.url), "title": title[:200], "text": text[:60000]}


async def relevant_chunks(text: str, question: str, max_chars: int = 4500) -> str:
    paras = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 40]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) < 700:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)
            buf = p[:1400]
    if buf:
        chunks.append(buf)
    if not chunks:
        return text[:max_chars]
    chunks = chunks[:120]
    vecs, _ = await embed_many([question or "main content summary"] + [c[:800] for c in chunks])
    q, cv = vecs[0], vecs[1:]
    ranked = sorted(range(len(chunks)), key=lambda i: cosine(q, cv[i]), reverse=True)
    picked, total = [], 0
    for i in ranked:
        if total + len(chunks[i]) > max_chars:
            continue
        picked.append(i)
        total += len(chunks[i])
    return "\n\n".join(chunks[i] for i in sorted(picked))
