"""Global memory (per user, per section).

Retrieval is **pgvector semantic search**: every memory entry has a vector in
``memory_vectors`` (source_type="memory"); the user's message is embedded once
and the nearest memories above a similarity threshold are injected. Whether
memory is needed *at all* - and which categories - is decided by nearest
neighbour search over category prototypes in ``intent_vectors`` (no regex).
The in-process dictionary gives O(1) access to (decrypted) values by id or by
(category, key) once the vector search has picked the entries.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import intents, vectors
from app.models import Memory
from app.security.crypto import decrypt


class MemoryCache:
    """{(user_id, section): {"by_cat": {category: {key: entry}}, "by_id": {id: entry}}}"""

    def __init__(self) -> None:
        self._data: dict[tuple[int, str], dict] = {}

    def invalidate(self, user_id: int, section: str | None = None) -> None:
        for k in [k for k in self._data if k[0] == user_id and (section is None or k[1] == section)]:
            self._data.pop(k, None)

    async def load(self, db: AsyncSession, user_id: int, section: str) -> dict:
        key = (user_id, section)
        if key in self._data:
            return self._data[key]
        rows = (
            await db.execute(
                select(Memory).where(Memory.user_id == user_id, Memory.section == section, Memory.deleted_at.is_(None))
            )
        ).scalars().all()
        by_cat: dict[str, dict[str, dict]] = defaultdict(dict)
        by_id: dict[int, dict] = {}
        for m in rows:
            entry = {
                "id": m.id,
                "category": m.category,
                "key": m.key,
                "value": decrypt(m.value) if m.is_sensitive else m.value,
                "data": dict(m.data or {}),
                "sensitive": m.is_sensitive,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            by_cat[m.category][m.key] = entry
            by_id[m.id] = entry
        self._data[key] = {"by_cat": dict(by_cat), "by_id": by_id}
        return self._data[key]

    async def get(self, db: AsyncSession, user_id: int, section: str, category: str, key: str) -> dict | None:
        return (await self.load(db, user_id, section))["by_cat"].get(category, {}).get(key)  # O(1)

    async def by_id(self, db: AsyncSession, user_id: int, section: str, mem_id: int) -> dict | None:
        return (await self.load(db, user_id, section))["by_id"].get(int(mem_id))  # O(1)

    async def category(self, db: AsyncSession, user_id: int, section: str, category: str) -> dict[str, dict]:
        return (await self.load(db, user_id, section))["by_cat"].get(category, {})

    async def known_emails(self, db: AsyncSession, user_id: int) -> set[str]:
        out: set[str] = set()
        for section in ("personal", "business", "coding"):
            for entry in (await self.load(db, user_id, section))["by_cat"].get("contact", {}).values():
                e = (entry.get("data") or {}).get("email")
                if e:
                    out.add(e.lower())
        return out


memory_cache = MemoryCache()


def describe(entry: dict) -> str:
    """One line used both for prompts and for the vector 'content'."""
    if entry.get("category") == "contact":
        d = entry.get("data") or {}
        bits = [f"relation={d.get('relation') or '-'}", f"email={d.get('email') or 'n/a'}", f"phone={d.get('phone') or 'n/a'}"]
        if d.get("birthday"):
            bits.append(f"birthday={d['birthday']}")
        if d.get("notes"):
            bits.append(f"notes={d['notes']}")
        return f"contact '{d.get('name') or entry.get('key')}': " + ", ".join(bits)
    tag = " (sensitive)" if entry.get("sensitive") else ""
    return f"[{entry.get('category')}{tag}] {entry.get('key')}: {entry.get('value')}"


async def memory_context(db: AsyncSession, user_id: int, section: str, vec: list[float], model: str,
                         max_chars: int = 1800) -> tuple[str, list[str], int]:
    """Semantic memory retrieval. Returns (prompt block, matched categories, hits)."""
    cats = [n for n, _ in await intents.matches(db, vec, model, "memory_category")]
    hits = await vectors.search_vec(db, user_id, section, vec, model, k=8, types=["memory"], kind="memory")
    if not cats and not hits:
        return "", [], 0
    table = await memory_cache.load(db, user_id, section)
    lines: list[str] = []
    seen: set[int] = set()
    for h in hits:  # nearest memories first
        entry = table["by_id"].get(int(h["source_id"]))
        # standing instructions are always in the prompt already (their own block) - don't repeat them here
        if entry and entry["id"] not in seen and entry["category"] != "instruction":
            seen.add(entry["id"])
            lines.append("- " + describe(entry))
    for c in cats:  # whole category the message is about (O(1) dict access)
        for entry in table["by_cat"].get(c, {}).values():
            if entry["id"] not in seen and len(lines) < 25:
                seen.add(entry["id"])
                lines.append("- " + describe(entry))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n- ..."
    return text, cats, len(hits)
