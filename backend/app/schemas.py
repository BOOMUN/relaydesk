from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contact_attributes import public_contact_attributes


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    email: str
    password: str


class UserResponse(ApiModel):
    id: int
    tenant_id: int
    name: str
    email: str
    role: str


class IntegrationStatus(BaseModel):
    openai: bool
    whatsapp: bool
    whatsapp_provider: Literal["demo", "meta", "evolution"]
    mode: Literal["demo", "live"]


class BootstrapResponse(BaseModel):
    app_name: str
    user: UserResponse
    integration: IntegrationStatus


class LeadQualificationOption(BaseModel):
    value: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    score: int = Field(default=0, ge=-100, le=100)


class LeadQualificationQuestion(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    prompt: str = Field(min_length=2, max_length=500)
    prompt_en: str | None = Field(default=None, min_length=2, max_length=500)
    kind: Literal["text", "single_choice", "number"] = "text"
    required: bool = True
    default_score: int = Field(default=0, ge=-100, le=100)
    options: list[LeadQualificationOption] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_options(self):
        if self.kind == "single_choice" and not self.options:
            raise ValueError("单选问题必须配置选项")
        values = [item.value.casefold() for item in self.options]
        if len(values) != len(set(values)):
            raise ValueError("同一问题的选项值不能重复")
        return self


class LeadQualificationGrade(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    min_score: int = Field(ge=-10000, le=10000)
    tag: str | None = Field(default=None, min_length=1, max_length=80)
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    team_id: int | None = Field(default=None, ge=1)
    user_id: int | None = Field(default=None, ge=1)


class LeadQualificationConfiguration(BaseModel):
    enabled: bool = False
    trigger_terms: list[str] = Field(default_factory=list, max_length=30)
    questions: list[LeadQualificationQuestion] = Field(default_factory=list, max_length=20)
    grades: list[LeadQualificationGrade] = Field(default_factory=list, max_length=10)

    @field_validator("trigger_terms")
    @classmethod
    def normalize_trigger_terms(cls, value: list[str]) -> list[str]:
        normalized = list(
            dict.fromkeys(str(item).strip() for item in value if str(item).strip())
        )
        if any(len(item) > 120 for item in normalized):
            raise ValueError("线索触发词不能超过 120 个字符")
        return normalized

    @model_validator(mode="after")
    def validate_configuration(self):
        question_ids = [item.id for item in self.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("线索问题 ID 不能重复")
        grade_names = [item.name.casefold() for item in self.grades]
        if len(grade_names) != len(set(grade_names)):
            raise ValueError("线索等级名称不能重复")
        thresholds = [item.min_score for item in self.grades]
        if len(thresholds) != len(set(thresholds)):
            raise ValueError("线索等级最低分不能重复")
        if self.enabled and (not self.trigger_terms or not self.questions or not self.grades):
            raise ValueError("启用线索资格前必须配置触发词、问题和等级")
        return self


class AgentProfileDraftUpdate(BaseModel):
    identity: str | None = Field(default=None, min_length=2, max_length=2000)
    service_scope: list[str] | None = Field(default=None, min_length=1, max_length=30)
    tone: str | None = Field(default=None, min_length=2, max_length=2000)
    knowledge_priority: list[str] | None = Field(
        default=None, min_length=1, max_length=20
    )
    prohibitions: list[str] | None = Field(default=None, min_length=1, max_length=30)
    handoff_conditions: list[str] | None = Field(
        default=None, min_length=1, max_length=30
    )
    reply_language: Literal["auto"] | None = None
    fallback_language: Literal["zh-CN", "zh-TW", "en"] | None = None
    order_intake_enabled: bool | None = None
    automation_timeout_minutes: int | None = Field(default=None, ge=5, le=1440)
    web_search_enabled: bool | None = None
    web_search_allowed_domains: list[str] | None = Field(
        default=None, max_length=30
    )
    lead_qualification: LeadQualificationConfiguration | None = None
    instructions: str | None = Field(default=None, min_length=20, max_length=30000)

    @field_validator(
        "service_scope", "knowledge_priority", "prohibitions", "handoff_conditions"
    )
    @classmethod
    def normalize_agent_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = list(
            dict.fromkeys(str(item).strip() for item in value if str(item).strip())
        )
        if not normalized:
            raise ValueError("至少保留一项配置")
        if any(len(item) > 500 for item in normalized):
            raise ValueError("单项配置不能超过 500 个字符")
        return normalized

    @field_validator("web_search_allowed_domains")
    @classmethod
    def normalize_search_domains(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for raw in value:
            domain = str(raw).strip().casefold().rstrip(".")
            if domain.startswith("*."):
                domain = domain[2:]
            if not domain or len(domain) > 253 or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain
            ):
                raise ValueError(f"无效的搜索来源域名：{raw}")
            normalized.append(domain)
        return list(dict.fromkeys(normalized))


class AgentProfileGenerateRequest(BaseModel):
    source_url: str = Field(min_length=8, max_length=2048)


class AgentProfileVersionResponse(ApiModel):
    id: int
    version_number: int
    status: Literal["draft", "published", "superseded"]
    identity: str
    service_scope: list[str]
    tone: str
    knowledge_priority: list[str]
    prohibitions: list[str]
    handoff_conditions: list[str]
    reply_language: Literal["auto"]
    fallback_language: Literal["zh-CN", "zh-TW", "en"]
    order_intake_enabled: bool
    automation_timeout_minutes: int
    web_search_enabled: bool
    web_search_allowed_domains: list[str]
    lead_qualification: LeadQualificationConfiguration
    instructions: str
    source_url: str | None
    generation_summary: str | None
    created_by_user_id: int | None
    published_by_user_id: int | None
    rollback_from_version_id: int | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


class AgentProfileResponse(BaseModel):
    profile_id: int
    active_version: AgentProfileVersionResponse | None
    draft_version: AgentProfileVersionResponse | None


class AgentProfilePublishResponse(BaseModel):
    active_version: AgentProfileVersionResponse


class WorkspaceResponse(BaseModel):
    max_agent_seats: int
    active_agents: int
    supported_locales: list[str]
    default_locale: str


class WhatsAppConnectionResponse(BaseModel):
    provider: Literal["demo", "meta", "evolution"]
    configured: bool
    state: str
    instance_name: str | None = None
    webhook_url: str | None = None
    qr_code: str | None = None
    message: str | None = None


class TeamResponse(ApiModel):
    id: int
    name: str
    description: str
    is_default: bool


class AgentResponse(ApiModel):
    id: int
    name: str
    email: str
    role: str


class ContactSummary(ApiModel):
    id: int
    wa_id: str
    phone: str
    display_name: str
    language: str
    tags: list[str]
    custom_attributes: dict[str, Any]
    is_blocked: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("custom_attributes", mode="after")
    @classmethod
    def hide_internal_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        return public_contact_attributes(value)


class ContactUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    language: str | None = Field(default=None, max_length=16)
    tags: list[str] | None = None
    custom_attributes: dict[str, Any] | None = None


class MessageResponse(ApiModel):
    id: int
    external_id: str | None
    direction: str
    sender_type: str
    sender_name: str | None
    content_type: str
    body: str
    delivery_status: str
    metadata_json: dict[str, Any]
    created_at: datetime


class MessageTranslationResponse(BaseModel):
    message_id: int
    source_language: Literal["en"] = "en"
    target_language: Literal["zh-TW"] = "zh-TW"
    translated_text: str


class ConversationSummary(BaseModel):
    id: int
    contact: ContactSummary
    status: str
    priority: str
    subject: str
    assigned_team_id: int | None
    assigned_user_id: int | None
    assigned_team: str | None
    assigned_user: str | None
    ai_enabled: bool
    ai_route: str | None
    unread_count: int
    last_message: str
    last_message_sender: str | None
    last_message_at: datetime
    service_window_expires_at: datetime | None


class ConversationDetail(ConversationSummary):
    messages: list[MessageResponse]


class ConversationUpdate(BaseModel):
    status: Literal["open", "pending", "expired", "solved", "blocked"] | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    assigned_team_id: int | None = None
    assigned_user_id: int | None = None
    ai_enabled: bool | None = None
    mark_read: bool = False


class SendMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4096)
    internal: bool = False


class QuickReplyResponse(ApiModel):
    id: int
    shortcut: str
    title: str
    body: str
    language: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class QuickReplyCreate(BaseModel):
    shortcut: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4096)
    language: str = Field(default="zh-TW", max_length=16)


class QuickReplyUpdate(BaseModel):
    shortcut: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    title: str | None = Field(default=None, min_length=1, max_length=160)
    body: str | None = Field(default=None, min_length=1, max_length=4096)
    language: str | None = Field(default=None, max_length=16)
    is_active: bool | None = None


class ConversationActivity(BaseModel):
    id: int
    action: str
    user_name: str | None
    details: dict[str, Any]
    created_at: datetime


class InboxStats(BaseModel):
    all: int
    open: int
    pending: int
    solved: int
    unread: int
    unassigned: int
    mine: int


class SimulateInboundRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=32)
    display_name: str = Field(default="演示客户", min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4096)


