"""Complete relational schema.

Every user-owned table carries ``user_id`` and every query MUST filter on it
(tenant isolation). Per-user human-friendly numbers (chat#N, task#N, ...) are
allocated from ``user_counters`` so references never reveal other accounts.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.crypto_types import EncryptedJSON, EncryptedMessages, EncryptedText
from app.db import Base

EMBED_DIM = 384


def _uuid() -> str:
    return str(uuid.uuid4())


def _now_col(**kw):
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    ai_name: Mapped[str] = mapped_column(String(40), default="Deminic")
    avatar_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    about: Mapped[str] = mapped_column(Text, default="")
    owns_business: Mapped[bool] = mapped_column(Boolean, default=False)
    business_name: Mapped[str] = mapped_column(String(120), default="")
    business_description: Mapped[str] = mapped_column(Text, default="")
    business_version: Mapped[int] = mapped_column(Integer, default=1)
    theme: Mapped[str] = mapped_column(String(10), default="system")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    password_changed_at: Mapped[datetime] = _now_col()
    # One login per account: SHA-256 of the ONLY valid session token (set at login).
    # The middleware rejects any session whose token hash differs.
    session_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())

    # Always fetched in the SAME query via LEFT OUTER JOIN (no N+1, no lazy IO in async code).
    settings: Mapped["UserSettings"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="joined"
    )


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # Local gateway (OpenAI compatible) - e.g. http://<gateway-ip>:8000/v1
    gateway_url: Mapped[str] = mapped_column(String(255), default="")
    gateway_key_enc: Mapped[str] = mapped_column(Text, default="")
    local_model: Mapped[str] = mapped_column(String(80), default="gemma")
    coder_model: Mapped[str] = mapped_column(String(80), default="qwen")
    use_docker_fallback: Mapped[bool] = mapped_column(Boolean, default=True)
    # Online providers (used for BEYOND MY LIMITS escalation, coding and last-resort failover)
    claude_key_enc: Mapped[str] = mapped_column(Text, default="")
    claude_model: Mapped[str] = mapped_column(String(80), default="claude-opus-5-5")
    openai_key_enc: Mapped[str] = mapped_column(Text, default="")
    openai_model: Mapped[str] = mapped_column(String(80), default="gpt-4.1")
    github_models_key_enc: Mapped[str] = mapped_column(Text, default="")
    github_models_model: Mapped[str] = mapped_column(String(80), default="openai/gpt-4.1")
    provider_order: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSONB), default=lambda: ["claude", "openai", "github"]
    )
    coding_provider: Mapped[str] = mapped_column(String(20), default="auto")
    # SMTP (user's preferred sending mailbox)
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_username: Mapped[str] = mapped_column(String(255), default="")
    smtp_password_enc: Mapped[str] = mapped_column(Text, default="")
    smtp_from_email: Mapped[str] = mapped_column(String(254), default="")
    smtp_security: Mapped[str] = mapped_column(String(10), default="starttls")
    # GitHub account link (repos, clone, push new branches only)
    github_token_enc: Mapped[str] = mapped_column(Text, default="")
    github_login: Mapped[str] = mapped_column(String(100), default="")
    # Optional extras
    youtube_api_key_enc: Mapped[str] = mapped_column(Text, default="")
    # TMDB key -> "where to watch" (Netflix / Prime Video / ...) via JustWatch data
    tmdb_api_key_enc: Mapped[str] = mapped_column(Text, default="")
    watch_region: Mapped[str] = mapped_column(String(4), default="IN")
    news_region: Mapped[str] = mapped_column(String(8), default="US")
    share_location: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())

    user: Mapped[User] = relationship(back_populates="settings", lazy="raise")


class UserCounter(Base):
    """Per-user sequences for human friendly references (chat#3, task#7, ...)."""

    __tablename__ = "user_counters"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Chats - every message lives in a JSONB MutableList of dicts
#   {"chatno": 1, "prompt": "...", "reply": "...", "status": "done", ...}
# compacted_history is a TEXT column rebuilt on every new message from the
# previous prompt + reply pairs.
# ---------------------------------------------------------------------------
class Chat(Base):
    """Chat metadata. The history itself lives in the separate chat_histories table
    (no ORM relationship: always query explicitly by user_id; FK cascades delete)."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chat_no: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(12))
    title: Mapped[str] = mapped_column(EncryptedText, default="New chat")
    lawyer_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    main_points: Mapped[list] = mapped_column(MutableList.as_mutable(EncryptedJSON), default=list)
    business_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "chat_no", name="uq_chat_user_no"),
        Index("ix_chats_user_section_updated", "user_id", "section", "updated_at"),
    )


class ChatHistory(Base):
    """Separate chat-history table (1:1 with chats).

    ``messages`` is a JSONB MutableList of dicts in the format
    {"chatno": 1, "prompt": "...", "reply": "...", "status": ..., ...}.
    ``compacted_history`` is the TEXT column rebuilt on every new message from
    the previous prompt + reply pairs (what the LLM actually sees).
    Both are encrypted at rest (per-message content fields / whole text).
    """

    __tablename__ = "chat_histories"

    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    messages: Mapped[list] = mapped_column(MutableList.as_mutable(EncryptedMessages), default=list)
    compacted_history: Mapped[str] = mapped_column(EncryptedText, default="")
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


# ---------------------------------------------------------------------------
# Global memory (per section). Contacts / family profiles are memories with
# category="contact" and structured ``data``. key/value/data are encrypted at
# rest; ``key_hash`` (keyed HMAC) provides uniqueness + lookups.
# ---------------------------------------------------------------------------
class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    section: Mapped[str] = mapped_column(String(12))
    category: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(EncryptedText)
    key_hash: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(EncryptedText, default="")
    data: Mapped[dict] = mapped_column(MutableDict.as_mutable(EncryptedJSON), default=dict)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_msg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_job: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        Index("ix_mem_user_section", "user_id", "section"),
        Index(
            "uq_mem_active_key",
            "user_id",
            "section",
            "category",
            "key_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class MemoryVector(Base):
    """pgvector store used for semantic recall. Clients/reminders are NOT stored
    here as tensors - they live in their own relational tables."""

    __tablename__ = "memory_vectors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    section: Mapped[str] = mapped_column(String(12))
    source_type: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(EncryptedText)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    embed_model: Mapped[str] = mapped_column(String(40), default="all-minilm")
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (
        Index("ix_vec_user_section", "user_id", "section", "embed_model"),
        Index("ix_vec_source", "user_id", "source_type", "source_id"),
        Index(
            "ix_vec_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class IntentVector(Base):
    """Global (non user) semantic prototypes used instead of regex routing:
    tool intents, memory categories, profile/photo questions, summary and
    auto-memory triggers and prompt-injection patterns."""

    __tablename__ = "intent_vectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    embed_model: Mapped[str] = mapped_column(String(40))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))

    __table_args__ = (
        Index("ix_intent_kind_model", "kind", "embed_model"),
        UniqueConstraint("kind", "name", "text", "embed_model", name="uq_intent_proto"),
    )


# ---------------------------------------------------------------------------
# Personal productivity
# ---------------------------------------------------------------------------
class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_no: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(12), default="personal")
    title: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # active | queued | done | cancelled
    status: Mapped[str] = mapped_column(String(12), default="active")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    queued_behind_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    location_name: Mapped[str] = mapped_column(String(200), default="")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_msg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reminder_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reminder_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "task_no", name="uq_task_user_no"),)


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_no: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(12), default="personal")
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    location: Mapped[str] = mapped_column(String(255), default="")
    meet_link: Mapped[str] = mapped_column(String(255), default="")
    needs_meet_link: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_msg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (UniqueConstraint("user_id", "event_no", name="uq_event_user_no"),)


# ---------------------------------------------------------------------------
# Business (relational tables - NOT AI tensors)
# ---------------------------------------------------------------------------
class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(160))
    company: Mapped[str] = mapped_column(String(160), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    reason_added: Mapped[str] = mapped_column(Text, default="")
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_msg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (UniqueConstraint("user_id", "client_no", name="uq_client_user_no"),)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reminder_no: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(12), default="business")
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(EncryptedText)
    amount: Mapped[str] = mapped_column(EncryptedText, default="")  # decimal string, encrypted (financial)
    currency: Mapped[str] = mapped_column(String(8), default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # pending | done | cancelled
    status: Mapped[str] = mapped_column(String(12), default="pending")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_msg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (UniqueConstraint("user_id", "reminder_no", name="uq_reminder_user_no"),)


class Transaction(Base):
    """Credit / debit statements, each referencing the chat it came from.
    Financial fields are encrypted at rest - totals are computed in Python."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    txn_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(8))  # credit | debit
    amount: Mapped[str] = mapped_column(EncryptedText)  # decimal string, encrypted (financial)
    currency: Mapped[str] = mapped_column(String(8), default="")
    counterparty: Mapped[str] = mapped_column(EncryptedText, default="")
    description: Mapped[str] = mapped_column(EncryptedText, default="")
    occurred_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_msg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (UniqueConstraint("user_id", "txn_no", name="uq_txn_user_no"),)


class Email(Base):
    """Outbox / "sent inbox": drafts, sent and failed mails with chat reference."""

    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email_no: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(12), default="personal")
    kind: Mapped[str] = mapped_column(String(16), default="general")  # cold_sales | family | general
    to_email: Mapped[str] = mapped_column(String(254), default="")
    to_name: Mapped[str] = mapped_column(String(160), default="")
    subject: Mapped[str] = mapped_column(EncryptedText, default="")
    body: Mapped[str] = mapped_column(EncryptedText, default="")
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft | sent | failed
    error: Mapped[str] = mapped_column(Text, default="")
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_msg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (UniqueConstraint("user_id", "email_no", name="uq_email_user_no"),)


