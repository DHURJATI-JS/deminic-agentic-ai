"""One login per account: the login token stored in the DB is checked by the
middleware on every request; a newer login invalidates every older session
(HTTP + live sockets)."""
from __future__ import annotations

import time

import socketio

from helpers import BASE, PASSWORD, Client


def _socket(c: Client):
    events: list = []
    s = socketio.Client(reconnection=False)
    s.on("session:replaced", lambda data=None: events.append(("session:replaced", data)))
    cookie = "; ".join(f"{k}={v}" for k, v in c.http.cookies.items())
    s.connect(BASE, headers={"Cookie": cookie}, auth={"csrf": c.http.cookies.get("dm_csrf")},
              transports=["websocket"], wait_timeout=10)
    return s, events


def test_newer_login_replaces_older_session_everywhere():
    first = Client().register("single")
    assert first.get("/api/me").status_code == 200
    sock, events = _socket(first)
    assert sock.connected

    second = Client()
    r = second.post("/api/auth/login", json={"login": first.username, "password": PASSWORD})
    assert r.status_code == 200
    time.sleep(1.0)

    # The server clears the stale cookie on its first rejection, so each path is checked
    # with its own copy of the old cookie (as two open tabs of the old device would be).
    old_cookies = dict(first.http.cookies)
    page_tab = Client()
    for k, v in old_cookies.items():
        page_tab.http.cookies.set(k, v)
    # old HTTP session is rejected by the middleware token checker
    r = first.get("/api/me")
    assert r.status_code == 401 and r.json().get("code") == "session_replaced", r.text
    # pages bounce to the sign-in screen with the reason
    r = page_tab.get("/app")
    assert r.status_code == 303 and "reason=elsewhere" in r.headers["location"], r.headers.get("location")
    first.http.cookies.set("dm_session", old_cookies["dm_session"])
    # old live socket was told and disconnected
    assert ("session:replaced" in [e for e, _ in events]) or not sock.connected
    sock.disconnect()
    # the old cookie can no longer open a socket
    try:
        s2, _ = _socket(first)
        reconnected = s2.connected
        s2.disconnect()
    except socketio.exceptions.ConnectionError:
        reconnected = False
    assert not reconnected
    # the newest login works
    assert second.get("/api/me").status_code == 200

    # logging out clears the stored token: that session is dead, a fresh login works again
    assert second.post("/api/auth/logout").status_code == 200
    assert second.get("/api/me").status_code == 401
    third = Client()
    assert third.post("/api/auth/login", json={"login": first.username, "password": PASSWORD}).status_code == 200
    assert third.get("/api/me").status_code == 200


def test_db_stores_only_a_hash_of_the_login_token():
    import asyncio

    c = Client().register("tokhash")
    raw_cookie = c.http.cookies.get("dm_session")

    async def stored():
        from sqlalchemy import text

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from app.config import settings

        engine = create_async_engine(settings.database_url, poolclass=NullPool)

        async with engine.connect() as conn:
            v = (await conn.execute(text("SELECT session_token_hash FROM users WHERE username=:u"),
                                    {"u": c.username})).scalar_one()
        await engine.dispose()
        return v

    h = asyncio.run(stored())
    import hashlib

    assert h == hashlib.sha256(raw_cookie.encode()).hexdigest() and raw_cookie not in h
