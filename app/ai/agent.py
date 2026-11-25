"""Agent orchestrator - every chat message runs as a cancellable background job
so switching chats never stops generation (several chats can generate in
parallel; each is visible + cancellable by request id in Background tasks).

Pipeline per message (the message is embedded ONCE; that vector drives every
routing decision through pgvector - no regex routing):
  1. compaction of the previous exchange into chat_histories.compacted_history
  2. Sentinel semantic injection scan
  3. semantic memory retrieval (vector DB) -> O(1) dict value lookups
  4. pgvector recall of related chats / tasks / items
  5. profile / photo access (semantic match)
  6. semantic tool selection -> JSON-schema constrained LLM router -> tools
  7. streamed answer (gateway -> docker fallback -> online) with strict
     BEYOND MY LIMITS detection -> escalation to Claude / ChatGPT / GitHub
  8. Sentinel output guard (secrets + cross-account leak redaction)
  9. save into chat_histories.messages (JSONB MutableList) + push chat:done
 10. low-priority insights job (main points, summaries, auto-memory, vectors)
"""
from __future__ import annotations

import asyncio
import json
import base64
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import failsafe, prompts
from app.ai.providers import Provider, parse_json_loose
from app.ai.tools import TOOLS, ToolCtx, ToolResult, execute, select_tools
from app.config import settings
from app.core import intents, vectors
from app.core.compaction import compact_previous
from app.core.embeddings import embed_one
from app.core.jobs import DuplicateJob, JobContext, jobs
from app.core.logs import ulog
from app.core.memory import memory_context, memory_cache
from app.core.sockets import emit_user
from app.core.utils import clip, local_now, render_business_vars
from app.db import SessionLocal
from app.models import Chat, ChatHistory, SecurityEvent, User
from app.security import sentinel
from app.services import memory_service

log = logging.getLogger("deminic.agent")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
BEYOND_TEXT = "BEYOND MY LIMITS"


_PSEUDO_REF = re.compile(r"[ \t]*\(?\b([a-z_]+)#(\d+)(?:\.\d+)?\b\)?", re.I)
_VALID_REF_KINDS = {"chat", "task", "client", "email", "event", "reminder", "txn"}


def strip_fake_refs(text: str, known: list[str]) -> str:
    """Remove references the model invented (e.g. "(weather#1)", "(health#1)").
    Real kinds (chat/task/client/email/event/reminder/txn) are kept."""

    def _fix(m: re.Match) -> str:
        return m.group(0) if m.group(1).lower() in _VALID_REF_KINDS else ""

    return _PSEUDO_REF.sub(_fix, text or "")


_TOOL_ECHO = re.compile(r"^\s*((?:TOOL RESULTS|ACTIONS ALREADY DONE)\b.*|\[[a-z_]+\]\s+(OK|FAILED):.*)$", re.I | re.M)
# internal headings mentioned mid-sentence ("... in the previous TOOL RESULTS (event#1)") - exact tokens only
_TOOL_MENTION = re.compile(r"\s*\b(?:(?:in|from|under|see)\s+)?(?:the\s+)?(?:(?:previous|above|earlier)\s+)?"
                           r"(?:TOOL RESULTS|ACTIONS ALREADY DONE)\b", re.I)


