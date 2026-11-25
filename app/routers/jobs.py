"""Background tasks API (Socket.IO ``job:cancel`` is the primary cancel path)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jobs import jobs
from app.core.logs import history
from app.db import get_db
from app.models import BackgroundJob, User
from app.security.sessions import current_user

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/jobs")
async def list_jobs(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    live = jobs.list_for(user.id)
    seen = {j["request_id"] for j in live}
    rows = (await db.execute(select(BackgroundJob).where(BackgroundJob.user_id == user.id)
                             .order_by(BackgroundJob.created_at.desc()).limit(60))).scalars().all()
    past = [{
        "request_id": r.id, "kind": r.kind, "title": r.title, "status": r.status, "progress": r.progress,
        "detail": r.detail, "cancellable": r.cancellable, "can_cancel": False, "cancel_reason": "",
        "created_at": r.created_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "meta": dict(r.meta or {}), "attempts": r.attempts,
    } for r in rows if r.id not in seen]
    return {"jobs": live + past}


@router.post("/jobs/{request_id}/cancel")
async def cancel_job(request_id: str, user: User = Depends(current_user)):
    ok, msg = await jobs.cancel(user.id, request_id, reason="cancelled by user (http)")
    return {"ok": ok, "message": msg, "request_id": request_id}


@router.get("/terminal")
async def terminal_history(user: User = Depends(current_user)):
    return {"lines": await history(user.id, 300)}


# --------------------------------------------------------------------------- terminal AI suggestions
from fastapi import Request  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.ai import failsafe  # noqa: E402
from app.ai.providers import parse_json_loose  # noqa: E402
from app.security.ratelimit import limiter  # noqa: E402

ALLOWED_HEADS = {"help", "jobs", "cancel", "clear", "whoami", "status", "theme", "section", "tab", "open", "new",
                 "immersive", "comfort", "filter", "palette"}
SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {"suggestions": {"type": "array", "maxItems": 3, "items": {
        "type": "object", "properties": {"command": {"type": "string"}, "why": {"type": "string"}},
        "required": ["command", "why"]}}},
    "required": ["suggestions"],
}


class SuggestIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    commands: list[str] = Field(default_factory=list, max_length=40)
    context: dict = Field(default_factory=dict)


@router.post("/terminal/suggest")
@limiter.limit("30/minute")
async def terminal_suggest(request: Request, body: SuggestIn, user: User = Depends(current_user)):
    """Turn a plain-English request into terminal commands (validated against the command list)."""
    grammar = "\n".join(c[:120] for c in body.commands[:40])
    ctx = {k: body.context.get(k) for k in ("section", "tabs", "running_jobs")}
    msgs = [
        {"role": "system", "content": "You convert a user's plain-English request into commands for the Deminic in-app "
                                      "terminal. Use ONLY these commands and their exact syntax:\n" + grammar +
                                      "\nReturn up to 3 suggestions, best first, each with a short reason. JSON only."},
        {"role": "user", "content": f"Context: {ctx}\nRequest: {body.text}"},
    ]
    try:
        raw, _ = await failsafe.complete(user.settings, "personal", msgs, uid=user.id, json_schema=SUGGEST_SCHEMA,
                                         max_tokens=220, temperature=0.1, allow_online=False)
    except failsafe.AIUnavailable:
        return {"suggestions": []}
    out = []
    for s in (parse_json_loose(raw) or {}).get("suggestions") or []:
        cmd = str(s.get("command", "")).strip().lstrip("$> ").strip()
        if cmd and cmd.split()[0].lower() in ALLOWED_HEADS:
            out.append({"command": cmd[:120], "why": str(s.get("why", ""))[:160]})
    return {"suggestions": out[:3]}
