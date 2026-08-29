from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationHandoffInput(ActionInput):
    conversation_id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    target_team_id: int | None = Field(default=None, ge=1)


class ConversationResumeAIInput(ActionInput):
    conversation_id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class ConversationAssignInput(ActionInput):
    conversation_id: int = Field(ge=1)
    team_id: int | None = Field(default=None, ge=1)
    user_id: int | None = Field(default=None, ge=1)


class ContactUpdateProfileInput(ActionInput):
    contact_id: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    phone: str | None = Field(default=None, min_length=3, max_length=64)
    language: Literal["zh-CN", "zh-TW", "en"] | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.display_name is None and self.phone is None and self.language is None:
            raise ValueError("At least one profile field is required")
        return self


class ContactTagsInput(ActionInput):
    contact_id: int = Field(ge=1)
    tags: list[str] = Field(min_length=1, max_length=20)


class ContactCustomFieldsSetInput(ActionInput):
    contact_id: int = Field(ge=1)
    fields: dict[str, Any] = Field(min_length=1, max_length=30)


class ContactCustomFieldsRemoveInput(ActionInput):
    contact_id: int = Field(ge=1)
    keys: list[str] = Field(min_length=1, max_length=30)


class ConversationUpdateInput(ActionInput):
    conversation_id: int = Field(ge=1)
    status: Literal["open", "pending", "expired", "solved", "blocked"] | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_change(self):
        if self.status is None and self.priority is None:
            raise ValueError("Status or priority is required")
        return self


class IdentityVerifyInput(ActionInput):
    conversation_id: int = Field(ge=1)
    method: Literal[
        "order_details",
        "registered_phone",
        "email_otp",
        "sms_otp",
        "staff_review",
    ]
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_hint: str = Field(default="", max_length=120)
    expires_minutes: int = Field(default=30, ge=5, le=240)


class SensitiveOrderOperationInput(ActionInput):
    conversation_id: int = Field(ge=1)
    operation: Literal[
        "refund",
        "cancel_order",
        "change_address",
        "update_sensitive_data",
    ]
    order_number: str = Field(min_length=3, max_length=120)
    details: dict[str, Any] = Field(default_factory=dict, max_length=30)


class RestApiCallInput(ActionInput):
    endpoint_id: int = Field(ge=1)
    conversation_id: int | None = Field(default=None, ge=1)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=500)
    query: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, max_length=50
    )
    json_body: dict[str, Any] | list[Any] | None = None


class WhatsAppTextSendInput(ActionInput):
    conversation_id: int = Field(ge=1)
    existing_message_id: int | None = Field(default=None, ge=1)
    body: str = Field(min_length=1, max_length=4096)
    sender_type: Literal["ai", "agent", "system"] = "system"
    sender_name: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WhatsAppTemplateSendInput(ActionInput):
    conversation_id: int = Field(ge=1)
    template_id: int = Field(ge=1)
    components: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    sender_name: str | None = Field(default=None, max_length=120)


class InteractiveButton(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=20)


class InteractiveListRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=24)
    description: str | None = Field(default=None, max_length=72)


class InteractiveListSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=24)
    rows: list[InteractiveListRow] = Field(min_length=1, max_length=10)


class WhatsAppInteractiveSendInput(ActionInput):
    conversation_id: int = Field(ge=1)
    kind: Literal["buttons", "list"]
    body: str = Field(min_length=1, max_length=1024)
    header: str | None = Field(default=None, max_length=60)
    footer: str | None = Field(default=None, max_length=60)
    buttons: list[InteractiveButton] = Field(default_factory=list, max_length=3)
    button_text: str | None = Field(default=None, max_length=20)
    sections: list[InteractiveListSection] = Field(default_factory=list, max_length=10)
    sender_name: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_kind_payload(self):
        if self.kind == "buttons" and not self.buttons:
            raise ValueError("Buttons are required for a button message")
        if self.kind == "list" and (not self.button_text or not self.sections):
            raise ValueError("Button text and sections are required for a list message")
        return self


class WhatsAppTemplatesSyncInput(ActionInput):
    channel_account_id: int | None = Field(default=None, ge=1)
