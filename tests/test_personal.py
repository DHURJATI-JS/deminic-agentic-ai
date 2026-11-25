"""Personal section bug hunt (ported from the personal module's REST suite):
same-name task queue, reminders + 3-minute cancel window, calendar / Meet /
.ics, family contacts in global memory, e-mail drafts, media, summary job,
tenant isolation."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from helpers import Client


def iso(delta_s: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def _tasks(c: Client) -> dict:
    return {t["task_no"]: t for t in c.get("/api/personal/tasks?status=all").json()["tasks"]}


def test_same_name_queue_and_reminders(alice):
    r = alice.post("/api/personal/tasks", json={"title": "Eat dinner at 8pm", "due_at": iso(7200), "location_name": "Home"})
    t1 = r.json()["task"]
    assert t1["status"] == "active" and t1["reminder"]["can_cancel"] and t1["location_name"] == "Home"
    r = alice.post("/api/personal/tasks", json={"title": "eat dinner at 9pm", "due_at": iso(10800)})
    t2 = r.json()["task"]
    assert t2["status"] == "queued" and r.json()["queued_behind"] == t1["task_no"]
    t3 = alice.post("/api/personal/tasks", json={"title": "Eat dinner", "due_at": iso(14400)}).json()["task"]
    assert t3["status"] == "queued"
    q = alice.get("/api/personal/tasks/queue").json()["queue"]
    assert len(q[0]["waiting"]) == 2 and q[0]["active"]["task_no"] == t1["task_no"]
    act = alice.post(f"/api/personal/tasks/{t1['task_no']}/complete").json()["activated"]
    assert act["task_no"] == t2["task_no"] and act["status"] == "active" and act["reminder"]["status"] == "scheduled"
    assert _tasks(alice)[t3["task_no"]]["queued_behind_no"] == t2["task_no"]
    # a user-cancelled reminder is remembered (never rescheduled)
    assert alice.post(f"/api/jobs/{act['reminder']['request_id']}/cancel").json()["ok"] is True
    time.sleep(1.5)
    assert _tasks(alice)[t2["task_no"]]["reminder_cancelled"] is True


def test_reminder_cancel_window_and_firing(alice):
    tm = alice.post("/api/personal/tasks", json={"title": "Take medicine", "due_at": iso(120)}).json()["task"]
    assert tm["reminder"]["can_cancel"] is False
    r = alice.post(f"/api/jobs/{tm['reminder']['request_id']}/cancel").json()
    assert r["ok"] is False and "3 minutes" in r["message"]
    ts = alice.post("/api/personal/tasks", json={"title": "Stretch break", "due_at": iso(6)}).json()["task"]
    time.sleep(10)
    assert _tasks(alice)[ts["task_no"]]["reminded_at"] is not None


def test_calendar_meet_flow_and_ics(alice):
    ev = alice.post("/api/personal/events", json={"title": "Design sync", "starts_at": iso(86400), "wants_meet": True}).json()["event"]
    assert ev["needs_meet_link"] and "calendar.google.com" in ev["google_url"]
    ics = alice.get(ev["ics_url"])
    assert ics.status_code == 200 and "BEGIN:VEVENT" in ics.text and "text/calendar" in ics.headers["content-type"]
    assert alice.post(f"/api/personal/events/{ev['event_no']}/meet", json={"meet_link": "https://zoom.us/j/123"}).status_code == 422
    ev2 = alice.post(f"/api/personal/events/{ev['event_no']}/meet",
                     json={"meet_link": "https://meet.google.com/abc-defg-hij"}).json()["event"]
    assert ev2["meet_link"].endswith("abc-defg-hij") and "abc-defg-hij" in ev2["google_url"]


def test_family_contacts_live_in_global_memory_and_drafts(alice):
    c = alice.post("/api/personal/contacts", json={"name": "Rahul", "relation": "brother", "email": "rahul@example.com"}).json()["contact"]
    assert any(m["category"] == "contact" for m in alice.get("/api/memory?section=personal").json()["memories"])
    em = alice.post("/api/personal/emails", json={"contact_id": c["id"], "subject": "Hi", "body": "Hello bro"}).json()["email"]
    assert em["status"] == "draft" and em["to_email"] == "rahul@example.com"
    r = alice.post(f"/api/personal/emails/{em['email_no']}/send")
    assert r.status_code == 400 and "SMTP" in r.text  # no SMTP configured -> clear error, draft kept


def test_media_endpoints(alice):
    assert alice.get("/api/personal/media/radio?q=synthwave").json().get("stations")
    w = alice.get("/api/personal/media/watch?title=Dangal&year=2016").json()
    assert any(l["name"] == "Netflix" for l in w.get("links", [])) or w.get("providers")


def test_personal_isolation(alice, bob):
    t = alice.post("/api/personal/tasks", json={"title": "Secret errand", "due_at": iso(7200)}).json()["task"]
    ev = alice.post("/api/personal/events", json={"title": "Private", "starts_at": iso(86400)}).json()["event"]
    c = alice.post("/api/personal/contacts", json={"name": "Mom", "relation": "mother", "email": "mom@example.com"}).json()["contact"]
    bt = bob.get("/api/personal/tasks?status=all").json()
    assert bt["tasks"] == [] and bt["queue"] == []
    assert bob.post(f"/api/personal/tasks/{t['task_no']}/complete").status_code == 404
    assert bob.get(ev["ics_url"]).status_code == 404
    assert bob.get("/api/personal/contacts").json()["contacts"] == []
    assert bob.patch(f"/api/personal/contacts/{c['id']}", json={"name": "x"}).status_code == 404
    assert bob.delete(f"/api/personal/contacts/{c['id']}").status_code == 404
    assert bob.post(f"/api/jobs/{t['reminder']['request_id']}/cancel").json()["ok"] is False
    assert Client().get("/api/personal/tasks").status_code == 401
