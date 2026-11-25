"""Memory requests are honoured: an explicit 'save this to my memory' message (even full of typos)
creates a REAL memory, a reply never claims a save that did not happen, and saved instructions
apply to every later reply of that mode."""
from __future__ import annotations

import asyncio

from helpers import Client


def _memories(c: Client, section: str = "personal") -> list[dict]:
    return c.get(f"/api/memory?section={section}").json()["memories"]


def test_typo_save_request_really_saves():
    # the exact conversation that used to end with a false "I've added it to your global memory"
    c = Client().register("memreq")
    no = c.new_chat("personal")
    c.send(no, "stop reaausrring htats it")
    m = c.send(no, "no in th enotes add stop reausrering and save thios to my global emmeory")
    hits = [x for x in _memories(c) if "reassur" in f"{x['key']} {x['value']}".lower()]
    assert hits, (m["reply"], _memories(c))
    assert any(card.get("type") == "memory" for card in m.get("cards") or []), m


def test_saved_instruction_applies_to_later_replies():
    c = Client().register("meminstr")
    r = c.post("/api/memory", json={"section": "personal", "category": "instruction", "key": "sign off",
                                    "value": "End every reply with the exact word PINEAPPLE."})
    assert r.status_code == 200 and r.json()["memory"]["category"] == "instruction"
    no = c.new_chat("personal")
    m = c.send(no, "Say hello in one short sentence.")
    assert "pineapple" in m["reply"].lower(), m["reply"]
    # instructions are per mode: business replies are unaffected
    bno = c.new_chat("business")
    b = c.send(bno, "Say hello in one short sentence.")
    assert "pineapple" not in b["reply"].lower(), b["reply"]


def test_reply_claims_are_detected():
    async def run() -> tuple[float, float]:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.config import settings
        from app.core import intents

        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                pos = await intents.claims_save(db, 'Okay, I\'ve added "stop reassuring" to your global memory. Anything else?')
                neg = await intents.claims_save(db, "Okay, got it. I won't re-assure you again. Let me know what else you need.")
                return pos, neg
        finally:
            await engine.dispose()

    pos, neg = asyncio.run(run())
    assert pos > 0, "a claimed save must be detected"
    assert neg == 0, "an ordinary acknowledgement is not a save claim"