def strip_tool_echo(text: str) -> str:
    """Remove the internal ACTIONS ALREADY DONE / TOOL RESULTS block if the model pasted it, and
    any mid-sentence mention of those internal headings."""
    cleaned = _TOOL_ECHO.sub("", text or "")
    cleaned = _TOOL_MENTION.sub("", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip() or (text or "")


# A save request this unambiguous ("save this to my global memory") is saved even if the router skips it.
FORCE_SAVE_SCORE = 0.85


def _saved(results: list[tuple[str, ToolResult]]) -> bool:
    return any(name == "remember" and r.ok for name, r in results)


async def _force_remember(tctx: ToolCtx, user: User, hist: ChatHistory, text: str,
                          results: list[tuple[str, ToolResult]], state: dict, tlog) -> bool:
    """Work out WHAT the user wants saved (typos fixed, 'this'/'that' resolved from the conversation)
    and run the real remember tool, so the memory card and the database agree with the reply."""
    args = await memory_service.extract_requested(user, text, hist.compacted_history or "")
    if not args:
        await tlog("memory: nothing concrete to save could be extracted")
        return False
    res = await execute(tctx, "remember", args)
    results.append(("remember", res))
    state["cards"] += res.cards
    state["refs"] += res.refs
    await tlog(f"tool -> remember (guaranteed save): {'ok' if res.ok else 'failed'} {clip(res.text, 160)}")
    return res.ok


def router_schema(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {"tool": {"type": "string", "enum": names}, "args": {"type": "object"}},
                    "required": ["tool", "args"],
                },
            }
        },
        "required": ["actions"],
    }


def is_outdated(chat: Chat, user: User) -> bool:
    return chat.section == "business" and chat.business_version is not None and chat.business_version != user.business_version


async def get_history(db: AsyncSession, chat: Chat, *, lock: bool = False) -> ChatHistory:
    stmt = select(ChatHistory).where(ChatHistory.chat_id == chat.id, ChatHistory.user_id == chat.user_id)
    if lock:
        stmt = stmt.with_for_update()
    hist = (await db.execute(stmt)).scalar_one_or_none()
    if hist is None:
        hist = ChatHistory(chat_id=chat.id, user_id=chat.user_id, messages=[], compacted_history="")
        db.add(hist)
        await db.flush()
    return hist


# =========================================================================== submit
async def start_generation(
    db: AsyncSession,
    user: User,
    chat: Chat,
    text: str,
    *,
    location: dict | None = None,
    lawyer: bool | None = None,
    request_id: str | None = None,
    provider: str | None = None,
    attachments: list[str] | None = None,
) -> dict:
    from app.services import attachments as att_svc

    text = (text or "").strip()
    att_meta = await att_svc.resolve(user.id, attachments or [])
    if not text:
        raise HTTPException(400, "Message is empty")
    if len(text) > 8000:
        raise HTTPException(413, "Message too long (8000 characters max)")
    if is_outdated(chat, user):
        raise HTTPException(409, "This chat belongs to your previous business description - please start a new chat.")
    dedupe = f"gen:{user.id}:{chat.id}"
    if jobs.dedupe_owner(dedupe):
        raise HTTPException(409, "Still generating the previous reply in this chat - wait for it or cancel it first.")

    locked_chat = (
        await db.execute(select(Chat).where(Chat.id == chat.id, Chat.user_id == user.id).with_for_update())
    ).scalar_one()
    hist = await get_history(db, locked_chat, lock=True)
    compact_previous(hist, user.ai_name)
    msg_no = len(hist.messages) + 1
    rid = (request_id or "").lower()
    if not _UUID.match(rid) or jobs.get(user.id, rid) or rid in jobs._jobs:  # noqa: SLF001
        rid = str(uuid.uuid4())
    hist.messages.append(
        {
            "chatno": msg_no,
            "prompt": text,
            "reply": "",
            "status": "generating",
            "request_id": rid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "location": location or None,
            "attachments": [{k: m[k] for k in ("id", "name", "kind", "chars")} for m in att_meta] or None,
        }
    )
    if not locked_chat.title or locked_chat.title == "New chat":
        locked_chat.title = clip(re.sub(r"\s+", " ", text), 60)
    if lawyer is not None:
        locked_chat.lawyer_mode = bool(lawyer)
    locked_chat.message_count = msg_no
    locked_chat.updated_at = datetime.now(timezone.utc)
    chat_id, chat_no, section = locked_chat.id, locked_chat.chat_no, locked_chat.section
    await db.commit()
    await ulog(user.id, f"chat#{chat_no}.{msg_no}: message received ({len(text)} chars) - previous exchange compacted",
               src="agent")

    async def _job(ctx: JobContext):
        return await run_generation(ctx, chat_id, msg_no, text, location, provider, att_meta)

    try:
        h = await jobs.spawn(
            user.id,
            "generation",
            f"{section} chat#{chat_no}.{msg_no} reply",
            _job,
            dedupe_key=dedupe,
            job_id=rid,
            cancellable=True,
            retries=1,
            timeout_s=settings.generation_timeout_s,
            meta={"chat_no": chat_no, "msg_no": msg_no, "section": section},
        )
    except DuplicateJob as e:
        await save_message(chat_id, user.id, msg_no, status="error", reply="(duplicate submission blocked)")
        raise HTTPException(409, "Duplicate submission blocked - a reply is already generating.") from e
    return {"request_id": h.id, "chat_no": chat_no, "msg_no": msg_no}


