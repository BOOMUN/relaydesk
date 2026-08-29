from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import settings
from .database import Base
from .vector_types import EmbeddingVector


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"
    KNOWLEDGE_MANAGER = "knowledge_manager"
    ANALYST = "analyst"


class ConversationStatus(StrEnum):
    OPEN = "open"
    PENDING = "pending"
    EXPIRED = "expired"
    SOLVED = "solved"
    BLOCKED = "blocked"


class ConversationPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class ActionRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    PENDING_CONFIRMATION = "pending_confirmation"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class MessageSender(StrEnum):
    CUSTOMER = "customer"
    AI = "ai"
    AGENT = "agent"
    SYSTEM = "system"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(40), default=UserRole.AGENT.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenant: Mapped[Tenant] = relationship()


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentProfile(Base):
    """Tenant-owned AI agent configuration with one reviewed live version."""

    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_agent_profile_tenant"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    active_version_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "agent_profile_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_agent_profile_active_version",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentProfileVersion(Base):
    """A reviewable snapshot of all customer-facing AI agent instructions."""

    __tablename__ = "agent_profile_versions"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "version_number", name="uq_agent_profile_version_number"
        ),
        Index("ix_agent_profile_version_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("agent_profiles.id", ondelete="CASCADE"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    identity: Mapped[str] = mapped_column(Text)
    service_scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    tone: Mapped[str] = mapped_column(Text)
    knowledge_priority: Mapped[list[str]] = mapped_column(JSON, default=list)
    prohibitions: Mapped[list[str]] = mapped_column(JSON, default=list)
    handoff_conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    reply_language: Mapped[str] = mapped_column(String(30), default="auto")
    fallback_language: Mapped[str] = mapped_column(String(16), default="zh-TW")
    order_intake_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    automation_timeout_minutes: Mapped[int] = mapped_column(Integer, default=30)
    web_search_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    web_search_allowed_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    lead_qualification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    instructions: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rollback_from_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_profile_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_team_tenant_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class ChannelAccount(Base):
    """Tenant-owned messaging account backed by Meta, Evolution, or demo."""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_channel_account_tenant_name"),
        Index("ix_channel_account_provider_external", "provider", "external_account_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(120))
    external_account_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    phone_number_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    business_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    instance_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    credentials_reference: Mapped[str] = mapped_column(String(255), default="environment")
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    connection_state: Mapped[str] = mapped_column(String(40), default="unknown")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("tenant_id", "wa_id", name="uq_contact_tenant_wa_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    wa_id: Mapped[str] = mapped_column(String(64), index=True)
    phone: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(160), default="WhatsApp customer")
    language: Mapped[str] = mapped_column(String(16), default="zh-CN")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    custom_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChannelContactIdentity(Base):
    """Provider-specific address for an AgentDesk-owned contact."""

    __tablename__ = "channel_contact_identities"
    __table_args__ = (
        UniqueConstraint(
            "channel_account_id",
            "external_user_id",
            name="uq_channel_contact_account_external",
        ),
        UniqueConstraint(
            "channel_account_id",
            "contact_id",
            name="uq_channel_contact_account_contact",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    channel_account_id: Mapped[int] = mapped_column(
        ForeignKey("channel_accounts.id", ondelete="CASCADE"), index=True
    )
    external_user_id: Mapped[str] = mapped_column(String(255), index=True)
    address: Mapped[str] = mapped_column(String(255))
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversation_tenant_status_updated", "tenant_id", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(30), default="whatsapp")
    channel_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default=ConversationStatus.OPEN.value, index=True)
    priority: Mapped[str] = mapped_column(String(20), default=ConversationPriority.NORMAL.value)
    subject: Mapped[str] = mapped_column(String(255), default="New WhatsApp conversation")
    assigned_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_route: Mapped[str | None] = mapped_column(String(50), nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    service_window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    contact: Mapped[Contact] = relationship()
    assigned_team: Mapped[Team | None] = relationship(foreign_keys=[assigned_team_id])
    assigned_user: Mapped[User | None] = relationship(foreign_keys=[assigned_user_id])
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("tenant_id", "external_id", name="uq_message_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    channel_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    direction: Mapped[str] = mapped_column(String(20))
    sender_type: Mapped[str] = mapped_column(String(20))
    sender_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content_type: Mapped[str] = mapped_column(String(30), default="text")
    body: Mapped[str] = mapped_column(Text, default="")
    delivery_status: Mapped[str] = mapped_column(String(30), default="received")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class MessageDeliveryReceipt(Base):
    """Latest provider receipt, retained even if it arrives before the message row."""

    __tablename__ = "message_delivery_receipts"

    external_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    delivery_status: Mapped[str] = mapped_column(String(30))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageDeliveryAttempt(Base):
    """Immutable send-attempt history for an outbound message.

    A message can be retried with a new provider ID. Keeping attempts separate
    lets a late receipt for an older attempt still update the logical message.
    """

    __tablename__ = "message_delivery_attempts"
    __table_args__ = (
        UniqueConstraint("message_id", "attempt_number", name="uq_message_delivery_attempt"),
        UniqueConstraint("provider", "external_id", name="uq_delivery_attempt_provider_external"),
        Index("ix_delivery_attempt_message_created", "message_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    channel_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(30))
    attempt_number: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(30), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChannelWebhookEvent(Base):
    """Durable inbox record used to authenticate, deduplicate, and replay webhooks."""

    __tablename__ = "channel_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "provider", "account_key", "event_key", name="uq_channel_webhook_event"
        ),
        Index("ix_channel_webhook_event_status_received", "status", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    channel_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(30), index=True)
    account_key: Mapped[str] = mapped_column(String(255), index=True)
    event_key: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="received", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WhatsAppTemplate(Base):
    """Locally synchronized Meta template; approval remains owned by Meta."""

    __tablename__ = "whatsapp_templates"
    __table_args__ = (
        UniqueConstraint(
            "channel_account_id", "name", "language", name="uq_whatsapp_template_name_language"
        ),
        Index("ix_whatsapp_template_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    channel_account_id: Mapped[int] = mapped_column(
        ForeignKey("channel_accounts.id", ondelete="CASCADE"), index=True
    )
    provider_template_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(512), index=True)
    language: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(80), default="UTILITY")
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)
    parameter_format: Mapped[str] = mapped_column(String(30), default="POSITIONAL")
    components: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    quality_rating: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WhatsAppTemplateSyncRun(Base):
    __tablename__ = "whatsapp_template_sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    channel_account_id: Mapped[int] = mapped_column(
        ForeignKey("channel_accounts.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    template_count: Mapped[int] = mapped_column(Integer, default=0)
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ActionExecution(Base):
    """Durable, idempotent request for one approved business capability."""

    __tablename__ = "action_executions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "action_name", "idempotency_key", name="uq_action_idempotency"
        ),
        Index("ix_action_execution_status_created", "status", "created_at"),
        Index("ix_action_execution_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    action_name: Mapped[str] = mapped_column(String(120), index=True)
    action_version: Mapped[int] = mapped_column(Integer, default=1)
    purpose: Mapped[str] = mapped_column(Text, default="")
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requested_by_type: Mapped[str] = mapped_column(String(30), index=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    risk_level: Mapped[str] = mapped_column(String(20), index=True)
    permission_scope: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(
        String(30), default=ActionStatus.PROPOSED.value, index=True
    )
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ActionAttempt(Base):
    __tablename__ = "action_attempts"
    __table_args__ = (
        UniqueConstraint(
            "action_execution_id", "attempt_number", name="uq_action_attempt_number"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    action_execution_id: Mapped[str] = mapped_column(
        ForeignKey("action_executions.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="running")
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AutomationFormSession(Base):
    """Durable state for one LangGraph-driven customer data collection flow."""

    __tablename__ = "automation_form_sessions"
    __table_args__ = (
        Index(
            "ix_automation_form_conversation_status",
            "conversation_id",
            "status",
            "updated_at",
        ),
        Index("ix_automation_form_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    agent_profile_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_profile_versions.id", ondelete="SET NULL"), nullable=True
    )
    workflow_key: Mapped[str] = mapped_column(String(80), index=True)
    operation: Mapped[str] = mapped_column(String(80), default="inquiry", index=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    answers_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AutomationFormEvent(Base):
    """Append-only transition and answer history for a form session."""

    __tablename__ = "automation_form_events"
    __table_args__ = (
        Index("ix_automation_form_event_session_created", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("automation_form_sessions.id", ondelete="CASCADE"), index=True
    )
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    field_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdentityVerification(Base):
    """Human-recorded identity check; raw evidence is never persisted."""

    __tablename__ = "identity_verifications"
    __table_args__ = (
        Index(
            "ix_identity_verification_contact_status_expiry",
            "contact_id",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="verified", index=True)
    method: Mapped[str] = mapped_column(String(80))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    evidence_hint: Mapped[str] = mapped_column(String(120), default="")
    verified_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SensitiveOperationRequest(Base):
    """Human-confirmed request record; it never performs the external mutation."""

    __tablename__ = "sensitive_operation_requests"
    __table_args__ = (
        UniqueConstraint(
            "action_execution_id", name="uq_sensitive_operation_action_execution"
        ),
        Index(
            "ix_sensitive_operation_conversation_created",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    action_execution_id: Mapped[str] = mapped_column(
        ForeignKey("action_executions.id", ondelete="CASCADE"), index=True
    )
    identity_verification_id: Mapped[str] = mapped_column(
        ForeignKey("identity_verifications.id", ondelete="RESTRICT"), index=True
    )
    operation: Mapped[str] = mapped_column(String(80), index=True)
    order_number: Mapped[str] = mapped_column(String(120))
    requested_changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        String(40), default="approved_for_manual_execution", index=True
    )
    approved_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RestActionEndpoint(Base):
    """Administrator-approved outbound REST capability and encrypted credential."""

    __tablename__ = "rest_action_endpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_rest_action_endpoint_name"),
        Index("ix_rest_action_endpoint_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    base_url: Mapped[str] = mapped_column(Text)
    path_pattern: Mapped[str] = mapped_column(String(500))
    allowed_methods: Mapped[list[str]] = mapped_column(JSON, default=list)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    requires_identity_verification: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    secret_header_name: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_fingerprint: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class KnowledgeSource(Base):
    """A public website crawl configured by a knowledge manager."""

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "root_url", name="uq_knowledge_source_root"),
        Index("ix_knowledge_source_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    root_url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    max_pages: Mapped[int] = mapped_column(Integer, default=500)
    max_depth: Mapped[int] = mapped_column(Integer, default=5)
    discovered_pages: Mapped[int] = mapped_column(Integer, default=0)
    imported_pages: Mapped[int] = mapped_column(Integer, default=0)
    failed_pages: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    pages: Mapped[list[KnowledgeWebPage]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )
    sync_runs: Mapped[list[KnowledgeSyncRun]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(255), default="manual")
    category: Mapped[str] = mapped_column(String(80), default="general")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    web_page: Mapped[KnowledgeWebPage | None] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        uselist=False,
    )
    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class KnowledgeWebPage(Base):
    """Crawl provenance and review state for one stored knowledge document."""

    __tablename__ = "knowledge_web_pages"
    __table_args__ = (
        UniqueConstraint("source_id", "url", name="uq_knowledge_page_source_url"),
        Index("ix_knowledge_page_tenant_review", "tenant_id", "review_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), unique=True, index=True
    )
    url: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str] = mapped_column(String(30), default="html")
    language: Mapped[str] = mapped_column(String(16), default="unknown")
    review_status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    source: Mapped[KnowledgeSource] = relationship(back_populates="pages")
    document: Mapped[KnowledgeDocument] = relationship(back_populates="web_page")
    revisions: Mapped[list[KnowledgePageRevision]] = relationship(
        back_populates="web_page",
        cascade="all, delete-orphan",
    )
    sync_state: Mapped[KnowledgePageSyncState | None] = relationship(
        back_populates="web_page",
        cascade="all, delete-orphan",
        uselist=False,
    )


class KnowledgePageRevision(Base):
    """A crawled change waiting for review while the live document stays active."""

    __tablename__ = "knowledge_page_revisions"
    __table_args__ = (
        UniqueConstraint("web_page_id", "content_hash", name="uq_knowledge_revision_hash"),
        Index("ix_knowledge_revision_source_status", "source_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    web_page_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_web_pages.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(80), default="other")
    content_type: Mapped[str] = mapped_column(String(30), default="html")
    language: Mapped[str] = mapped_column(String(16), default="unknown")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    web_page: Mapped[KnowledgeWebPage] = relationship(back_populates="revisions")


class KnowledgePageSyncState(Base):
    """Tracks whether a previously known page is still present on the website."""

    __tablename__ = "knowledge_page_sync_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    web_page_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_web_pages.id", ondelete="CASCADE"), unique=True, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    consecutive_missing: Mapped[int] = mapped_column(Integer, default=0)
    availability_status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    suspected_missing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    web_page: Mapped[KnowledgeWebPage] = relationship(back_populates="sync_state")


class KnowledgeSyncRun(Base):
    """Durable audit record for an initial, manual, scheduled, or retry crawl."""

    __tablename__ = "knowledge_sync_runs"
    __table_args__ = (
        Index("ix_knowledge_sync_run_source_queued", "source_id", "queued_at"),
        Index("ix_knowledge_sync_run_status_available", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    trigger: Mapped[str] = mapped_column(String(30), default="manual")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    new_pages: Mapped[int] = mapped_column(Integer, default=0)
    changed_pages: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_pages: Mapped[int] = mapped_column(Integer, default=0)
    missing_pages: Mapped[int] = mapped_column(Integer, default=0)
    failed_pages: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[KnowledgeSource] = relationship(back_populates="sync_runs")


class KnowledgeChunk(Base):
    """A persisted RAG chunk and its embedding for local development retrieval."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_index"),
        Index("ix_knowledge_chunk_tenant_document", "tenant_id", "document_id"),
        Index(
            "ix_knowledge_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_knowledge_chunk_tenant_model_document",
            "tenant_id",
            "embedding_model",
            "document_id",
        ),
        Index("ix_knowledge_chunk_embedding_model", "embedding_model"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    page_title: Mapped[str] = mapped_column(String(255), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    section_path: Mapped[str] = mapped_column(Text, default="")
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # PostgreSQL uses vector(embedding_dimensions); SQLite keeps JSON solely
    # for the deterministic local test/demo database.  The dimension is fixed
    # at schema creation time and validated again before writes.
    embedding: Mapped[list[float]] = mapped_column(EmbeddingVector(), default=list)
    embedding_model: Mapped[str] = mapped_column(
        String(255), default=lambda: settings.configured_embedding_model
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


class ProductPriceSource(Base):
    """A public product catalogue whose prices are synchronized automatically."""

    __tablename__ = "product_price_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "root_url", name="uq_product_price_source_root"),
        Index("ix_product_price_source_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160))
    root_url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255))
    adapter: Mapped[str] = mapped_column(String(40), default="auto")
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_pages: Mapped[int] = mapped_column(Integer, default=100)
    discovered_products: Mapped[int] = mapped_column(Integer, default=0)
    imported_products: Mapped[int] = mapped_column(Integer, default=0)
    imported_offers: Mapped[int] = mapped_column(Integer, default=0)
    failed_pages: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    products: Mapped[list[Product]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )
    sync_runs: Mapped[list[ProductPriceSyncRun]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )


class Product(Base):
    """One customer-facing product or destination within a catalogue source."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("source_id", "external_key", name="uq_product_source_external"),
        Index("ix_product_tenant_active_category", "tenant_id", "is_active", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("product_price_sources.id", ondelete="CASCADE"), index=True
    )
    external_key: Mapped[str] = mapped_column(String(255))
    canonical_url: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255))
    name_translations: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(80), default="other", index=True)
    product_type: Mapped[str] = mapped_column(String(80), default="product")
    destination: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    network: Mapped[str | None] = mapped_column(String(30), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    consecutive_missing: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    source: Mapped[ProductPriceSource] = relationship(back_populates="products")
    offers: Mapped[list[ProductPriceOffer]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductPriceOffer.id",
    )


class ProductPriceOffer(Base):
    """A quoteable price variant, such as daily rental or an eSIM data plan."""

    __tablename__ = "product_price_offers"
    __table_args__ = (
        UniqueConstraint("product_id", "external_key", name="uq_product_offer_external"),
        Index("ix_product_offer_tenant_active", "tenant_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("product_price_sources.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    external_key: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(255), default="标准价格")
    currency: Mapped[str] = mapped_column(String(12), default="HKD")
    price_amount: Mapped[Any] = mapped_column(Numeric(12, 2))
    original_amount: Mapped[Any | None] = mapped_column(Numeric(12, 2), nullable=True)
    unit: Mapped[str] = mapped_column(String(40), default="item")
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    promo_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    availability: Mapped[str] = mapped_column(String(40), default="in_stock")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    consecutive_missing: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    product: Mapped[Product] = relationship(back_populates="offers")
    history: Mapped[list[ProductPriceHistory]] = relationship(
        back_populates="offer",
        cascade="all, delete-orphan",
        order_by="ProductPriceHistory.observed_at.desc()",
    )


class ProductPriceHistory(Base):
    """Immutable price snapshots for audit and change review."""

    __tablename__ = "product_price_history"
    __table_args__ = (Index("ix_product_price_history_offer_observed", "offer_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int] = mapped_column(
        ForeignKey("product_price_offers.id", ondelete="CASCADE"), index=True
    )
    change_type: Mapped[str] = mapped_column(String(30), default="changed")
    currency: Mapped[str] = mapped_column(String(12), default="HKD")
    price_amount: Mapped[Any] = mapped_column(Numeric(12, 2))
    original_amount: Mapped[Any | None] = mapped_column(Numeric(12, 2), nullable=True)
    unit: Mapped[str] = mapped_column(String(40), default="item")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    offer: Mapped[ProductPriceOffer] = relationship(back_populates="history")


class ProductPriceSyncRun(Base):
    """Durable audit record for a product catalogue synchronization."""

    __tablename__ = "product_price_sync_runs"
    __table_args__ = (
        Index("ix_product_price_run_source_queued", "source_id", "queued_at"),
        Index("ix_product_price_run_status_available", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("product_price_sources.id", ondelete="CASCADE"), index=True
    )
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    trigger: Mapped[str] = mapped_column(String(30), default="manual")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    new_products: Mapped[int] = mapped_column(Integer, default=0)
    new_offers: Mapped[int] = mapped_column(Integer, default=0)
    changed_offers: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_offers: Mapped[int] = mapped_column(Integer, default=0)
    missing_products: Mapped[int] = mapped_column(Integer, default=0)
    failed_pages: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[ProductPriceSource] = relationship(back_populates="sync_runs")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class QuickReply(Base):
    """A reusable agent response kept per tenant."""

    __tablename__ = "quick_replies"
    __table_args__ = (UniqueConstraint("tenant_id", "shortcut", name="uq_quick_reply_tenant_shortcut"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    shortcut: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16), default="zh-CN")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
