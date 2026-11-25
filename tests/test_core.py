"""Core functional bug hunt: chats, AI replies through the gateway, history
compaction + encryption, memory (vector search), protected wipe job, UI state,
business-description versioning, background-task rules."""
from __future__ import annotations

import asyncio
import uuid

from helpers import Client


def test_pages_render(anon):
    for p in ("/", "/login", "/register", "/forgot", "/locust", "/disclaimer", "/technical", "/healthz"):
        assert anon.get(p).status_code == 200, p
    tech = anon.get("/technical").text
    assert 'aria-current="page"' in tech and "How it works" in tech and 'id="architecture"' in tech
    assert anon.get("/app").status_code == 303
    assert anon.get("/nope-not-here").status_code == 404


def test_signed_in_user_can_reach_home_without_sign_in_or_sign_up(alice):
    home = alice.get("/")
    assert home.status_code == 200
    # home page offers the way back into the app, never sign-in / sign-up
    assert 'href="/app"' in home.text
    assert 'href="/login"' not in home.text and 'href="/register"' not in home.text
    # the sign-in / sign-up screens send a signed-in user to the app
    for p in ("/login", "/register"):
        r = alice.get(p)
        assert r.status_code == 303 and r.headers["location"] == "/app", p
    # the app and settings link back to the home page
    import re

    for p in ("/app", "/settings"):
        assert re.search(r'<a href="/"[\s>]', alice.get(p).text), p


def test_chat_crud(alice):
    no = alice.new_chat("personal")
    assert alice.patch(f"/api/chats/{no}", json={"title": "Renamed", "lawyer_mode": True}).json()["chat"]["lawyer_mode"]
    chats = alice.get("/api/chats?section=personal").json()["chats"]
    assert chats[0]["title"] == "Renamed" and chats[0]["ref"] == f"chat#{no}"
    assert alice.get(f"/api/refs/resolve?ref=chat%23{no}").json()["section"] == "personal"
    assert alice.delete(f"/api/chats/{no}").status_code == 200
    assert alice.get(f"/api/chats/{no}").status_code == 404


def test_ai_reply_and_compaction_encrypted(alice):
    no = alice.new_chat("personal")
    m1 = alice.send(no, "My favourite colour is teal. Reply with one short sentence.")
    assert m1["status"] == "done" and m1["reply"], m1
    m2 = alice.send(no, "What did I just tell you my favourite colour is?")
    assert "teal" in m2["reply"].lower(), m2["reply"]
    assert m2["ref"] == f"chat#{no}.2"

    async def raw():
        from sqlalchemy import text

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from app.config import settings

        engine = create_async_engine(settings.database_url, poolclass=NullPool)

        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT h.messages::text, h.compacted_history, c.title FROM chat_histories h JOIN chats c ON c.id=h.chat_id "
                "JOIN users u ON u.id=c.user_id WHERE u.username=:u AND c.chat_no=:n"), {"u": alice.username, "n": no})).one()
        await engine.dispose()
        return row

    messages_raw, compacted_raw, title_raw = asyncio.run(raw())
    assert "teal" not in messages_raw.lower() and '"chatno": 1' in messages_raw  # JSONB list of dicts, content encrypted
    assert compacted_raw.startswith("enc:") and title_raw.startswith("enc:")


def test_memory_semantic_search_and_crud(alice):
    for k, v, c in (("peanut allergy", "severe, carries an EpiPen", "health"), ("monthly salary", "95,000 INR", "finance"),
                    ("favourite cuisine", "south indian", "preference")):
        assert alice.post("/api/memory", json={"section": "personal", "category": c, "key": k, "value": v}).status_code == 200
    items = alice.get("/api/memory?section=personal").json()["memories"]
    assert {i["category"] for i in items} == {"health", "finance", "preference"}
    assert all(i["sensitive"] for i in items if i["category"] in ("health", "finance"))
    hits = alice.get("/api/memory/search?section=personal&q=what food can I not eat").json()["results"]
    assert hits and hits[0]["key"] == "peanut allergy", hits
    # sections are separate memories
    assert alice.get("/api/memory?section=business").json()["memories"] == []
    assert alice.delete(f"/api/memory/{items[0]['id']}?section=personal").status_code == 200


