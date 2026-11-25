"""Per-user terminal logging: every step is logged to the server console, kept
in a capped Redis list per user and streamed live to that user's terminal."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.core.state import redis

logger = logging.getLogger("deminic")
_MAX = 600


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "engineio", "socketio", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def ulog(uid: int | None, msg: str, *, level: str = "info", src: str = "app") -> None:
    msg = (msg or "").replace("\r", " ")[:2000]
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "level": level, "src": src, "msg": msg}
    getattr(logger, "warning" if level == "warn" else level if level in ("info", "error", "debug") else "info")(
        "[u%s] [%s] %s", uid if uid is not None else "-", src, msg
    )
    if uid is None:
        return
    try:
        key = f"logs:{uid}"
        await redis.lpush(key, json.dumps(line))
        await redis.ltrim(key, 0, _MAX - 1)
    except Exception:  # noqa: BLE001
        logger.debug("redis log push failed", exc_info=True)
    from app.core.sockets import emit_user

    await emit_user(uid, "log", line)


async def history(uid: int, n: int = 200) -> list[dict]:
    raw = await redis.lrange(f"logs:{uid}", 0, n - 1)
    out = []
    for r in reversed(raw):
        try:
            out.append(json.loads(r))
        except ValueError:
            continue
    return out


async def clear(uid: int) -> None:
    await redis.delete(f"logs:{uid}")