class Summary(Base):
    """AI summaries: type='business' (finance/status) or type='personal' (mind map, challenges)."""

    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(12))
    content: Mapped[str] = mapped_column(EncryptedText, default="")
    data: Mapped[dict] = mapped_column(MutableDict.as_mutable(EncryptedJSON), default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    business_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_msg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (Index("ix_summaries_user_type", "user_id", "type", "is_current"),)


class NewsDigest(Base):
    __tablename__ = "news_digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    digest_date: Mapped[date] = mapped_column(Date)
    headlines: Mapped[list] = mapped_column(JSONB, default=list)
    analysis: Mapped[str] = mapped_column(Text, default="")
    opportunities: Mapped[list] = mapped_column(JSONB, default=list)
    world: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (UniqueConstraint("user_id", "digest_date", name="uq_news_user_date"),)


class Simulation(Base):
    __tablename__ = "simulations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scenario: Mapped[str] = mapped_column(EncryptedText)
    result: Mapped[dict] = mapped_column(EncryptedJSON, default=dict)
    source_chat_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _now_col()


# ---------------------------------------------------------------------------
# Background jobs + restore points (Windows-restore-point style rollback)
# ---------------------------------------------------------------------------
class BackgroundJob(Base):
    __tablename__ = "background_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200), default="")
    # queued | running | completed | failed | cancelled | reverted
    status: Mapped[str] = mapped_column(String(16), default="running")
    cancellable: Mapped[bool] = mapped_column(Boolean, default=True)
    cancel_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    restore_point_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    meta: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSONB), default=dict)
    created_at: Mapped[datetime] = _now_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_jobs_user_status", "user_id", "status"),)


