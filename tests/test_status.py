"""System status dialog API (live pings, public subset when signed out, the
end-to-end AI response test, the Socket.IO round trip) and the immersive /
VR showcase that every visitor sees on the home page."""
from __future__ import annotations

import socketio

from helpers import BASE, Client


def test_ping_public_subset_when_signed_out(anon):
    r = anon.get("/api/status/ping")
    assert r.status_code == 200
    d = r.json()
    keys = {c["key"] for c in d["components"]}
    assert d["signed_in"] is False
    assert {"gateway", "coder", "fallback", "embeddings", "postgres", "redis", "clamav"} <= keys
    # nothing account-specific: no jobs row, no online providers, no gateway address
    assert "jobs" not in keys and not any(k.startswith("online") for k in keys)
    gw = next(c for c in d["components"] if c["key"] == "gateway")
    assert "192.168." not in gw["detail"] and ":8000" not in gw["detail"]
    assert d["overall"] in ("ok", "degraded", "down") and d["headline"]


def test_ping_signed_in_deep_reports_every_component(alice):
    d = alice.get("/api/status/ping?deep=1").json()
    keys = {c["key"] for c in d["components"]}
    assert d["signed_in"] is True and {"jobs", "online"} <= keys  # no online keys yet -> one 'off' row
    pg = next(c for c in d["components"] if c["key"] == "postgres")
    assert pg["state"] == "ok" and pg["detail"].startswith("pgvector")
    for c in d["components"]:
        assert c["state"] in ("ok", "info", "warn", "down", "off"), c
        assert c["ms"] is None or c["ms"] >= 0
    assert d["chat_route"], "some model must be able to answer chats"


def test_ai_test_requires_sign_in():
    anon = Client()
    anon.get("/")  # picks up the CSRF cookie, so only the missing sign-in can reject it
    assert anon.post("/api/status/ai-test", json={"tier": "chat"}).status_code == 401


def test_ai_test_gets_a_real_answer(alice):
    r = alice.post("/api/status/ai-test", json={"tier": "chat"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["correct"], d
    assert 0 <= d["ttft_ms"] <= d["total_ms"] and d["provider"]
    coding = alice.post("/api/status/ai-test", json={"tier": "coding"}).json()
    assert coding["ok"] or coding["attempts"], coding
    assert alice.post("/api/status/ai-test", json={"tier": "bogus"}).status_code == 422


def test_socket_status_ping_round_trip(alice):
    s = socketio.Client(reconnection=False)
    cookie = "; ".join(f"{k}={v}" for k, v in alice.http.cookies.items())
    s.connect(BASE, headers={"Cookie": cookie}, auth={"csrf": alice.http.cookies.get("dm_csrf")},
              transports=["websocket"], wait_timeout=10)
    try:
        assert s.call("status:ping", {"t": 123}, timeout=5) == {"ok": True, "t": 123}
    finally:
        s.disconnect()


def test_home_shows_vr_mode_to_everyone(anon, alice):
    for client in (anon, alice):
        html = client.get("/").text
        assert 'id="immersive"' in html and "data-immersive-toggle" in html and "data-status-open" in html
    for name in ("immersive-night", "immersive-day", "immersive-quest"):
        r = anon.get(f"/static/img/showcase/{name}.webp")
        assert r.status_code == 200 and r.headers["content-type"] == "image/webp", name
    # signed-out visitors can switch immersive mode on for their device
    assert anon.put("/api/device-prefs", json={"immersive": True}).status_code == 200
    assert 'data-immersive="1"' in anon.get("/").text
