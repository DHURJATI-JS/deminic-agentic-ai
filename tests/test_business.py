"""Business section bug hunt: clients (reason + chat ref), reminder cascade on
client removal, 3-minute cancel window, encrypted credit/debit statements,
sent inbox, {business_name} variable after a rename, tenant isolation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


def iso(delta_s: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def test_client_removal_cascades_reminders(alice):
    c = alice.post("/api/business/clients", json={"name": "Acme Corp", "email": "jane@acme.com",
                                                  "reason": "wants 500 bags monthly"}).json()["client"]
    assert c["client_no"] and "500 bags" in str(c)
    r = alice.post("/api/business/reminders", json={"client_no": c["client_no"], "amount": "2000", "currency": "USD",
                                                    "due_at": iso(86400)}).json()["reminder"]
    assert r["reminder_no"]
    detail = alice.get(f"/api/business/clients/{c['client_no']}").json()
    assert len(detail["reminders"]) == 1
    assert alice.post(f"/api/business/clients", json={"name": "Acme Corp"}).status_code == 409  # duplicate guard
    assert alice.delete(f"/api/business/clients/{c['client_no']}").status_code == 200
    assert alice.get(f"/api/business/clients/{c['client_no']}").status_code == 404
    remaining = alice.get("/api/business/reminders?status=all").json()["reminders"]
    assert all(x.get("reminder_no") != r["reminder_no"] for x in remaining), remaining


def test_reminder_cancel_refused_inside_three_minutes(alice):
    r = alice.post("/api/business/reminders", json={"client_name": "Late Payer Ltd", "amount": "100",
                                                    "due_at": iso(120)}).json()["reminder"]
    res = alice.post(f"/api/business/reminders/{r['reminder_no']}/cancel")
    assert res.status_code in (400, 409, 422) and "3 minute" in res.text.lower(), res.text


def test_statements_totals_and_encryption(alice):
    alice.post("/api/business/transactions", json={"kind": "credit", "amount": "50000", "currency": "INR",
                                                   "counterparty": "Zenith Ventures", "description": "seed funding"})
    alice.post("/api/business/transactions", json={"kind": "debit", "amount": "1200.50", "currency": "INR",
                                                   "counterparty": "Landlord", "description": "rent"})
    data = alice.get("/api/business/transactions").json()
    assert len(data["transactions"]) == 2
    assert "48799.5" in str(data["totals"]) or "48,799.5" in str(data["totals"]), data["totals"]
    assert alice.get("/api/business/transactions?q=zenith").json()["transactions"][0]["counterparty"] == "Zenith Ventures"

    async def raw():
        from sqlalchemy import text

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from app.config import settings

        engine = create_async_engine(settings.database_url, poolclass=NullPool)

        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT t.amount, t.counterparty FROM transactions t JOIN users u ON u.id=t.user_id WHERE u.username=:u"),
                {"u": alice.username})).all()
        await engine.dispose()
        return rows

    for amount, counterparty in asyncio.run(raw()):
        assert amount.startswith("enc:") and counterparty.startswith("enc:")


def test_inbox_and_business_name_variable(alice):
    alice.put("/api/settings/business", json={"owns_business": True, "business_name": "Nimbus Coffee",
                                              "business_description": "Coffee roaster", "confirm_new_business": True})
    em = alice.post("/api/business/emails", json={"to_email": "owner@cafe.example", "subject": "Beans from {business_name}",
                                                 "body": "Hi! {business_name} roasts fresh beans weekly."}).json()["email"]
    got = alice.get(f"/api/business/emails/{em['email_no']}").json()
    assert "Nimbus Coffee" in str(got)
    alice.put("/api/settings/business", json={"business_name": "Nimbus Roasters"})  # rename only
    got2 = alice.get(f"/api/business/emails/{em['email_no']}").json()
    assert "Nimbus Roasters" in str(got2) and "Nimbus Coffee" not in str(got2)
    assert alice.post(f"/api/business/emails/{em['email_no']}/send").status_code == 400  # no SMTP configured
    assert any(e["email_no"] == em["email_no"] for e in alice.get("/api/business/emails").json()["emails"])


def test_business_isolation(alice, bob):
    c = alice.post("/api/business/clients", json={"name": "Private Client"}).json()["client"]
    t = alice.post("/api/business/transactions", json={"kind": "credit", "amount": "999", "counterparty": "Secret"}).json()["transaction"]
    em = alice.post("/api/business/emails", json={"to_email": "x@example.com", "subject": "s", "body": "b"}).json()["email"]
    assert bob.get("/api/business/clients").json()["clients"] == []
    assert bob.get(f"/api/business/clients/{c['client_no']}").status_code == 404
    assert bob.delete(f"/api/business/clients/{c['client_no']}").status_code == 404
    assert bob.get("/api/business/transactions").json()["transactions"] == []
    assert bob.delete(f"/api/business/transactions/{t['txn_no']}").status_code == 404
    assert bob.get(f"/api/business/emails/{em['email_no']}").status_code == 404
    assert "Secret" not in bob.get("/api/business/overview").text