async def save_message(chat_id: int, uid: int, msg_no: int, **fields) -> None:
    async with SessionLocal() as db:
        hist = (
            await db.execute(
                select(ChatHistory).where(ChatHistory.chat_id == chat_id, ChatHistory.user_id == uid).with_for_update()
            )
        ).scalar_one_or_none()
        if hist is None:
            return
        for i, m in enumerate(hist.messages):
            if m.get("chatno") == msg_no:
                hist.messages[i] = {**m, **fields}
                break
        chat = (await db.execute(select(Chat).where(Chat.id == chat_id, Chat.user_id == uid))).scalar_one_or_none()
        if chat is not None:
            chat.updated_at = datetime.now(timezone.utc)
        await db.commit()


# =========================================================================== run
async def run_generation(ctx: JobContext, chat_id: int, msg_no: int, text: str,
                         location: dict | None, provider_override: str | None,
                         att_meta: list[dict] | None = None) -> dict:
    uid = ctx.user_id
    state = {"partial": "", "cards": [], "refs": [], "provider": None, "escalated": False}

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        s = user.settings
        chat = (await db.execute(select(Chat).where(Chat.id == chat_id, Chat.user_id == uid))).scalar_one()
        hist = await get_history(db, chat)
        section = chat.section
        base = {"request_id": ctx.id, "chat_no": chat.chat_no, "msg_no": msg_no, "section": section}

        async def emit(event: str, data: dict) -> None:
            await emit_user(uid, event, {**base, **data})

        async def tlog(m: str) -> None:
            await ulog(uid, f"chat#{chat.chat_no}.{msg_no}: {m}", src="agent")

        try:
            await emit("chat:status", {"stage": "thinking"})
            vec, emodel = await embed_one(text)
            await tlog(f"message embedded ({emodel}) for vector routing")

            # ---- 2. Sentinel semantic input scan ----------------------------
            verdict = await sentinel.scan_input(db, vec, emodel)
            if verdict.flagged:
                await tlog(f"sentinel: input flagged {verdict.risk} ({'; '.join(verdict.reasons)})")
                db.add(SecurityEvent(user_id=uid, kind="input-flag", detail=f"{verdict.risk}:{verdict.nearest}:{verdict.score}"))
                await db.commit()

            # ---- location -> place -----------------------------------------
            place = ""
            if location and s.share_location:
                from app.services.geo import place_for

                place = await place_for(location)
                if place:
                    await tlog(f"location resolved: {place}")

            # ---- 3/4. semantic memory + recall -------------------------------
            mem_text, cats, hits = await memory_context(db, uid, section, vec, emodel)
            recall: list[dict] = []
            if mem_text:
                await tlog(f"vector memory search: {hits} memory hit(s), categories {cats or '-'}")
            else:
                await tlog("vector memory search: no global memory needed for this message")
            recall = await vectors.search_vec(db, uid, section, vec, emodel, k=4,
                                              types=["chat", "task", "event", "note"], kind="recall")
            recall = [r for r in recall if not (r["source_type"] == "chat" and r["source_id"] == chat.id)]
            if recall:
                await tlog(f"pgvector recall: {len(recall)} related item(s)")

            # ---- 5. profile / photo (semantic) --------------------------------
            profile_block, image_part = "", None
            wants_profile = bool(await intents.matches(db, vec, emodel, "profile"))
            wants_photo = bool(await intents.matches(db, vec, emodel, "photo"))
            if wants_profile or wants_photo:
                profile_block = (
                    f"name: {user.name}\nusername: {user.username}\nAI name (you): {user.ai_name}\n"
                    f"about: {clip(user.about, 400)}\nbusiness: {user.business_name or '-'}"
                )
                if wants_photo:
                    p = settings.uploads_dir / "avatars" / Path(user.avatar_path or "none").name
                    if user.avatar_path and p.exists():
                        b64 = base64.b64encode(p.read_bytes()).decode()
                        image_part = {"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{b64}"}}
                        await tlog("profile photo attached (the user asked about it)")
                    else:
                        profile_block += "\nprofile photo: none uploaded yet"
                await tlog("profile access granted (semantic match to a profile question)")

            # ---- attachments ("analyze" skill) --------------------------------
            extra_parts: list[dict] = []
            if att_meta:
                from app.services import attachments as att_svc

                for meta in att_meta:
                    kind, content = att_svc.load_content(uid, meta)
                    if kind == "image" and content:
                        extra_parts.append({"type": "image_url", "image_url": {"url": content}})
                    elif content:
                        fenced = await sentinel.neutralize_external(db, content[:14000],
                                                                    source=f"attached file {meta['name']}")
                        extra_parts.append({"type": "text",
                                            "text": f"\n\nATTACHED FILE '{meta['name']}' ({meta['chars']} chars):\n{fenced}"})
                await tlog(f"{len(att_meta)} attachment(s) loaded for analysis: {', '.join(m['name'] for m in att_meta)}")

            # ---- 6. semantic tool selection + router + execution ---------------
            tctx = ToolCtx(db=db, user=user, settings=s, section=section, chat=chat, history=hist, msg_no=msg_no,
                           request_id=ctx.id, text=text, location=location, place=place, log=tlog, emit=emit,
                           vec=vec, embed_model=emodel)
            results: list[tuple[str, ToolResult]] = []
            specs, scored = await select_tools(db, section, vec, emodel)
            top = ", ".join(f"{n}:{sc:.2f}" for n, sc in scored[:4])
            # explicit "save this to memory" (typo-tolerant): a SOFT signal - remember goes on the table and
            # the router is told; it still decides (e.g. "show my notes" also scores high).
            save_score = await intents.save_requested(db, text, vec, emodel)
            route_hint = ""
            if save_score:
                rem = TOOLS.get("remember")
                if rem is not None and section in rem.sections and rem not in specs:
                    specs = [rem, *specs][:6]
                route_hint = ("The user may be asking to save something to their global memory (the message can contain "
                              "typos). If so, include a remember action with exactly what to save; rules about how you "
                              "should behave use category instruction.")
                await tlog(f"memory request signal {save_score:.2f} - remember tool offered to the router")
            if specs:
                await tlog(f"vector tool search -> candidates [{', '.join(t.name for t in specs)}] (top: {top})")
                await emit("chat:status", {"stage": "planning"})
                actions = await _route(s, user, section, hist, text, specs, uid, hint=route_hint)
                for a in actions:
                    ctx.heartbeat()
                    await tlog(f"tool -> {a['tool']}({clip(str(a.get('args', {})), 160)})")
                    await emit("chat:status", {"stage": "tool", "tool": a["tool"]})
                    res = await execute(tctx, a["tool"], a.get("args") or {})
                    results.append((a["tool"], res))
                    state["cards"] += res.cards
                    state["refs"] += res.refs
                    await tlog(f"tool <- {a['tool']}: {'ok' if res.ok else 'failed'} {clip(res.text, 160)}")
                await db.commit()
            else:
                await tlog(f"vector tool search: nothing relevant (top: {top or '-'}) - direct answer")
            # an unmistakable save request that the router skipped is saved anyway
            forced_tried = False
            if save_score >= FORCE_SAVE_SCORE and not _saved(results):
                forced_tried = True
                await _force_remember(tctx, user, hist, text, results, state, tlog)
            instructions = await memory_service.standing_instructions(db, uid, section)
            if instructions:
                await tlog(f"{len(instructions)} standing instruction(s) from global memory applied")

            # ---- 7. answer ---------------------------------------------------
            lawyer = bool(chat.lawyer_mode or any(r.lawyer for _, r in results))
            verbatim = next((r.reply for _, r in results if r.reply), None)
            force_escalate = any(r.escalate for _, r in results)

            def _msgs(escalated: bool = False) -> list[dict]:
                return build_messages(user, section, hist, text, lawyer=lawyer, place=place, mem_text=mem_text,
                                      recall=recall, profile_block=profile_block, image_part=image_part,
                                      results=results, escalated=escalated, extra_parts=extra_parts,
                                      instructions=instructions)

            if verbatim is not None:
                final = verbatim
                await emit("chat:delta", {"delta": final})
                state["provider"] = "tools"
            else:
                final = await _answer(ctx, s, user, section, _msgs(), emit, tlog, state,
                                      force_escalate=force_escalate, provider_override=provider_override,
                                      escalate_builder=lambda: _msgs(True))

            final = strip_fake_refs(strip_tool_echo(final), state["refs"])

            # ---- 7b. truthfulness: a reply may only claim a memory save that really happened ----
            if not _saved(results):
                claim = await intents.claims_save(db, final)
                if claim:
                    await tlog(f"reply claims a memory save ({claim:.2f}) but nothing was saved - making it true")
                    if forced_tried or not await _force_remember(tctx, user, hist, text, results, state, tlog):
                        final = (final.rstrip() + "\n\n_Correction: nothing was saved to your memory. Tell me exactly "
                                 "what to remember and I'll save it._")
                        await tlog("memory claim could not be honoured - correction added to the reply")

            # ---- 8. Sentinel output guard -----------------------------------
            allowed = await memory_cache.known_emails(db, uid)
            final, leaks = await sentinel.guard_output(final, own_email=user.email, own_public_id=user.public_id,
                                                       allowed_emails=allowed)
            if leaks:
                await tlog(f"SENTINEL: blocked {len(leaks)} cross-account identifier(s) in the reply")
                db.add(SecurityEvent(user_id=uid, kind="leak-blocked", detail=f"{len(leaks)} identifier(s)"))
                await db.commit()

            # ---- 9. save + push ---------------------------------------------
            await save_message(chat_id, uid, msg_no, reply=final, status="done", cards=state["cards"],
                               refs=sorted(set(state["refs"])), provider=state["provider"],
                               escalated=state["escalated"], completed_at=datetime.now(timezone.utc).isoformat())
            await emit("chat:done", {
                "status": "done",
                "reply": render_business_vars(final, user.business_name),
                "cards": state["cards"],
                "refs": sorted(set(state["refs"])),
                "provider": state["provider"],
                "escalated": state["escalated"],
            })
            await tlog(f"reply saved ({len(final)} chars via {state['provider']})")
        except asyncio.CancelledError:
            partial = state["partial"]
            await save_message(chat_id, uid, msg_no, status="cancelled", cards=state["cards"],
                               refs=sorted(set(state["refs"])),
                               reply=(partial + "\n\n_[generation cancelled]_").strip())
            await emit_user(uid, "chat:done", {**base, "status": "cancelled",
                                                "reply": render_business_vars(partial, user.business_name)
                                                + "\n\n_[generation cancelled]_", "cards": state["cards"]})
            raise
        except failsafe.AIUnavailable as e:
            if ctx.handle.attempts <= ctx.handle.retries:
                await tlog("all AI providers failed - failsafe will retry the whole step")
                await emit("chat:reset", {})
                state["partial"] = ""
                raise
            msg = ("⚠️ I couldn't reach any AI model (local gateway, docker fallback"
                   + (" and online providers" if failsafe.online(s) else "") + "). Please check Settings → AI providers.")
            await save_message(chat_id, uid, msg_no, status="error", reply=msg, cards=state["cards"])
            await emit("chat:done", {"status": "error", "reply": msg, "cards": state["cards"]})
            await tlog(f"AI unavailable: {e}")
            return {"error": "ai-unavailable"}

    await spawn_insights(uid, chat_id, msg_no, section)
    return {"chat_no": base["chat_no"], "msg_no": msg_no}


