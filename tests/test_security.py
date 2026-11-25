"""Security bug hunt: headers/CSP nonce, CSRF (signed double-submit), idempotency
(double-submit guard), auth hardening, ClamAV, SSRF guard, secret redaction."""
from __future__ import annotations

import re
import threading
import uuid

import httpx
import pytest

from helpers import BASE, PASSWORD, Client


def test_security_headers_and_csp_nonce(anon):
    r = anon.get("/")
    csp = r.headers["content-security-policy"]
    m = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
    assert m, csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "microphone=(self)" in r.headers["permissions-policy"]
    # every inline/script tag carries THIS response's nonce
    for tag in re.findall(r"<script[^>]*>", r.text):
        assert f'nonce="{m.group(1)}"' in tag, tag
    assert anon.get("/").headers["content-security-policy"] != csp  # fresh nonce per request


def test_csrf_required_and_bound_to_session(alice, bob):
    assert alice.http.post("/api/chats", json={"section": "personal"}).status_code == 403
    r = alice.http.post("/api/chats", json={"section": "personal"}, headers={"X-CSRF-Token": "forged.token"})
    assert r.status_code == 403
    # Bob's valid token does not work for Alice's session (HMAC binding)
    r = alice.http.post("/api/chats", json={"section": "personal"}, headers={"X-CSRF-Token": bob.http.cookies.get("dm_csrf")})
    assert r.status_code == 403
    assert alice.post("/api/chats", json={"section": "personal"}).status_code == 200


def test_idempotency_replays_duplicates(alice):
    key = str(uuid.uuid4())
    r1 = alice.post("/api/chats", json={"section": "business"}, idem=key)
    r2 = alice.post("/api/chats", json={"section": "business"}, idem=key)
    assert r1.status_code == r2.status_code == 200
    assert r2.headers.get("idempotent-replay") == "true"
    assert r1.json()["chat"]["chat_no"] == r2.json()["chat"]["chat_no"]
    chats = alice.get("/api/chats?section=business").json()["chats"]
    assert len(chats) == 1


def test_concurrent_double_submit_creates_one(alice):
    key = str(uuid.uuid4())
    results = []

    def fire():
        c = httpx.Client(base_url=BASE, cookies=alice.http.cookies, timeout=30)
        results.append(c.post("/api/chats", json={"section": "coding"}, headers=alice.headers(key)))

    ts = [threading.Thread(target=fire) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(r.status_code in (200, 409) for r in results), [r.status_code for r in results]
    assert len(alice.get("/api/chats?section=coding").json()["chats"]) == 1


def test_login_rules(anon):
    c = Client().register("login")
    bad = anon.post("/api/auth/login", json={"login": c.username, "password": "wrong-Passw0rd"})
    unknown = anon.post("/api/auth/login", json={"login": "nobody-" + uuid.uuid4().hex, "password": "wrong-Passw0rd"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.json()["detail"] == unknown.json()["detail"]  # no user enumeration
    ok = Client().post("/api/auth/login", json={"login": c.username.upper(), "password": PASSWORD})
    assert ok.status_code == 200


def test_weak_password_and_duplicate_username(anon):
    r = anon.post("/api/auth/register", data={"name": "X", "username": "weak" + uuid.uuid4().hex[:6],
                                              "email": f"{uuid.uuid4().hex[:8]}@example.com", "password": "short"})
    assert r.status_code == 422
    c = Client().register("dupe")
    r = Client().post("/api/auth/register", data={"name": "Y", "username": c.username, "email": f"{uuid.uuid4().hex[:8]}@example.com",
                                                  "password": PASSWORD})
    assert r.status_code == 409


def test_login_bruteforce_throttled_per_account():
    victim = Client().register("victim")
    attacker = Client()
    codes = [attacker.post("/api/auth/login", json={"login": victim.username, "password": "Wrong-pass1"}).status_code
             for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[-1] == 429, codes
    # even the right password is refused while the account is throttled
    assert attacker.post("/api/auth/login", json={"login": victim.username, "password": PASSWORD}).status_code == 429


def test_clamav_rejects_eicar_avatar(alice):
    # assembled at runtime so the test file itself is not flagged on disk
    eicar = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-" + "TEST-FILE!$H+H*").encode()
    r = alice.post("/api/settings/avatar", files={"avatar": ("x.png", eicar, "image/png")})
    assert r.status_code == 400 and "clamav" in r.text.lower(), r.text


def test_avatar_rejects_non_image(alice):
    r = alice.post("/api/settings/avatar", files={"avatar": ("x.png", b"definitely not an image" * 20, "image/png")})
    assert r.status_code == 400


def test_unauthenticated_api_is_401(anon):
    for url in ("/api/me", "/api/chats?section=personal", "/api/jobs", "/api/memory?section=personal", "/api/settings"):
        assert anon.get(url).status_code == 401, url


@pytest.mark.parametrize("url,bad", [("http://127.0.0.1:8700/", True), ("http://169.254.169.254/latest", True),
                                     ("http://10.0.0.1/", True), ("file:///etc/passwd", True), ("https://example.com", False)])
async def test_ssrf_guard(url, bad):
    from app.security.ssrf import UnsafeURL, check_url

    if bad:
        with pytest.raises(UnsafeURL):
            await check_url(url)
    else:
        assert await check_url(url)


def test_secret_redaction():
    from app.security.sentinel import redact_secrets

    text, n = redact_secrets("key sk-ant-api03-" + "a" * 40 + " and ghp_" + "b" * 36)
    assert n == 2 and "sk-ant" not in text and "ghp_" not in text
