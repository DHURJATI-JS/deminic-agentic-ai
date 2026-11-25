"""pgvector helpers - always scoped by user_id (+ section) and embed_model."""
from __future__ import annotations

import logging

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import HASH_MODEL, PRIMARY_MODEL, embed_many, embed_one, primary_available, threshold
from app.models import MemoryVector

log = logging.getLogger("deminic.vectors")


async def upsert(db: AsyncSession, user_id: int, section: str, source_type: str, source_id: int, content: str,
                 *, embed_text: str | None = None) -> None:
    """Store/replace the vector for one source row. ``content`` is what is shown
    on recall; ``embed_text`` (optional) is what gets embedded - used for
    sensitive memories whose value must not be stored in plain text."""
    content = (content or "").strip()[:4000]
    await db.execute(
        delete(MemoryVector).where(
            MemoryVector.user_id == user_id,
            MemoryVector.source_type == source_type,
            MemoryVector.source_id == source_id,
        )
    )
    if not content:
        return
    vec, model = await embed_one(embed_text or content)
    db.add(MemoryVector(user_id=user_id, section=section, source_type=source_type, source_id=source_id,
                        content=content, embedding=vec, embed_model=model))


async def remove(db: AsyncSession, user_id: int, source_type: str, source_id: int) -> None:
    await db.execute(
        delete(MemoryVector).where(
            MemoryVector.user_id == user_id,
            MemoryVector.source_type == source_type,
            MemoryVector.source_id == source_id,
        )
    )


async def search_vec(db: AsyncSession, user_id: int, section: str, vec: list[float], model: str, *,
                     k: int = 5, types: list[str] | None = None, kind: str = "recall",
                     min_score: float | None = None) -> list[dict]:
    dist = MemoryVector.embedding.cosine_distance(vec)
    stmt = (
        select(MemoryVector.content, MemoryVector.source_type, MemoryVector.source_id, dist.label("d"))
        .where(MemoryVector.user_id == user_id, MemoryVector.section == section, MemoryVector.embed_model == model)
        .order_by(dist)
        .limit(k)
    )
    if types:
        stmt = stmt.where(MemoryVector.source_type.in_(types))
    rows = (await db.execute(stmt)).all()
    floor = threshold(model, kind) if min_score is None else min_score
    out = []
    for content, st, sid, d in rows:
        score = 1.0 - float(d)
        if score >= floor:
            out.append({"content": content, "source_type": st, "source_id": sid, "score": round(score, 3)})
    return out


async def search(db: AsyncSession, user_id: int, section: str, query: str, k: int = 5,
                 types: list[str] | None = None, kind: str = "recall") -> list[dict]:
    vec, model = await embed_one(query)
    return await search_vec(db, user_id, section, vec, model, k=k, types=types, kind=kind)


async def reembed_hash_rows(db: AsyncSession, limit: int = 500) -> int:
    """Upgrade vectors written by the offline fallback once MiniLM is back."""
    if not primary_available():
        return 0
    rows = (
        await db.execute(select(MemoryVector.id, MemoryVector.content).where(MemoryVector.embed_model == HASH_MODEL).limit(limit))
    ).all()
    if not rows:
        return 0
    try:
        vecs, model = await embed_many([c for _, c in rows], model=PRIMARY_MODEL)
    except Exception:  # noqa: BLE001
        return 0
    for (rid, _), v in zip(rows, vecs):
        await db.execute(update(MemoryVector).where(MemoryVector.id == rid).values(embedding=v, embed_model=model))
    await db.commit()
    return len(rows)
