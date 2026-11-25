"""N+1 audit: the number of SQL statements per request must not grow with the
number of rows returned. Runs the app in-process (ASGI) and counts statements."""
from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import event

from app.db import engine

COUNT = {"n": 0}


def _count(*_a, **_k):
    COUNT["n"] += 1


@pytest.fixture(scope="module")
def counting():
    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    yield
    event.remove(engine.sync_engine, "before_cursor_execute", _count)


async def _client():
    from app.main import app

    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    await c.get("/login")
    u = "np" + uuid.uuid4().hex[:10]
    h = {"X-CSRF-Token": c.cookies.get("dm_csrf"), "X-Idempotency-Key": str(uuid.uuid4())}
    r = await c.post("/api/auth/register", data={"name": "N Plus", "username": u, "email": f"{u}@example.com",
                                                 "password": "Passw0rdNp1"}, headers=h)
    assert r.status_code == 200, r.text
    return c


async def _post(c, url, body):
    return await c.post(url, json=body, headers={"X-CSRF-Token": c.cookies.get("dm_csrf"), "X-Idempotency-Key": str(uuid.uuid4())})


async def _queries_for(c, url) -> int:
    COUNT["n"] = 0
    r = await c.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:200])
    return COUNT["n"]


@pytest.mark.parametrize("section", ["personal", "business"])
async def test_chat_and_memory_lists_constant_queries(counting, section):
    c = await _client()
    for i in range(3):
        await _post(c, "/api/chats", {"section": section})
        await _post(c, "/api/memory", {"section": section, "category": "general", "key": f"k{i}", "value": "v"})
    small = {u: await _queries_for(c, u) for u in (f"/api/chats?section={section}", f"/api/memory?section={section}",
                                                   "/api/jobs", f"/api/points?section={section}")}
    for i in range(3, 30):
        await _post(c, "/api/chats", {"section": section})
        await _post(c, "/api/memory", {"section": section, "category": "general", "key": f"k{i}", "value": "v"})
    from app.core.memory import memory_cache

    memory_cache._data.clear()  # noqa: SLF001 - measure cold-cache cost too
    big = {u: await _queries_for(c, u) for u in small}
    for u in small:
        assert big[u] <= small[u] + 1, f"{u}: {small[u]} queries with 3 rows vs {big[u]} with 30 rows (N+1?)"
    await c.aclose()


async def test_section_lists_constant_queries(counting):
    """Section modules: clients (+reminder counts), reminders (+client names), statements, tasks, contacts."""
    from datetime import datetime, timedelta, timezone

    c = await _client()
    due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

    async def add(n0: int, n1: int):
        for i in range(n0, n1):
            r = await _post(c, "/api/business/clients", {"name": f"Client {i}", "reason": "audit"})
            no = r.json()["client"]["client_no"]
            await _post(c, "/api/business/reminders", {"client_no": no, "amount": "10", "due_at": due})
            await _post(c, "/api/business/transactions", {"kind": "credit", "amount": "5", "counterparty": f"C{i}"})
            await _post(c, "/api/personal/tasks", {"title": f"Unique errand {i}", "due_at": due})
            await _post(c, "/api/personal/contacts", {"name": f"Friend {i}", "relation": "friend", "email": f"f{i}@example.com"})

    urls = ("/api/business/clients", "/api/business/reminders?status=all", "/api/business/transactions",
            "/api/personal/tasks?status=all", "/api/personal/contacts")
    await add(0, 3)
    small = {u: await _queries_for(c, u) for u in urls}
    await add(3, 15)
    from app.core.memory import memory_cache

    memory_cache._data.clear()  # noqa: SLF001
    big = {u: await _queries_for(c, u) for u in urls}
    for u in urls:
        assert big[u] <= small[u] + 2, f"{u}: {small[u]} queries with 3 rows vs {big[u]} with 15 rows (N+1?)"
    await c.aclose()