async def _route(s, user: User, section: str, hist: ChatHistory, text: str, specs, uid: int, *,
                 hint: str = "") -> list[dict]:
    names = [t.name for t in specs]
    catalog = "\n".join(t.catalog_line() for t in specs)
    now = local_now(user.timezone)
    sys_p = prompts.ROUTER_SYSTEM.replace("{now}", now.isoformat(timespec="minutes")).replace("{tz}", user.timezone)
    tail = (hist.compacted_history or "")[-900:]
    note = f"NOTE: {hint}\n\n" if hint else ""
    usr = (f"TOOL CATALOGUE:\n{catalog}\n\nRECENT CONVERSATION (compact):\n{tail or '(none)'}\n\n{note}"
           f"LATEST USER MESSAGE:\n{text}")
    t0 = time.monotonic()
    try:
        raw, p = await failsafe.complete(s, section, [{"role": "system", "content": sys_p}, {"role": "user", "content": usr}],
                                         uid=uid, json_schema=router_schema(names), max_tokens=500, temperature=0.0,
                                         allow_online=False)
    except failsafe.AIUnavailable:
        await ulog(uid, "router unavailable - answering without tools", level="warn", src="agent")
        return []
    data = parse_json_loose(raw) or {}
    acts = data.get("actions") if isinstance(data.get("actions"), list) else []
    out, seen = [], set()
    for a in acts:
        if not (isinstance(a, dict) and a.get("tool") in names):
            continue
        sig = (a["tool"], json.dumps(a.get("args") or {}, sort_keys=True, default=str))
        if sig in seen:  # small models sometimes repeat the same call
            continue
        seen.add(sig)
        out.append(a)
    out = out[:4]
    # Deterministic guard: a plain question is never a new fact to save.
    is_question = text.rstrip().endswith("?") and "remember" not in text.lower() and "note that" not in text.lower()
    if is_question:
        out = [a for a in out if a.get("tool") != "remember"]
    await ulog(uid, f"router ({p.label}, {int((time.monotonic() - t0) * 1000)} ms): "
                    f"{[a['tool'] for a in out] or 'no tools'}", src="agent")
    return out


