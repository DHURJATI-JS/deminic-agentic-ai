"""Restore points - Windows-restore-point style rollback for non-cancellable work.

A job opens a restore point *before* it mutates anything and records an undo
operation (persisted immediately) before every destructive step. If the job
fails, is interrupted by the app closing, or the process crashes, the undo log
is replayed in reverse, returning the data to the restore point's timestamp.
"""
from __future__ import annotations

import base64
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import update

from app.db import SessionLocal
from app.models import (
    CalendarEvent,
    Chat,
    ChatHistory,
    Client,
    Email,
    Memory,
    MemoryVector,
    Reminder,
    RestorePoint,
    Summary,
    Task,
    Transaction,
)

log = logging.getLogger("deminic.restore")

MODELS = {
    "tasks": Task,
    "reminders": Reminder,
    "calendar_events": CalendarEvent,
    "memories": Memory,
    "summaries": Summary,
    "clients": Client,
    "emails": Email,
    "transactions": Transaction,
    "chats": Chat,
    "chat_histories": ChatHistory,
}

# Extension point: other modules can register custom undo handlers.
Reverter = Callable[[int, dict], Awaitable[None]]
REVERTERS: dict[str, Reverter] = {}


def register_reverter(name: str, fn: Reverter) -> None:
    REVERTERS[name] = fn


class RestoreSession:
    def __init__(self, rp_id: str, user_id: int, created_at: datetime):
        self.id = rp_id
        self.user_id = user_id
        self.created_at = created_at

    async def record(self, op: dict) -> None:
        """Persist an undo operation BEFORE performing the matching change."""
        async with SessionLocal() as s:
            rp = await s.get(RestorePoint, self.id)
            if rp is None:
                return
            rp.ops.append(op)
            await s.commit()


async def create(user_id: int, job_id: str | None, label: str) -> RestoreSession:
    async with SessionLocal() as s:
        rp = RestorePoint(user_id=user_id, job_id=job_id, label=label[:200], status="active", ops=[])
        s.add(rp)
        await s.commit()
        await s.refresh(rp)
        return RestoreSession(rp.id, user_id, rp.created_at)


async def commit(rp_id: str) -> None:
    async with SessionLocal() as s:
        await s.execute(update(RestorePoint).where(RestorePoint.id == rp_id).values(status="committed"))
        await s.commit()


async def _apply(user_id: int, op: dict) -> None:
    t = op.get("type")
    async with SessionLocal() as s:
        if t == "undelete_memories":
            ids = [int(i) for i in op.get("ids", [])]
            if ids:
                await s.execute(
                    update(Memory)
                    .where(Memory.user_id == user_id, Memory.id.in_(ids))
                    .values(deleted_at=None, deleted_by_job=None)
                )
        elif t == "restore_rows":
            model = MODELS[op["model"]]
            for row in op.get("rows", []):
                fields = {k: v for k, v in row.get("fields", {}).items() if k not in ("id", "user_id")}
                if fields:
                    await s.execute(
                        update(model).where(model.user_id == user_id, model.id == int(row["id"])).values(**fields)
                    )
        elif t == "delete_rows":
            model = MODELS[op["model"]]
            ids = [int(i) for i in op.get("ids", [])]
            if ids:
                from sqlalchemy import delete

                await s.execute(delete(model).where(model.user_id == user_id, model.id.in_(ids)))
        elif t == "reinsert_vectors":
            from app.core.embeddings import embed_many

            rows = op.get("rows", [])
            if rows:
                vecs, model = await embed_many([r.get("embed_text") or r["content"] for r in rows])
                for r, v in zip(rows, vecs):
                    s.add(
                        MemoryVector(
                            user_id=user_id,
                            section=r["section"],
                            source_type=r["source_type"],
                            source_id=int(r["source_id"]),
                            content=r["content"],
                            embedding=v,
                            embed_model=model,
                        )
                    )
        elif t == "restore_file":
            from app.services.workspace import user_root

            root = user_root(user_id)
            target = (root / op["path"]).resolve()
            if root.resolve() not in target.parents and target != root.resolve():
                raise ValueError("restore path escapes workspace")
            if op.get("existed"):
                target.parent.mkdir(parents=True, exist_ok=True)
                if op.get("b64"):
                    target.write_bytes(base64.b64decode(op.get("content") or ""))
                else:
                    target.write_text(op.get("content") or "", encoding="utf-8", newline="")
            elif target.exists() and target.is_file():
                target.unlink()
        elif t == "restore_dir":
            src, dst = Path(op["from"]), Path(op["to"])
            if src.exists():
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        elif t in REVERTERS:
            await REVERTERS[t](user_id, op)
        else:
            log.warning("unknown restore op %s", t)
        await s.commit()


async def revert(rp_id: str) -> tuple[bool, str]:
    async with SessionLocal() as s:
        rp = await s.get(RestorePoint, rp_id)
        if rp is None:
            return False, "restore point not found"
        if rp.status in ("restored", "committed"):
            return True, f"restore point already {rp.status}"
        ops = list(rp.ops or [])
        user_id = rp.user_id
        ts = rp.created_at
    errors = []
    for op in reversed(ops):
        try:
            await _apply(user_id, op)
        except Exception as e:  # noqa: BLE001 - keep reverting the rest
            log.exception("restore op failed")
            errors.append(f"{op.get('type')}: {e}")
    async with SessionLocal() as s:
        await s.execute(
            update(RestorePoint)
            .where(RestorePoint.id == rp_id)
            .values(status="restore_failed" if errors else "restored", restored_at=datetime.now(timezone.utc))
        )
        await s.commit()
    when = ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "?"
    if errors:
        return False, f"partially restored to {when}: " + "; ".join(errors)[:500]
    return True, f"rolled back to restore point {when} ({len(ops)} change(s) undone)"
