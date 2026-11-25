"""Socket.IO server.

* Connections are authenticated with the HttpOnly session cookie AND the CSRF
  token (sent in the ``auth`` payload) so a cross-site page cannot open a
  socket as the user. Origin is restricted to same-origin by python-engineio.
* Every user joins only their private room ``u:<id>``; all server pushes are
  emitted to that room, so events never cross accounts.
* ``job:cancel`` cancels exactly the background task whose request id is given,
  after verifying it belongs to the caller.
"""
from __future__ import annotations

import logging
import re
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import socketio

from app.security.sessions import SESSION_COOKIE, load_session

log = logging.getLogger("deminic.sockets")

def _same_host_origin(origin, environ=None) -> bool:  # noqa: ANN001
    """Same-origin check that works for http/https AND the ws/wss upgrade:
    the browser's Origin host:port must equal the Host header. (engineio's
    built-in check compares schemes and rejects every browser WebSocket.)"""
    if not origin:
        return True  # non-browser client - the session cookie + CSRF token are still required
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    host = ((environ or {}).get("HTTP_HOST") or "").lower()
    return parts.scheme in ("http", "https") and bool(host) and parts.netloc.lower() == host


sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_same_host_origin,  # same host only (any device on the LAN, http or https)
    logger=False,
    engineio_logger=False,
    ping_interval=20,
    ping_timeout=25,
    max_http_buffer_size=1_000_000,
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def room(uid: int) -> str:
    return f"u:{uid}"


async def emit_user(uid: int, event: str, data: dict) -> None:
    try:
        await sio.emit(event, data, room=room(uid))
    except Exception:  # noqa: BLE001 - never let a push failure break a job
        log.debug("emit failed", exc_info=True)


async def kick_other_sessions(uid: int, keep_th: str) -> int:
    """One login per account: disconnect sockets that belong to older sessions."""
    kicked = 0
    try:
        participants = list(sio.manager.get_participants("/", room(uid)))
    except Exception:  # noqa: BLE001
        return 0
    for p in participants:
        sid = p[0] if isinstance(p, (tuple, list)) else p
        try:
            sess = await sio.get_session(sid)
        except KeyError:
            continue
        if sess.get("th") != keep_th:
            try:
                await sio.emit("session:replaced", {"message": "Signed out - your account signed in on another device."}, to=sid)
                await sio.disconnect(sid)
                kicked += 1
            except Exception:  # noqa: BLE001
                log.debug("kick failed", exc_info=True)
    return kicked


async def _uid_for(sid: str) -> int | None:
    try:
        sess = await sio.get_session(sid)
    except KeyError:
        return None
    uid = sess.get("uid")
    th = sess.get("th")
    # re-validate that the HTTP session still exists (logout kills sockets too)
    from app.core.state import redis

    if not th or not await redis.exists(f"sess:{th}"):
        return None
    return uid


@sio.event
async def connect(sid, environ, auth):  # noqa: ANN001
    from app.security.middleware import CSRF_COOKIE, csrf_valid

    cookie = SimpleCookie()
    try:
        cookie.load(environ.get("HTTP_COOKIE", ""))
    except Exception:  # noqa: BLE001
        raise ConnectionRefusedError("bad cookies")
    token = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None
    loaded = await load_session(token)
    if not loaded:
        raise ConnectionRefusedError("unauthorized")
    data, th = loaded
    from app.security.sessions import login_token_valid

    if data.get("replaced") or not await login_token_valid(int(data["uid"]), th):  # one login per account
        raise ConnectionRefusedError("session_replaced")
    csrf = (auth or {}).get("csrf") if isinstance(auth, dict) else None
    cookie_csrf = cookie[CSRF_COOKIE].value if CSRF_COOKIE in cookie else ""
    if not csrf or csrf != cookie_csrf or not csrf_valid(csrf, f"s:{th}"):
        raise ConnectionRefusedError("csrf")
    uid = int(data["uid"])
    await sio.save_session(sid, {"uid": uid, "th": th})
    await sio.enter_room(sid, room(uid))
    return True


@sio.event
async def disconnect(sid, reason=None):  # noqa: ANN001
    return None


@sio.on("job:cancel")
async def on_job_cancel(sid, data):  # noqa: ANN001
    from app.core.jobs import jobs
    from app.core.logs import ulog

    uid = await _uid_for(sid)
    if not uid:
        return {"ok": False, "message": "unauthorized"}
    request_id = str((data or {}).get("request_id", "")).lower()
    if not _UUID.match(request_id):
        return {"ok": False, "message": "invalid request id", "request_id": request_id}
    ok, message = await jobs.cancel(uid, request_id, reason="cancelled by user")
    await ulog(uid, f"cancel request for {request_id[:8]}: {message}", level="info" if ok else "warn", src="jobs")
    return {"ok": ok, "message": message, "request_id": request_id}


@sio.on("terminal:history")
async def on_terminal_history(sid, data=None):  # noqa: ANN001
    from app.core.logs import history

    uid = await _uid_for(sid)
    if not uid:
        return {"ok": False, "lines": []}
    return {"ok": True, "lines": await history(uid, 300)}


@sio.on("status:ping")
async def on_status_ping(sid, data=None):  # noqa: ANN001
    """Round-trip probe for the status dialog - the client measures the live
    connection's latency from this acknowledgement."""
    t = data.get("t") if isinstance(data, dict) else None
    return {"ok": bool(await _uid_for(sid)), "t": t if isinstance(t, (int, float)) else None}


@sio.on("chat:buffer")
async def on_chat_buffer(sid, data):  # noqa: ANN001
    """Return the partial reply of a still-running generation (when the user
    switches back to a chat while it is generating in the background)."""
    from app.core.jobs import jobs

    uid = await _uid_for(sid)
    if not uid:
        return {"ok": False}
    request_id = str((data or {}).get("request_id", "")).lower()
    h = jobs.get(uid, request_id)
    if not h:
        return {"ok": False}
    return {"ok": True, "text": h.meta.get("partial", ""), "status": h.status}