def build_messages(user: User, section: str, hist: ChatHistory, text: str, *, lawyer: bool, place: str, mem_text: str,
                   recall: list[dict], profile_block: str, image_part: dict | None,
                   results: list[tuple[str, ToolResult]], escalated: bool = False,
                   extra_parts: list[dict] | None = None, instructions: list[str] | None = None) -> list[dict]:
    addenda = [r.system_addendum for _, r in results if r.system_addendum]
    sys_p = prompts.system_prompt(user, section, lawyer=lawyer, place=place, extra="\n\n".join(addenda))
    if escalated:
        sys_p = sys_p.replace(prompts.BEYOND_RULE, "You are the escalation model: answer fully and expertly.")
    blocks = [sys_p]
    if instructions:  # saved in global memory as category "instruction" - apply to EVERY reply of this mode
        blocks.append("STANDING INSTRUCTIONS FROM THE USER (saved in their global memory - always follow them):\n"
                      + "\n".join(f"- {i}" for i in instructions))
    if profile_block:
        blocks.append("USER PROFILE (the user asked about it):\n" + profile_block)
    if mem_text:
        blocks.append("GLOBAL MEMORY (retrieved by vector search for this message):\n" + mem_text)
    if recall:
        blocks.append("RELEVANT RECALL:\n" + "\n".join(f"- ({r['source_type']}) {clip(r['content'], 240)}" for r in recall))
    hist_text = (hist.compacted_history or "")[-2600:]
    if hist_text:
        blocks.append("CONVERSATION SO FAR (compacted):\n" + hist_text)
    tool_lines = [f"[{name}] {'OK' if r.ok else 'FAILED'}: {r.text}" for name, r in results if r.text]
    if tool_lines:
        blocks.append("ACTIONS ALREADY DONE (internal - report them accurately with their references; never name this "
                      "heading):\n" + clip("\n".join(tool_lines), 3500))
    parts: list[dict] = [{"type": "text", "text": text}]
    if image_part is not None:
        parts.append(image_part)
    parts += list(extra_parts or [])
    if any(p.get("type") == "image_url" for p in parts):
        content: str | list = parts
    else:
        content = "".join(p.get("text", "") for p in parts)
    return [{"role": "system", "content": "\n\n".join(blocks)}, {"role": "user", "content": content}]


