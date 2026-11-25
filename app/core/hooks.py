"""Startup / shutdown / maintenance hooks that feature modules can register
(e.g. re-scheduling task reminders, rebuilding the task queue, daily news)."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("deminic.hooks")

Hook = Callable[[], Awaitable[None]]
_startup: list[tuple[str, Hook]] = []
_shutdown: list[tuple[str, Hook]] = []
_maintenance: list[tuple[str, Hook]] = []


def on_startup(name: str):
    def deco(fn: Hook) -> Hook:
        _startup.append((name, fn))
        return fn

    return deco


def on_shutdown(name: str):
    def deco(fn: Hook) -> Hook:
        _shutdown.append((name, fn))
        return fn

    return deco


def every_maintenance_cycle(name: str):
    """Runs roughly once a minute."""

    def deco(fn: Hook) -> Hook:
        _maintenance.append((name, fn))
        return fn

    return deco


async def _run(hooks: list[tuple[str, Hook]], phase: str) -> None:
    for name, fn in hooks:
        try:
            await fn()
        except Exception:  # noqa: BLE001
            log.exception("%s hook %s failed", phase, name)


async def run_startup() -> None:
    await _run(_startup, "startup")


async def run_shutdown() -> None:
    await _run(_shutdown, "shutdown")


async def run_maintenance() -> None:
    await _run(_maintenance, "maintenance")