class SimulateInboundResponse(BaseModel):
    conversation: ConversationDetail
    agent_route: str
    agent_answered: bool


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    content: str = Field(min_length=10, max_length=500000)
    source: str = Field(default="manual", max_length=2048)
    category: str = Field(default="general", max_length=80)


class KnowledgeUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=255)
    content: str | None = Field(default=None, min_length=10, max_length=500000)
    source: str | None = Field(default=None, max_length=2048)
    category: str | None = Field(default=None, max_length=80)
    is_active: bool | None = None
    review_status: Literal["draft", "published"] | None = None
    pending_revision_id: int | None = None


class KnowledgeResponse(ApiModel):
    id: int
    title: str
    content: str
    source: str
    category: str
    is_active: bool
    source_id: int | None = None
    source_type: Literal["manual", "html", "pdf"] = "manual"
    source_url: str | None = None
    review_status: Literal["draft", "published"] = "published"
    language: str = "unknown"
    word_count: int = 0
    pending_update: bool = False
    pending_revision_id: int | None = None
    pending_title: str | None = None
    pending_content: str | None = None
    pending_category: str | None = None
    availability_status: Literal["active", "missing_once", "suspected_missing"] = "active"
    consecutive_missing: int = 0
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceCreate(BaseModel):
    root_url: str = Field(min_length=8, max_length=2048)
    max_pages: int = Field(default=500, ge=1, le=500)
    max_depth: int = Field(default=5, ge=0, le=5)