async def _answer(ctx: JobContext, s, user: User, section: str, messages: list[dict], emit, tlog, state: dict, *,
                  force_escalate: bool, provider_override: str | None, escalate_builder) -> str:
    max_tokens = 1800 if section == "coding" or "LAWYER MODE" in str(messages[0].get("content")) else 1100
    if not force_escalate:
        buf, flushed, last_emit = "", 0, 0.0
        beyond = False
        provider: Provider | None = None
        await emit("chat:status", {"stage": "writing"})
        async for delta, p in failsafe.stream(s, section, messages, uid=user.id, max_tokens=max_tokens,
                                              override=provider_override):
            if delta == failsafe.RESET:
                buf, flushed = "", 0
                await emit("chat:reset", {})
                await tlog(f"failsafe: {p.label} failed mid-answer - restarting on the next provider")
                continue
            if provider is None or provider.key != p.key:
                provider = p
                state["provider"] = p.label
                await tlog(f"answering with {p.label}")
            buf += delta
            state["partial"] = buf
            ctx.handle.meta["partial"] = buf
            ctx.heartbeat()
            head = buf.lstrip().upper()
            if len(head) < len(BEYOND_TEXT) and BEYOND_TEXT.startswith(head):
                continue  # hold back until we know it is not the escalation phrase
            if failsafe.is_beyond(buf[:80]):
                beyond = True
                break
            now = time.monotonic()
            if now - last_emit > 0.05 or len(buf) - flushed > 60:
                await emit("chat:delta", {"delta": buf[flushed:]})
                flushed, last_emit = len(buf), now
        if not beyond and failsafe.is_beyond(buf) and len(buf.strip()) < 400:
            beyond = True
        if not beyond:
            if flushed < len(buf):
                await emit("chat:delta", {"delta": buf[flushed:]})
            return buf
        if provider and provider.online:
            return buf
    # ---- escalation (the "beyond my limits" function) ----------------------
    online = failsafe.online(s)
    await tlog("BEYOND MY LIMITS -> escalation function triggered")
    if not online:
        msg = ("\U0001f53c **This is beyond my local limits.** The local model flagged this request as needing a stronger "
               "model, but no online provider is configured. Add a **Claude**, **ChatGPT** or **GitHub Models** key in "
               "Settings → AI providers and ask again - I'll hand it over automatically.")
        await emit("chat:reset", {})
        await emit("chat:delta", {"delta": msg})
        state["provider"] = "escalation (no online provider)"
        return msg
    state["escalated"] = True
    await emit("chat:reset", {})
    await emit("chat:status", {"stage": "escalating", "provider": online[0].label})
    await tlog(f"escalating to {online[0].label}")
    buf, flushed, last_emit = "", 0, 0.0
    async for delta, p in failsafe.stream(s, section, escalate_builder(), uid=user.id, max_tokens=2500, chain=online):
        if delta == failsafe.RESET:
            buf, flushed = "", 0
            await emit("chat:reset", {})
            continue
        state["provider"] = p.label + " (escalated)"
        buf += delta
        state["partial"] = buf
        ctx.handle.meta["partial"] = buf
        ctx.heartbeat()
        now = time.monotonic()
        if now - last_emit > 0.05:
            await emit("chat:delta", {"delta": buf[flushed:]})
            flushed, last_emit = len(buf), now
    if flushed < len(buf):
        await emit("chat:delta", {"delta": buf[flushed:]})
    return buf