class RestorePoint(Base):
    __tablename__ = "restore_points"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    # active (job running) | committed | restored | restore_failed
    status: Mapped[str] = mapped_column(String(16), default="active")
    ops: Mapped[list] = mapped_column(MutableList.as_mutable(JSONB), default=list)
    created_at: Mapped[datetime] = _now_col()
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now_col()


# ---------------------------------------------------------------------------
# Coding
# ---------------------------------------------------------------------------
class FileVersion(Base):
    """Timestamped snapshot of a workspace file (undo history + restore points)."""

    __tablename__ = "file_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    existed: Mapped[bool] = mapped_column(Boolean, default=True)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(32), default="save")
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = _now_col()

    __table_args__ = (Index("ix_filever_user_path", "user_id", "path"),)


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    gh_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(120))
    owner: Mapped[str] = mapped_column(String(100))
    html_url: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    fork: Mapped[bool] = mapped_column(Boolean, default=False)
    default_branch: Mapped[str] = mapped_column(String(120), default="main")
    language: Mapped[str] = mapped_column(String(60), default="")
    stars: Mapped[int] = mapped_column(Integer, default=0)
    forks: Mapped[int] = mapped_column(Integer, default=0)
    watchers: Mapped[int] = mapped_column(Integer, default=0)
    open_issues: Mapped[int] = mapped_column(Integer, default=0)
    size_kb: Mapped[int] = mapped_column(Integer, default=0)
    clones_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clones_uniques: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views_uniques: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cloned_path: Mapped[str] = mapped_column(String(512), default="")
    # none | cloning | clean | infected | error
    clone_status: Mapped[str] = mapped_column(String(12), default="none")
    scan_report: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSONB), default=dict)
    branches_pushed: Mapped[list] = mapped_column(MutableList.as_mutable(JSONB), default=list)

    __table_args__ = (UniqueConstraint("user_id", "gh_id", name="uq_repo_user_gh"),)


class VulnScan(Base):
    __tablename__ = "vuln_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    target: Mapped[str] = mapped_column(String(512), default="")
    kind: Mapped[str] = mapped_column(String(12), default="manual")  # gate | manual | repo
    verdict: Mapped[str] = mapped_column(String(8), default="go")  # go | block | error
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = _now_col()


class LocustRun(Base):
    __tablename__ = "locust_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_url: Mapped[str] = mapped_column(String(500))
    users: Mapped[int] = mapped_column(Integer, default=10)
    spawn_rate: Mapped[float] = mapped_column(Float, default=2.0)
    duration_s: Mapped[int] = mapped_column(Integer, default=30)
    paths: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(12), default="running")
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
