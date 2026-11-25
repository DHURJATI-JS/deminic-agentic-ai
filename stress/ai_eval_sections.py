"""Section-level AI evaluation cases (personal / business / coding tools).
Imported by ai_eval.py; each case drives real chats and checks the outcome."""
from __future__ import annotations

import json

from ai_eval import Ctx, case, cards, has


def card_like(m: dict, *subs: str) -> list[dict]:
    return [c for c in cards(m) if any(s in (c.get("type") or "") for s in subs)]


def blob(m: dict) -> str:
    return (json.dumps(m.get("cards") or []) + " " + (m.get("reply") or "")).lower()


# ------------------------------------------------------------------ personal
@case("creates a task with time + location", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "Remind me to eat dinner at 8pm today", fresh=True)
    return bool(card_like(m, "task")) and any(r.startswith("task#") for r in m.get("refs") or []), str(m.get("refs"))


@case("same-name task waits in the processing queue", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "Also remind me to eat dinner at 9pm today")
    return "queue" in blob(m), m["reply"][:160]


@case("marks a task done when told", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "I finished eating dinner, mark the dinner task as done")
    b = blob(m)
    return ("done" in b or "complete" in b) and bool(card_like(m, "task")), m["reply"][:160]


@case("asks the user to create a Google Meet link", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "Schedule a Google Meet with Rahul tomorrow at 5pm", fresh=True)
    return "meet.google.com" in blob(m) and bool(card_like(m, "event", "meet")), m["reply"][:200]


@case("adds the pasted Meet link to the calendar event", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "Here is the meet link https://meet.google.com/abc-defg-hij")
    b = blob(m)
    return "abc-defg-hij" in b and "calendar.google.com" in b, m["reply"][:200]


@case("creates a family member profile in global memory", "personal", "personal")
def _(x: Ctx):
    x.ask("personal", "Create a profile for my sister Priya, her email is priya.sharma@example.com", fresh=True)
    mem = x.c.get("/api/memory?section=personal").json()["memories"]
    return any(i["category"] == "contact" and "priya" in json.dumps(i).lower() for i in mem), str([i["key"] for i in mem])[:200]


@case("drafts an e-mail to a family member", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "Email my sister Priya wishing her a happy birthday")
    return bool(card_like(m, "email")) and "priya" in blob(m), m["reply"][:160]


@case("travel guide recommends local dishes", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "I just landed in Jaipur, what local dishes should I try?", fresh=True)
    return bool(card_like(m, "guide")) or has(m["reply"], "dal"), m["reply"][:160]


@case("plays music alongside the chat", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "Play some synthwave music")
    mc = card_like(m, "music", "audio")
    return bool(mc) and ("http" in json.dumps(mc)), json.dumps(mc)[:200]


@case("recommends movies", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "Recommend two good science fiction movies", fresh=True)
    return bool(card_like(m, "movie")) or m["reply"].count("\n") >= 2, m["reply"][:160]


@case("shows where a movie can be watched / plays it from YouTube", "personal", "personal")
def _(x: Ctx):
    m = x.ask("personal", "Where can I watch Inception?")
    b = blob(m)
    return any(k in b for k in ("netflix", "prime", "justwatch", "youtube")), m["reply"][:160]


# ------------------------------------------------------------------ business
@case("adds a client with the reason and chat reference", "business", "business")
def _(x: Ctx):
    m = x.ask("business", "Add a new client Acme Corp, contact jane@acme.com - they want 500 bags of beans every month", fresh=True)
    return bool(card_like(m, "client")) and any(r.startswith("client#") for r in m.get("refs") or []), str(m.get("refs"))


@case("adds a payment reminder for the client", "business", "business")
def _(x: Ctx):
    m = x.ask("business", "Acme Corp has to pay 2000 USD on 5 December, remind me")
    return bool(card_like(m, "reminder")), m["reply"][:160]


@case("records a credit statement", "business", "business")
def _(x: Ctx):
    m = x.ask("business", "We received 50,000 INR from Zenith Ventures today")
    tc = card_like(m, "transaction", "txn")
    return bool(tc) and "credit" in json.dumps(tc).lower(), json.dumps(tc)[:160]


@case("drafts a cold sales e-mail into the sent inbox", "business", "business")
def _(x: Ctx):
    m = x.ask("business", "Draft a cold sales email to cafe owners in Pune about our beans", fresh=True)
    return bool(card_like(m, "email")), m["reply"][:160]


@case("runs a what-if simulation", "business", "business")
def _(x: Ctx):
    m = x.ask("business", "What if we raise our prices by 15%? Simulate it.", fresh=True)
    return bool(card_like(m, "simulation", "scenario")), m["reply"][:160]


@case("removing a client removes its reminders", "business", "business")
def _(x: Ctx):
    m = x.ask("business", "Remove the client Acme Corp", fresh=True)
    return bool(card_like(m, "client")) and ("remov" in blob(m) or "delet" in blob(m)), m["reply"][:160]


# ------------------------------------------------------------------ coding
@case("scans a dependency with OSV.dev", "coding", "coding")
def _(x: Ctx):
    m = x.ask("coding", "Is requests 2.19.0 safe to use? check it for vulnerabilities", fresh=True)
    vc = card_like(m, "vuln", "scan")
    return bool(vc) and ("ghsa" in json.dumps(vc).lower() or "cve" in json.dumps(vc).lower() or "pysec" in json.dumps(vc).lower()), json.dumps(vc)[:200]