# =========================================================================== insights
POINTS_SCHEMA = {
    "type": "object",
    "properties": {"points": {"type": "array", "maxItems": 3, "items": {"type": "string"}}},
    "required": ["points"],
}


async def spawn_insights(uid: int, chat_id: int, msg_no: int, section: str) -> None:
    async def _job(ctx: JobContext):
        return await run_insights(ctx, chat_id, msg_no)

    try:
        await jobs.spawn(uid, "insights", f"main points & summaries for message {msg_no}", _job,
                         dedupe_key=f"insights:{uid}:{chat_id}:{msg_no}", retries=1, timeout_s=300,
                         meta={"section": section})
    except DuplicateJob:
        pass


async def run_insights(ctx: JobContext, chat_id: int, msg_no: int) -> dict:
    from app.services import memory_service, summaries

    uid = ctx.user_id
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        s = user.settings
        chat = (await db.execute(select(Chat).where(Chat.id == chat_id, Chat.user_id == uid))).scalar_one_or_none()
        if chat is None:
            return {}
        hist = await get_history(db, chat)
        m = next((x for x in hist.messages if x.get("chatno") == msg_no), None)
        if not m or m.get("status") != "done":
            return {}
        prompt, reply = m.get("prompt", ""), m.get("reply", "")
        section, chat_no = chat.section, chat.chat_no
        await ctx.progress(0.1, "extracting main points")
        points: list[str] = []
        if len(prompt) + len(reply) > 40:
            try:
                raw, _ = await failsafe.complete(
                    s, section if section != "coding" else "personal",
                    [{"role": "system", "content": "Extract the 1-3 most important takeaways (facts, decisions, "
                                                   "action items) from this exchange. Each under 20 words. JSON only."},
                     {"role": "user", "content": f"USER: {clip(prompt, 1500)}\n\nASSISTANT: {clip(reply, 2500)}"}],
                    uid=uid, json_schema=POINTS_SCHEMA, max_tokens=250, temperature=0.1, background=True,
                    allow_online=False)
                data = parse_json_loose(raw) or {}
                points = [clip(str(p).strip(), 200) for p in (data.get("points") or []) if str(p).strip()][:3]
            except failsafe.AIUnavailable:
                points = []
    if points:
        async with SessionLocal() as db:
            chat = (await db.execute(select(Chat).where(Chat.id == chat_id, Chat.user_id == uid).with_for_update())).scalar_one_or_none()
            if chat is not None:
                existing = {p.get("text") for p in chat.main_points}
                for p in points:
                    if p not in existing:
                        chat.main_points.append({"msg": msg_no, "text": p, "ref": f"chat#{chat_no}.{msg_no}"})
                if len(chat.main_points) > 40:
                    del chat.main_points[: len(chat.main_points) - 40]
                await vectors.upsert(db, uid, section, "chat", chat.id,
                                     f"chat#{chat_no} {chat.title}: " + "; ".join(p.get("text", "") for p in chat.main_points[-12:]))
                await db.commit()
        await emit_user(uid, "chat:points", {"chat_no": chat_no, "section": section,
                                              "points": [{"msg": msg_no, "text": p, "ref": f"chat#{chat_no}.{msg_no}"} for p in points]})
        await ulog(uid, f"chat#{chat_no}.{msg_no}: {len(points)} main point(s) saved", src="insights")
    await ctx.progress(0.5, "updating summaries / memory")
    vec, emodel = await embed_one(prompt)
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        await summaries.maybe_update(db, user, section, chat_no, msg_no, prompt, reply, vec, emodel)
        await memory_service.auto_extract(db, user, section, chat_no, msg_no, prompt, vec, emodel)
    return {"points": len(points)}
