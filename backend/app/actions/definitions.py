from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel

from ..models import ActionRisk, UserRole
from .schemas import (
    ContactCustomFieldsRemoveInput,
    ContactCustomFieldsSetInput,
    ContactTagsInput,
    ContactUpdateProfileInput,
    ConversationAssignInput,
    ConversationHandoffInput,
    ConversationResumeAIInput,
    ConversationUpdateInput,
    IdentityVerifyInput,
    RestApiCallInput,
    SensitiveOrderOperationInput,
    WhatsAppInteractiveSendInput,
    WhatsAppTemplateSendInput,
    WhatsAppTemplatesSyncInput,
    WhatsAppTextSendInput,
)

if TYPE_CHECKING:
    from .executor import ActionContext, ActionHandler


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    name: str
    purpose: str
    input_model: type[BaseModel]
    permission_scope: str
    risk_level: str
    timeout_seconds: int
    max_attempts: int
    allowed_callers: frozenset[str]
    allowed_roles: frozenset[str]
    confirmation_callers: frozenset[str]
    handler_name: str

    @property
    def input_schema(self) -> dict:
        return self.input_model.model_json_schema()

    def requires_confirmation(self, context: "ActionContext") -> bool:
        return context.caller_type in self.confirmation_callers


ALL_STAFF_ROLES = frozenset(
    {
        UserRole.ADMIN.value,
        UserRole.MANAGER.value,
        UserRole.AGENT.value,
        UserRole.KNOWLEDGE_MANAGER.value,
    }
)
MANAGER_ROLES = frozenset({UserRole.ADMIN.value, UserRole.MANAGER.value})


def _definition(
    name: str,
    purpose: str,
    input_model: type[BaseModel],
    permission_scope: str,
    risk: ActionRisk,
    handler_name: str,
    *,
    callers: set[str] | frozenset[str] = frozenset({"user", "system", "model"}),
    roles: frozenset[str] = ALL_STAFF_ROLES,
    confirmation_callers: set[str] | frozenset[str] = frozenset(),
    timeout: int = 10,
    attempts: int = 1,
) -> ActionDefinition:
    return ActionDefinition(
        name=name,
        purpose=purpose,
        input_model=input_model,
        permission_scope=permission_scope,
        risk_level=risk.value,
        timeout_seconds=timeout,
        max_attempts=attempts,
        allowed_callers=frozenset(callers),
        allowed_roles=roles,
        confirmation_callers=frozenset(confirmation_callers),
        handler_name=handler_name,
    )


ACTION_REGISTRY = {
    item.name: item
    for item in (
        _definition(
            "conversation.handoff",
            "Pause AI and place a conversation in the human support queue.",
            ConversationHandoffInput,
            "conversation:handoff",
            ActionRisk.MEDIUM,
            "conversation_handoff",
            attempts=3,
        ),
        _definition(
            "conversation.resume_ai",
            "Resume AI after a staff member has reviewed the conversation.",
            ConversationResumeAIInput,
            "conversation:resume_ai",
            ActionRisk.HIGH,
            "conversation_resume_ai",
            callers={"user"},
            attempts=3,
        ),
        _definition(
            "conversation.assign",
            "Assign a conversation to a team or staff member.",
            ConversationAssignInput,
            "conversation:assign",
            ActionRisk.LOW,
            "conversation_assign",
            attempts=3,
        ),
        _definition(
            "contact.update_profile",
            "Update AgentDesk-owned contact profile fields.",
            ContactUpdateProfileInput,
            "contact:update",
            ActionRisk.MEDIUM,
            "contact_update_profile",
            confirmation_callers={"model"},
            attempts=3,
        ),
        _definition(
            "contact.tags.add",
            "Atomically add normalized tags to a contact.",
            ContactTagsInput,
            "contact:tag",
            ActionRisk.LOW,
            "contact_tags_add",
            attempts=3,
        ),
        _definition(
            "contact.tags.remove",
            "Atomically remove tags from a contact.",
            ContactTagsInput,
            "contact:tag",
            ActionRisk.LOW,
            "contact_tags_remove",
            attempts=3,
        ),
        _definition(
            "contact.custom_fields.set",
            "Set AgentDesk-owned public custom fields on a contact.",
            ContactCustomFieldsSetInput,
            "contact:custom_fields",
            ActionRisk.MEDIUM,
            "contact_custom_fields_set",
            confirmation_callers={"model"},
            attempts=3,
        ),
        _definition(
            "contact.custom_fields.remove",
            "Remove AgentDesk-owned public custom fields from a contact.",
            ContactCustomFieldsRemoveInput,
            "contact:custom_fields",
            ActionRisk.MEDIUM,
            "contact_custom_fields_remove",
            confirmation_callers={"model"},
            attempts=3,
        ),
        _definition(
            "conversation.update",
            "Update conversation status or priority through validated transitions.",
            ConversationUpdateInput,
            "conversation:update",
            ActionRisk.MEDIUM,
            "conversation_update",
            confirmation_callers={"model"},
            attempts=3,
        ),
        _definition(
            "identity.verify",
            "Record a time-limited identity verification without storing raw evidence.",
            IdentityVerifyInput,
            "identity:verify",
            ActionRisk.HIGH,
            "identity_verify",
            callers={"user"},
            attempts=1,
        ),
        _definition(
            "order.sensitive.request",
            "Create a verified, human-confirmed request for a sensitive order operation.",
            SensitiveOrderOperationInput,
            "order:sensitive_request",
            ActionRisk.HIGH,
            "order_sensitive_request",
            callers={"user", "system", "model"},
            confirmation_callers={"user", "system", "model"},
            attempts=1,
        ),
        _definition(
            "rest.api.call",
            "Call one administrator-approved REST endpoint within its exact method and path policy.",
            RestApiCallInput,
            "rest_action:execute",
            ActionRisk.HIGH,
            "rest_api_call",
            callers={"user", "system", "model"},
            confirmation_callers={"system", "model"},
            timeout=30,
            attempts=2,
        ),
        _definition(
            "whatsapp.text.send",
            "Send a text message through the conversation's configured channel.",
            WhatsAppTextSendInput,
            "message:send",
            ActionRisk.MEDIUM,
            "whatsapp_text_send",
            callers={"user", "system"},
            timeout=20,
        ),
        _definition(
            "whatsapp.template.send",
            "Send a locally synchronized, Meta-approved WhatsApp template.",
            WhatsAppTemplateSendInput,
            "message:send_template",
            ActionRisk.HIGH,
            "whatsapp_template_send",
            confirmation_callers={"model"},
            timeout=20,
        ),
        _definition(
            "whatsapp.interactive.send",
            "Send reply buttons or a list through the configured WhatsApp channel.",
            WhatsAppInteractiveSendInput,
            "message:send_interactive",
            ActionRisk.HIGH,
            "whatsapp_interactive_send",
            confirmation_callers={"model"},
            timeout=20,
        ),
        _definition(
            "whatsapp.templates.sync",
            "Synchronize languages and Meta review states without changing Meta approval.",
            WhatsAppTemplatesSyncInput,
            "template:sync",
            ActionRisk.LOW,
            "whatsapp_templates_sync",
            callers={"user", "system"},
            roles=MANAGER_ROLES,
            timeout=30,
            attempts=2,
        ),
    )
}
