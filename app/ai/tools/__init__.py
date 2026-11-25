"""Agent tool registry (semantic, pgvector-routed - no regex triggers).

Section modules register tools with the ``@tool`` decorator:

    @tool("create_task", "Create a to-do task / reminder for the user",
          args={"title": "short title without time words", "due": "ISO-8601 datetime or null"},
          sections=("personal", "business"),
          examples=["remind me to eat dinner at 9pm", "add a task to call the bank tomorrow"])
    async def create_task(ctx: ToolCtx, args: dict) -> ToolResult: ...

The description + examples are embedded into ``intent_vectors`` (kind="tool").
For each message the agent embeds the text once and asks pgvector for the
nearest tools; only tools above the similarity threshold are offered to the
JSON-schema constrained LLM router (if none qualify the router is skipped).
Tools return a ToolResult whose ``text`` feeds the final answer and whose
``cards`` are rendered in the chat UI (see static/js/chat.js registerCard).
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chat, ChatHistory, User, UserSettings

log = logging.getLogger("deminic.tools")


@dataclass
class ToolResult:
    ok: bool = True
    text: str = ""
    cards: list[dict] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    system_addendum: str = ""
    reply: str | None = None  # final reply verbatim (skips the LLM answer step)
    lawyer: bool = False  # switch the answer into lawyer mode
    escalate: bool = False  # force escalation to an online provider


@dataclass
class ToolCtx:
    db: AsyncSession
    user: User
    settings: UserSettings
    section: str
    chat: Chat
    history: ChatHistory
    msg_no: int
    request_id: str
    text: str
    location: dict | None
    place: str
    log: Callable[[str], Awaitable[None]]
    emit: Callable[[str, dict], Awaitable[None]]
    vec: list[float] = field(default_factory=list)
    embed_model: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> int:
        return self.user.id

    @property
    def tz(self) -> str:
        return self.user.timezone or "UTC"

    @property
    def chat_no(self) -> int:
        return self.chat.chat_no

    @property
    def ref(self) -> str:
        return f"chat#{self.chat.chat_no}.{self.msg_no}"


ToolFn = Callable[[ToolCtx, dict], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    args: dict[str, str]
    sections: tuple[str, ...]
    fn: ToolFn
    examples: list[str] = field(default_factory=list)

    def catalog_line(self) -> str:
        a = ", ".join(f"{k}: {v}" for k, v in self.args.items()) or "no args"
        return f"- {self.name}({a}) - {self.description}"


TOOLS: dict[str, ToolSpec] = {}


def tool(name: str, description: str, *, args: dict[str, str] | None = None,
         sections: tuple[str, ...] = ("personal", "business", "coding"), examples: list[str] | None = None):
    def deco(fn: ToolFn) -> ToolFn:
        TOOLS[name] = ToolSpec(name, description, args or {}, tuple(sections), fn, list(examples or []))
        return fn

    return deco


def prototypes() -> dict[str, list[str]]:
    """Texts embedded into intent_vectors for semantic tool selection."""
    return {t.name: [t.description] + t.examples for t in TOOLS.values()}


async def select_tools(db: AsyncSession, section: str, vec: list[float], model: str,
                       max_tools: int = 6) -> tuple[list[ToolSpec], list[tuple[str, float]]]:
    from app.core import intents

    from app.core.embeddings import threshold

    names = [t.name for t in TOOLS.values() if section in t.sections]
    scored = await intents.classify(db, vec, model, "tool", names=names, k=max_tools + 4)  # one pgvector query
    floor = threshold(model, "tool")
    chosen = [TOOLS[n] for n, s in scored if s >= floor and n in TOOLS][:max_tools]
    return chosen, scored


async def execute(ctx: ToolCtx, name: str, args: Any) -> ToolResult:
    spec = TOOLS.get(name)
    if spec is None or ctx.section not in spec.sections:
        return ToolResult(ok=False, text=f"unknown tool {name}")
    if not isinstance(args, dict):
        args = {}
    try:
        return await spec.fn(ctx, args)
    except Exception as e:  # noqa: BLE001 - a broken tool must not kill the reply
        log.exception("tool %s failed", name)
        await ctx.log(f"tool {name} failed: {type(e).__name__}: {e}")
        try:
            await ctx.db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(ok=False, text=f"Tool {name} failed: {type(e).__name__}: {str(e)[:200]}")


def load_all() -> None:
    for mod in ("common", "personal", "business", "coding"):
        try:
            importlib.import_module(f"app.ai.tools.{mod}")
        except ModuleNotFoundError as e:
            if e.name != f"app.ai.tools.{mod}":
                raise
            log.warning("tool module %s not present", mod)
    from app.core import intents

    intents.set_tool_prototypes(prototypes())
