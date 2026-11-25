"""Chat history compaction.

Every time a new message is sent, the *previous* user prompt and its reply are
compacted into one entry appended to ``chats.compacted_history`` (TEXT). The
LLM context is built from that compact text instead of the full message list,
which keeps prompts inside the local model's 8K context window. When the
compact text itself grows too large the oldest part is folded into a summary.
"""
from __future__ import annotations

import re

from app.models import ChatHistory

MAX_COMPACT_CHARS = 6000
KEEP_TAIL_CHARS = 3500

_CODE = re.compile(r"```.*?```", re.S)
_WS = re.compile(r"\s+")


def squash(text: str, limit: int) -> str:
    text = _CODE.sub(" [code block] ", text or "")
    text = _WS.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compact_exchange(chatno: int, prompt: str, reply: str, ai_name: str) -> str:
    return f"[#{chatno}] User: {squash(prompt, 320)}\n[#{chatno}] {ai_name}: {squash(reply, 520)}\n"


def compact_previous(chat: ChatHistory, ai_name: str) -> bool:
    """Compact every finished exchange not yet compacted (on the separate
    chat_histories row). Returns True if changed."""
    changed = False
    for i, m in enumerate(chat.messages):
        if m.get("compacted") or m.get("status") not in ("done", "cancelled", "error"):
            continue
        entry = compact_exchange(m.get("chatno", i + 1), m.get("prompt", ""), m.get("reply", ""), ai_name)
        chat.compacted_history = (chat.compacted_history or "") + entry
        chat.messages[i] = {**m, "compacted": True}
        changed = True
    if len(chat.compacted_history or "") > MAX_COMPACT_CHARS:
        text = chat.compacted_history
        cut = text.find("\n[#", len(text) - KEEP_TAIL_CHARS)
        if cut > 0:
            head, tail = text[:cut], text[cut + 1 :]
            chat.compacted_history = "[earlier conversation, condensed] " + squash(head, 900) + "\n" + tail
            changed = True
    return changed
