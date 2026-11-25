"""Test helpers: a small client that speaks the app's security protocol
(CSRF signed double-submit header + X-Idempotency-Key) against a live server."""
from __future__ import annotations

import os
import time
import uuid

import httpx

BASE = os.environ.get("DEMINIC_TEST_URL", "http://127.0.0.1:8700")
PASSWORD = "Passw0rdTest"


class Client:
    def __init__(self, base: str = BASE):
        self.http = httpx.Client(base_url=base, timeout=90, follow_redirects=False)
        self.http.get("/login")  # device + CSRF cookies
        self.user: dict = {}
        self.username = ""

    # ------------------------------------------------------------------ raw
    def headers(self, idem: str | None = None, csrf: bool = True) -> dict:
        h = {}
        if csrf:
            h["X-CSRF-Token"] = self.http.cookies.get("dm_csrf") or ""
        h["X-Idempotency-Key"] = idem or str(uuid.uuid4())
        return h

    def get(self, url: str, **kw) -> httpx.Response:
        return self.http.get(url, **kw)

    def post(self, url: str, json=None, data=None, files=None, idem: str | None = None, csrf: bool = True) -> httpx.Response:
        return self.http.post(url, json=json, data=data, files=files, headers=self.headers(idem, csrf))

    def put(self, url: str, json=None) -> httpx.Response:
        return self.http.put(url, json=json, headers=self.headers())

    def patch(self, url: str, json=None) -> httpx.Response:
        return self.http.patch(url, json=json, headers=self.headers())

    def delete(self, url: str) -> httpx.Response:
        return self.http.delete(url, headers=self.headers())

    # ------------------------------------------------------------------ flows
    def register(self, prefix: str = "t", **extra) -> "Client":
        self.username = f"{prefix}{uuid.uuid4().hex[:10]}"
        data = {"name": f"Tester {self.username}", "username": self.username, "email": f"{self.username}@example.com",
                "password": PASSWORD, "ai_name": "Deminic", "timezone": "Asia/Kolkata"}
        data.update({k: str(v) for k, v in extra.items()})
        r = self.post("/api/auth/register", data=data)
        assert r.status_code == 200, r.text
        self.user = r.json()["user"]
        return self

    def new_chat(self, section: str = "personal") -> int:
        r = self.post("/api/chats", json={"section": section})
        assert r.status_code == 200, r.text
        return r.json()["chat"]["chat_no"]

    def send(self, chat_no: int, text: str, timeout: float = 240) -> dict:
        rid = str(uuid.uuid4())
        r = self.post(f"/api/chats/{chat_no}/messages", json={"text": text, "request_id": rid}, idem=rid)
        assert r.status_code == 200, r.text
        msg_no = r.json()["msg_no"]
        return self.wait_reply(chat_no, msg_no, timeout)

    def wait_reply(self, chat_no: int, msg_no: int, timeout: float = 240) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.get(f"/api/chats/{chat_no}").json()
            m = next((x for x in d["messages"] if x["chatno"] == msg_no), None)
            if m and m["status"] != "generating":
                return m
            time.sleep(1.5)
        raise TimeoutError(f"no reply for chat#{chat_no}.{msg_no}")

    def wait_job(self, request_id: str, timeout: float = 120) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            jobs = self.get("/api/jobs").json()["jobs"]
            j = next((x for x in jobs if x["request_id"] == request_id), None)
            if j and j["status"] not in ("running", "scheduled", "queued"):
                return j
            time.sleep(1)
        raise TimeoutError(f"job {request_id} still running")