class KnowledgeSourceResponse(BaseModel):
    id: int
    root_url: str
    domain: str
    status: Literal["queued", "running", "completed", "partial", "failed"]
    max_pages: int
    max_depth: int
    discovered_pages: int
    imported_pages: int
    failed_pages: int
    draft_pages: int
    published_pages: int
    pending_updates: int = 0
    suspected_removed_pages: int = 0
    error_message: str | None
    auto_sync_enabled: bool = True
    sync_time: str = "03:10"
    sync_timezone: str = "Asia/Shanghai"
    next_sync_at: datetime
    next_retry_at: datetime | None = None
    last_sync_trigger: Literal["initial", "manual", "scheduled", "retry"] | None = None
    last_new_pages: int = 0
    last_changed_pages: int = 0
    last_unchanged_pages: int = 0
    last_missing_pages: int = 0
    last_successful_sync_at: datetime | None = None
    failed_task_count: int = 0
    partial_task_count: int = 0
    last_failed_task_at: datetime | None = None
    last_failure_message: str | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class KnowledgePublishResponse(BaseModel):
    source: KnowledgeSourceResponse
    published_count: int
    published_new_count: int = 0
    published_update_count: int = 0


class ProductPriceSourceCreate(BaseModel):
    root_url: str = Field(min_length=8, max_length=2048)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    max_pages: int = Field(default=100, ge=1, le=500)


class ProductPriceSourceResponse(BaseModel):
    id: int
    name: str
    root_url: str
    domain: str
    adapter: str
    status: Literal["queued", "running", "completed", "partial", "failed"]
    auto_sync_enabled: bool
    max_pages: int
    discovered_products: int
    imported_products: int
    imported_offers: int
    failed_pages: int
    error_message: str | None
    sync_time: str = "03:10"
    sync_timezone: str = "Asia/Shanghai"
    next_sync_at: datetime
    next_retry_at: datetime | None = None
    last_sync_trigger: Literal["initial", "manual", "scheduled", "retry"] | None = None
    last_new_products: int = 0
    last_new_offers: int = 0
    last_changed_offers: int = 0
    last_unchanged_offers: int = 0
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    @field_validator(
        "next_sync_at",
        "next_retry_at",
        "created_at",
        "started_at",
        "completed_at",
        "updated_at",
        mode="after",
    )
    @classmethod
    def attach_utc_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class ProductPriceOfferResponse(ApiModel):
    id: int
    external_key: str
    label: str
    currency: str
    price_amount: Decimal
    original_amount: Decimal | None
    unit: str
    duration_days: int | None
    data_label: str | None
    promo_label: str | None
    availability: str
    metadata_json: dict[str, Any]
    is_active: bool
    last_seen_at: datetime
    updated_at: datetime

    @field_validator("last_seen_at", "updated_at", mode="after")
    @classmethod
    def attach_utc_timezone(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ProductPriceProductResponse(BaseModel):
    id: int
    source_id: int
    source_name: str
    source_url: str
    canonical_url: str
    name: str
    name_translations: dict[str, str]
    aliases: list[str]
    category: str
    product_type: str
    destination: str | None
    network: str | None
    description: str
    metadata_json: dict[str, Any]
    is_active: bool
    last_seen_at: datetime
    updated_at: datetime
    offers: list[ProductPriceOfferResponse]

    @field_validator("last_seen_at", "updated_at", mode="after")
    @classmethod
    def attach_utc_timezone(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ProductPriceHistoryResponse(ApiModel):
    id: int
    change_type: str
    currency: str
    price_amount: Decimal
    original_amount: Decimal | None
    unit: str
    observed_at: datetime

    @field_validator("observed_at", mode="after")
    @classmethod
    def attach_utc_timezone(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class DashboardMetric(BaseModel):
    label: str
    value: int | float
    change: float | None = None
    unit: str | None = None


class DashboardResponse(BaseModel):
    metrics: list[DashboardMetric]
    status_counts: dict[str, int]
    route_counts: dict[str, int]
    recent_conversations: list[ConversationSummary]
