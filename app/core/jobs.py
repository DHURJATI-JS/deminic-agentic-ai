"""Background job manager with AI-failsafe protection and restore points.

Lifecycle rules
---------------
* Every job has a request id (UUID) and an owner. Cancels are only honoured
  for the owner and only for the exact request id (sent over Socket.IO).
* Double-submit protection: jobs may declare a ``dedupe_key``; a second job
  with the same key while the first is running is rejected (DuplicateJob).
* Failsafe: failures are retried with exponential backoff (AI calls inside a
  job additionally fail over gateway -> docker fallback -> online provider).
  Only as a LAST RESORT is a cancellable job cancelled.
* Non-cancellable jobs open a restore point before they start; if they fail,
  are interrupted, or the app closes, they are rolled back to that timestamp.
* Cancellable jobs may carry a ``cancel_deadline`` (e.g. reminders can only be
  cancelled until 3 minutes before the task).
* On shutdown: cancellable jobs are cancelled, non-cancellable ones reverted.
  On startup: anything left "running" by a crash is cancelled / reverted.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, OperationalError

from app.core import restore
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.db import SessionLocal
from app.models import BackgroundJob, RestorePoint

log = logging.getLogger("deminic.jobs")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class DuplicateJob(Exception):
    def __init__(self, existing_id: str):
        super().__init__(f"duplicate job (already running: {existing_id})")
        self.existing_id = existing_id


class TransientJobError(Exception):
    """Raise inside a job to request a failsafe retry."""


def is_transient(e: BaseException) -> bool:
    from app.ai.failsafe import AIUnavailable

    return isinstance(
        e,
        (
            TransientJobError,
            AIUnavailable,
            httpx.TransportError,
            httpx.TimeoutException,
            asyncio.TimeoutError,
            ConnectionError,
            OperationalError,
        ),
    ) or (isinstance(e, DBAPIError) and bool(getattr(e, "connection_invalidated", False)))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobHandle:
    id: str
    user_id: int
    kind: str
    title: str
    cancellable: bool = True
    cancel_deadline: datetime | None = None
    dedupe_key: str | None = None
    status: str = "running"
    progress: float = 0.0
    detail: str = ""
    meta: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    attempts: int = 0
    retries: int = 2
    timeout_s: float | None = None
    start_at: datetime | None = None
    task: asyncio.Task | None = None
    restore_session: restore.RestoreSession | None = None
    cancel_reason: str = ""
    last_progress_at: float = field(default_factory=time.monotonic)

    def public(self) -> dict:
        now = utcnow()
        can_cancel = self.cancellable and self.status in ("running", "queued", "scheduled")
        reason = ""
        if not self.cancellable:
            reason = "non-cancellable (protected by a restore point)"
        elif self.cancel_deadline and now >= self.cancel_deadline:
            can_cancel = False
            reason = "cancel window closed (less than 3 minutes before the task)"
        return {
            "request_id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "progress": round(self.progress, 3),
            "detail": self.detail,
            "cancellable": self.cancellable,
            "can_cancel": can_cancel,
            "cancel_reason": reason,
            "cancel_deadline": self.cancel_deadline.isoformat() if self.cancel_deadline else None,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "attempts": self.attempts,
            "meta": {k: v for k, v in self.meta.items() if k not in ("partial",) and not k.startswith("_")},
        }


class JobContext:
    def __init__(self, handle: JobHandle, manager: "JobManager"):
        self.handle = handle
        self.manager = manager

    @property
    def id(self) -> str:
        return self.handle.id

    @property
    def user_id(self) -> int:
        return self.handle.user_id

    @property
    def restore(self) -> restore.RestoreSession | None:
        return self.handle.restore_session

    async def progress(self, pct: float, detail: str = "") -> None:
        self.handle.progress = max(0.0, min(1.0, pct))
        if detail:
            self.handle.detail = detail
        self.handle.last_progress_at = time.monotonic()
        await self.manager.emit(self.handle)

    async def log(self, msg: str, level: str = "info") -> None:
        self.handle.last_progress_at = time.monotonic()
        await ulog(self.handle.user_id, f"[{self.handle.kind}:{self.handle.id[:8]}] {msg}", level=level, src="jobs")

    def heartbeat(self) -> None:
        self.handle.last_progress_at = time.monotonic()


JobFn = Callable[[JobContext], Awaitable[Any]]


class JobManager:
    KEEP_FINISHED_S = 1800

    def __init__(self) -> None:
        self._jobs: dict[str, JobHandle] = {}
        self._dedupe: dict[str, str] = {}
        self._watchdog: asyncio.Task | None = None
        self.shutting_down = False

    # ------------------------------------------------------------------ query
    def get(self, user_id: int, job_id: str) -> JobHandle | None:
        h = self._jobs.get((job_id or "").lower())
        if h is None or h.user_id != user_id:
            return None
        return h

    def running_for(self, user_id: int, kind: str | None = None) -> list[JobHandle]:
        return [
            h
            for h in self._jobs.values()
            if h.user_id == user_id and h.status in ("running", "scheduled") and (kind is None or h.kind == kind)
        ]

    def list_for(self, user_id: int) -> list[dict]:
        hs = [h for h in self._jobs.values() if h.user_id == user_id]
        hs.sort(key=lambda h: h.created_at, reverse=True)
        return [h.public() for h in hs]

    def dedupe_owner(self, key: str) -> str | None:
        jid = self._dedupe.get(key)
        if jid and jid in self._jobs and self._jobs[jid].status in ("running", "scheduled"):
            return jid
        return None

    # ------------------------------------------------------------------ spawn
    async def spawn(
        self,
        user_id: int,
        kind: str,
        title: str,
        fn: JobFn,
        *,
        cancellable: bool = True,
        cancel_deadline: datetime | None = None,
        dedupe_key: str | None = None,
        meta: dict | None = None,
        job_id: str | None = None,
        retries: int = 2,
        timeout_s: float | None = None,
        start_at: datetime | None = None,
        restore_label: str | None = None,
    ) -> JobHandle:
        if self.shutting_down:
            raise RuntimeError("server is shutting down")
        if dedupe_key:
            owner = self.dedupe_owner(dedupe_key)
            if owner:
                raise DuplicateJob(owner)
        jid = (job_id or "").lower()
        if not _UUID.match(jid) or jid in self._jobs:
            jid = str(uuid.uuid4())
        h = JobHandle(
            id=jid,
            user_id=user_id,
            kind=kind,
            title=title[:200],
            cancellable=cancellable,
            cancel_deadline=cancel_deadline,
            dedupe_key=dedupe_key,
            meta=dict(meta or {}),
            retries=retries,
            timeout_s=timeout_s,
            start_at=start_at,
            status="scheduled" if start_at and start_at > utcnow() else "running",
        )
        if not cancellable:
            h.restore_session = await restore.create(user_id, jid, restore_label or f"before: {title}")
        self._jobs[jid] = h
        if dedupe_key:
            self._dedupe[dedupe_key] = jid
        async with SessionLocal() as s:
            s.add(
                BackgroundJob(
                    id=jid,
                    user_id=user_id,
                    kind=kind,
                    title=h.title,
                    status=h.status,
                    cancellable=cancellable,
                    cancel_deadline=cancel_deadline,
                    restore_point_id=h.restore_session.id if h.restore_session else None,
                    meta={k: v for k, v in h.meta.items() if not k.startswith("_") and k != "partial"},
                )
            )
            await s.commit()
        h.task = asyncio.create_task(self._run(h, fn), name=f"job:{kind}:{jid}")
        await ulog(
            user_id,
            f"background task started: {kind} • {h.title} • id={jid[:8]}"
            + (" • non-cancellable (restore point created)" if not cancellable else ""),
            src="jobs",
        )
        await self.emit(h)
        return h

    # ------------------------------------------------------------------ run
    async def _run(self, h: JobHandle, fn: JobFn) -> None:
        ctx = JobContext(h, self)
        try:
            if h.start_at:
                delay = (h.start_at - utcnow()).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                h.status = "running"
                await self.emit(h)
            while True:
                h.attempts += 1
                try:
                    result = await fn(ctx)
                    if isinstance(result, dict):
                        h.meta.update({k: v for k, v in result.items() if k != "partial"})
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    if is_transient(e) and h.attempts <= h.retries and not self.shutting_down:
                        delay = min(2 ** h.attempts, 20)
                        h.detail = f"failsafe retry {h.attempts}/{h.retries} in {delay}s: {type(e).__name__}"
                        await ctx.log(f"failsafe: attempt {h.attempts} failed ({type(e).__name__}: {e}); retrying in {delay}s", "warn")
                        await self.emit(h)
                        await asyncio.sleep(delay)
                        continue
                    raise
            h.status = "completed"
            h.progress = 1.0
            if h.restore_session:
                await restore.commit(h.restore_session.id)
            await ctx.log("completed")
        except asyncio.CancelledError:
            if h.cancellable:
                h.status = "cancelled"
                h.detail = h.cancel_reason or "cancelled"
                if h.restore_session:
                    ok, msg = await restore.revert(h.restore_session.id)
                    h.detail += f" • {msg}"
                await ctx.log(f"cancelled ({h.detail})", "warn")
            else:
                ok, msg = await restore.revert(h.restore_session.id) if h.restore_session else (False, "no restore point")
                h.status = "reverted"
                h.detail = f"interrupted ({h.cancel_reason or 'shutdown'}) • {msg}"
                await ctx.log(h.detail, "warn")
        except Exception as e:  # noqa: BLE001 - last resort
            log.exception("job %s failed", h.id)
            if h.cancellable:
                if h.restore_session:
                    await restore.revert(h.restore_session.id)
                h.status = "cancelled"
                h.detail = f"cancelled as a last resort after {h.attempts} attempt(s): {type(e).__name__}: {str(e)[:300]}"
            else:
                ok, msg = await restore.revert(h.restore_session.id) if h.restore_session else (False, "no restore point")
                h.status = "reverted"
                h.detail = f"failed ({type(e).__name__}: {str(e)[:200]}) • {msg}"
            await ctx.log(h.detail, "error")
        finally:
            h.finished_at = utcnow()
            if h.dedupe_key and self._dedupe.get(h.dedupe_key) == h.id:
                self._dedupe.pop(h.dedupe_key, None)
            try:
                await self._persist_final(h)
            except Exception:  # noqa: BLE001
                log.exception("persist job failed")
            await self.emit(h)

    async def _persist_final(self, h: JobHandle) -> None:
        async with SessionLocal() as s:
            await s.execute(
                update(BackgroundJob)
                .where(BackgroundJob.id == h.id)
                .values(
                    status=h.status,
                    progress=h.progress,
                    detail=h.detail[:2000],
                    attempts=h.attempts,
                    finished_at=h.finished_at,
                    meta={k: v for k, v in h.meta.items() if not k.startswith("_") and k != "partial"},
                )
            )
            await s.commit()

    async def emit(self, h: JobHandle) -> None:
        await emit_user(h.user_id, "job", h.public())

    # ------------------------------------------------------------------ cancel
    async def cancel(self, user_id: int, job_id: str, reason: str = "cancelled by user") -> tuple[bool, str]:
        h = self.get(user_id, job_id)
        if h is None:
            return False, "no such background task"
        if h.status not in ("running", "scheduled"):
            return False, f"task already {h.status}"
        if not h.cancellable:
            return False, "this task is non-cancellable (it is protected by a restore point instead)"
        if h.cancel_deadline and utcnow() >= h.cancel_deadline:
            return False, "too late - tasks can only be cancelled up to 3 minutes before they are due"
        h.cancel_reason = reason
        if h.task:
            h.task.cancel()
        return True, "cancelled"

    # ------------------------------------------------------------------ lifecycle
    def start_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watchdog_loop(), name="jobs:watchdog")

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = utcnow()
            mono = time.monotonic()
            for h in list(self._jobs.values()):
                if h.status == "running" and h.timeout_s and h.task and not h.task.done():
                    age = (now - h.created_at).total_seconds()
                    stalled = mono - h.last_progress_at
                    if age > h.timeout_s and stalled > 60:
                        h.cancel_reason = f"watchdog: exceeded {int(h.timeout_s)}s and stalled (last resort)"
                        await ulog(h.user_id, f"failsafe watchdog stopping {h.kind}:{h.id[:8]} - {h.cancel_reason}", level="warn", src="jobs")
                        h.task.cancel()
                if h.finished_at and (now - h.finished_at).total_seconds() > self.KEEP_FINISHED_S:
                    self._jobs.pop(h.id, None)

    async def shutdown(self) -> None:
        self.shutting_down = True
        if self._watchdog:
            self._watchdog.cancel()
        running = [h for h in self._jobs.values() if h.task and not h.task.done()]
        for h in running:
            h.cancel_reason = "app closed"
            h.task.cancel()  # type: ignore[union-attr]
        if running:
            await asyncio.wait([h.task for h in running if h.task], timeout=20)
        log.info("job manager stopped (%d task(s) cancelled/reverted)", len(running))

    async def recover_on_startup(self) -> int:
        """Handle jobs left running by a crash / hard close."""
        n = 0
        async with SessionLocal() as s:
            rows = (
                await s.execute(select(BackgroundJob).where(BackgroundJob.status.in_(("running", "queued", "scheduled"))))
            ).scalars().all()
            items = [(r.id, r.user_id, r.cancellable, r.restore_point_id, r.kind) for r in rows]
        for jid, uid, cancellable, rp_id, kind in items:
            n += 1
            if rp_id:
                ok, msg = await restore.revert(rp_id)
                status, detail = "reverted", f"app was closed during this task • {msg}"
            elif cancellable:
                status, detail = "cancelled", "cancelled because the app was closed"
            else:
                status, detail = "failed", "interrupted by app shutdown"
            async with SessionLocal() as s:
                await s.execute(
                    update(BackgroundJob)
                    .where(BackgroundJob.id == jid)
                    .values(status=status, detail=detail, finished_at=utcnow())
                )
                await s.commit()
            await ulog(uid, f"recovery: {kind}:{jid[:8]} -> {status} ({detail})", level="warn", src="jobs")
        # orphaned active restore points (crash before job row was written)
        async with SessionLocal() as s:
            orphans = (
                await s.execute(select(RestorePoint.id).where(RestorePoint.status == "active"))
            ).scalars().all()
        for rp_id in orphans:
            await restore.revert(rp_id)
        return n


jobs = JobManager()


def deadline_before(due_at: datetime | None, minutes: int = 3) -> datetime | None:
    return due_at - timedelta(minutes=minutes) if due_at else None
