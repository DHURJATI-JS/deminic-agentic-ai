"""Locust website stress testing as cancellable background jobs.

Each run: generated locustfile -> ``python -m locust --headless`` subprocess ->
live stats from the CSV file (terminal + ``locust:stats`` socket events) ->
final stats stored on the LocustRun row. Cancelling kills the process tree.
Several runs can execute in parallel (each is its own job/subprocess).
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import ipaddress
import os
import shutil
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, update

from app.config import settings
from app.core.jobs import JobContext
from app.core.logs import ulog
from app.core.sockets import emit_user
from app.db import SessionLocal
from app.models import LocustRun
from app.services.locust_target_template import render

MAX_USERS = 200
MAX_DURATION = 600
MAX_RATE = 50.0


class StressError(ValueError):
    pass


def run_dir(user_id: int, run_id: int) -> Path:
    d = settings.data_dir / "locust_runs" / f"u{int(user_id)}" / str(int(run_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate(target_url: str, users: int, spawn_rate: float, duration: int, paths: list[str]) -> tuple[str, list[str]]:
    url = (target_url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise StressError("Target must be an http(s) URL, e.g. https://example.com")
    if parts.username or parts.password:
        raise StressError("Credentials in the URL are not allowed")
    host = parts.hostname
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise StressError("That address cannot be stress-tested")
    if host in ("metadata.google.internal",):
        raise StressError("That address cannot be stress-tested")
    if not (1 <= users <= MAX_USERS):
        raise StressError(f"Users must be between 1 and {MAX_USERS}")
    if not (0.1 <= spawn_rate <= MAX_RATE):
        raise StressError(f"Spawn rate must be between 0.1 and {MAX_RATE}/s")
    if not (5 <= duration <= MAX_DURATION):
        raise StressError(f"Duration must be between 5 and {MAX_DURATION} seconds")
    clean = []
    for p in paths or ["/"]:
        p = str(p).strip()
        if not p:
            continue
        if not p.startswith("/") or len(p) > 300 or any(c in p for c in "\r\n\t ") or "://" in p:
            raise StressError(f"Invalid path {p[:40]!r} - paths must start with / and contain no spaces")
        clean.append(p)
    if not clean:
        clean = ["/"]
    if len(clean) > 25:
        raise StressError("At most 25 paths")
    return f"{parts.scheme}://{parts.netloc}", clean


def _num(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_stats(csv_path: Path) -> dict:
    if not csv_path.exists():
        return {}
    rows = []
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return {}
    endpoints, totals = [], {}
    for r in rows:
        item = {
            "type": r.get("Type", ""), "name": r.get("Name", ""),
            "requests": int(_num(r.get("Request Count"))), "failures": int(_num(r.get("Failure Count"))),
            "median_ms": round(_num(r.get("Median Response Time")), 1), "avg_ms": round(_num(r.get("Average Response Time")), 1),
            "min_ms": round(_num(r.get("Min Response Time")), 1), "max_ms": round(_num(r.get("Max Response Time")), 1),
            "rps": round(_num(r.get("Requests/s")), 2), "fail_rps": round(_num(r.get("Failures/s")), 2),
            "p95_ms": round(_num(r.get("95%")), 1), "p99_ms": round(_num(r.get("99%")), 1),
        }
        if item["name"] == "Aggregated":
            totals = item
        else:
            endpoints.append(item)
    if totals:
        totals["failure_pct"] = round(100 * totals["failures"] / totals["requests"], 2) if totals["requests"] else 0.0
    return {"totals": totals, "endpoints": endpoints}


def parse_failures(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            return [{"method": r.get("Method"), "name": r.get("Name"), "error": (r.get("Error") or "")[:300],
                     "occurrences": int(_num(r.get("Occurrences")))} for r in csv.DictReader(f)][:30]
    except (OSError, csv.Error):
        return []


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
                                                          stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(killer.wait(), timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), timeout=10)


async def run_job(ctx: JobContext, run_id: int, target: str, users: int, spawn_rate: float, duration: int,
                  paths: list[str], method: str = "GET") -> dict:
    uid = ctx.user_id
    d = run_dir(uid, run_id)
    (d / "locustfile.py").write_text(render(run_id, paths, method), encoding="utf-8")
    stats_csv = d / "run_stats.csv"
    cmd = [sys.executable, "-m", "locust", "-f", str(d / "locustfile.py"), "--headless", "-u", str(users),
           "-r", str(spawn_rate), "-t", f"{duration}s", "--host", target, "--csv", str(d / "run"),
           "--only-summary", "--stop-timeout", "3", "--loglevel", "WARNING", "--exit-code-on-error", "0"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    await ctx.log(f"locust run #{run_id}: {users} users @ {spawn_rate}/s for {duration}s against {target} ({len(paths)} path(s))")
    kwargs = {"start_new_session": True} if sys.platform != "win32" else {}
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(d), env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, **kwargs)
    started = asyncio.get_running_loop().time()

    async def pump():
        assert proc.stdout is not None
        n = 0
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line and n < 400:
                n += 1
                await ulog(uid, f"[locust #{run_id}] {line[:300]}", src="locust")

    pump_task = asyncio.create_task(pump())
    status = "completed"
    try:
        while proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            elapsed = asyncio.get_running_loop().time() - started
            live = parse_stats(stats_csv)
            tot = live.get("totals") or {}
            await ctx.progress(min(elapsed / max(duration, 1), 0.99),
                               f"{tot.get('requests', 0)} req · {tot.get('rps', 0)} req/s · "
                               f"{tot.get('failures', 0)} failures · median {tot.get('median_ms', 0)} ms")
            await emit_user(uid, "locust:stats", {"run_id": run_id, "request_id": ctx.id, "elapsed": round(elapsed, 1),
                                                  "duration": duration, **live})
            if elapsed > duration + 90:
                await ctx.log("locust did not stop in time - killing it", "warn")
                await _kill_tree(proc)
    except asyncio.CancelledError:
        status = "cancelled"
        await _kill_tree(proc)
        raise
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(pump_task, timeout=5)
        final = parse_stats(stats_csv)
        final["failures_detail"] = parse_failures(d / "run_failures.csv")
        final["exit_code"] = proc.returncode
        if status != "cancelled" and proc.returncode not in (0, None) and not final.get("totals"):
            status = "failed"
        async with SessionLocal() as db:
            await db.execute(update(LocustRun).where(LocustRun.id == run_id, LocustRun.user_id == uid)
                             .values(status=status, stats=final, finished_at=datetime.now(timezone.utc)))
            await db.commit()
        await emit_user(uid, "locust:stats", {"run_id": run_id, "request_id": ctx.id, "done": True, "status": status, **final})
        tot = final.get("totals") or {}
        await ulog(uid, f"locust run #{run_id} {status}: {tot.get('requests', 0)} requests, {tot.get('failure_pct', 0)}% failed, "
                        f"median {tot.get('median_ms', 0)} ms, p95 {tot.get('p95_ms', 0)} ms, {tot.get('rps', 0)} req/s", src="locust")
        with contextlib.suppress(OSError):
            for leftover in d.glob("*_stats_history.csv"):
                leftover.unlink()
    if status == "failed":
        await ctx.progress(1.0, f"locust failed (exit code {proc.returncode}) - see the terminal output")
    return {"run_id": run_id, "status": status, "requests": (final.get("totals") or {}).get("requests", 0)}


async def runs_for(user_id: int, limit: int = 30) -> list[LocustRun]:
    async with SessionLocal() as db:
        return list((await db.execute(select(LocustRun).where(LocustRun.user_id == user_id)
                                      .order_by(LocustRun.created_at.desc()).limit(limit))).scalars().all())


def cleanup_run_dir(user_id: int, run_id: int) -> None:
    shutil.rmtree(settings.data_dir / "locust_runs" / f"u{int(user_id)}" / str(int(run_id)), ignore_errors=True)