def test_memory_wipe_is_protected_and_completes(alice):
    alice.post("/api/memory", json={"section": "business", "category": "work", "key": "biggest client", "value": "Acme"})
    r = alice.post("/api/memory/wipe", json={"section": "business"})
    rid = r.json()["request_id"]
    assert r.json()["restore_point"]
    assert alice.post(f"/api/jobs/{rid}/cancel").json()["ok"] is False  # non-cancellable
    dup = alice.post("/api/memory/wipe", json={"section": "business"})
    assert dup.status_code in (200, 409)
    job = alice.wait_job(rid)
    assert job["status"] == "completed", job
    assert alice.get("/api/memory?section=business").json()["memories"] == []
    rps = alice.get("/api/restore-points").json()["restore_points"]
    assert any(p["status"] == "committed" for p in rps)


def test_ui_state_in_redis_and_theme(alice):
    alice.put("/api/ui-state", json={"section": "coding", "tab_coding": "editor", "evil": "x"})
    st = alice.get("/api/ui-state").json()
    assert st["section"] == "coding" and "evil" not in st
    assert alice.put("/api/theme", json={"theme": "dark"}).json()["theme"] == "dark"
    assert 'data-theme="dark"' in alice.get("/app").text


def test_business_description_change_flow(alice):
    alice.put("/api/settings/business", json={"owns_business": True, "business_name": "Acme Bakes",
                                              "business_description": "We bake cakes", "confirm_new_business": True})
    no = alice.new_chat("business")
    # renaming alone needs no confirmation and keeps chats valid
    assert alice.put("/api/settings/business", json={"business_name": "Acme Bakery"}).status_code == 200
    assert alice.get(f"/api/chats/{no}").json()["chat"]["outdated"] is False
    r = alice.put("/api/settings/business", json={"business_description": "We now build drones"})
    assert r.status_code == 409 and r.json()["code"] == "confirm_new_business"
    r = alice.put("/api/settings/business", json={"business_description": "We now build drones", "confirm_new_business": True})
    assert r.status_code == 200 and r.json()["outdated_chats"] >= 1
    assert alice.get(f"/api/chats/{no}").json()["chat"]["outdated"] is True
    rid = str(uuid.uuid4())
    blocked = alice.post(f"/api/chats/{no}/messages", json={"text": "hello", "request_id": rid}, idem=rid)
    assert blocked.status_code == 409


def test_one_generation_per_chat(alice):
    no = alice.new_chat("personal")
    r1 = alice.post(f"/api/chats/{no}/messages", json={"text": "Write two paragraphs about the ocean."})
    r2 = alice.post(f"/api/chats/{no}/messages", json={"text": "And another one."})
    assert r1.status_code == 200 and r2.status_code == 409
    alice.wait_reply(no, r1.json()["msg_no"])


def test_parallel_generation_across_chats(alice):
    a, b = alice.new_chat("personal"), alice.new_chat("personal")
    ra = alice.post(f"/api/chats/{a}/messages", json={"text": "Name three planets."})
    rb = alice.post(f"/api/chats/{b}/messages", json={"text": "Name three rivers."})
    assert ra.status_code == rb.status_code == 200
    running = [j for j in alice.get("/api/jobs").json()["jobs"] if j["kind"] == "generation"]
    assert len(running) >= 2
    assert alice.wait_reply(a, ra.json()["msg_no"])["status"] == "done"
    assert alice.wait_reply(b, rb.json()["msg_no"])["status"] == "done"


def test_terminal_logs_every_step(alice):
    no = alice.new_chat("personal")
    alice.send(no, "Say hi in three words.")
    lines = [l["msg"] for l in alice.get("/api/terminal").json()["lines"]]
    joined = "\n".join(lines)
    for needle in ("message received", "embedded", "memory search", "tool search", "reply saved", "background task started"):
        assert needle in joined, needle


def test_settings_masks_secrets(alice):
    alice.put("/api/settings/providers", json={"claude_key": "sk-ant-test-" + "x" * 30})
    s = alice.get("/api/settings").json()
    assert "sk-ant" not in str(s) and s["providers"]["claude_key"].endswith("xxxx")
    alice.put("/api/settings/providers", json={"claude_key": "__clear__"})
    assert alice.get("/api/settings").json()["providers"]["claude_key"] == ""


def test_register_with_avatar_and_profile_photo_access():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 132, 255)).save(buf, "PNG")
    c = Client()
    c.username = "photo" + uuid.uuid4().hex[:8]
    r = c.post("/api/auth/register", data={"name": "Photo Person", "username": c.username, "email": f"{c.username}@example.com",
                                           "password": "Passw0rdTest"}, files={"avatar": ("me.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    url = r.json()["user"]["avatar_url"]
    assert url and url.endswith(".webp")
    assert c.get(url).headers["content-type"] == "image/webp"
    no = c.new_chat("personal")
    m = c.send(no, "What is my name?")
    assert "photo person" in m["reply"].lower() or "photo" in m["reply"].lower(), m["reply"]
