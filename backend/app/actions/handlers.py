from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..channels import ChannelProviderError, OutboundMessage, get_channel_provider
from ..contact_attributes import INTERNAL_CONTACT_ATTRIBUTE_PREFIX
from ..models import (
    AutomationFormEvent,
    AutomationFormSession,
    ChannelContactIdentity,
    ChannelAccount,
    Contact,
    Conversation,
    ConversationStatus,
    IdentityVerification,
    Message,
    MessageDeliveryAttempt,
    MessageDirection,
    MessageSender,
    RestActionEndpoint,
    SensitiveOperationRequest,
    Team,
    TeamMember,
    User,
    WhatsAppTemplate,
    WhatsAppTemplateSyncRun,
    utcnow,
)
from ..services.rest_actions import (
    RestActionSecurityError,
    execute_rest_action,
    rest_action_requires_identity,
)


class ActionHandlerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "action_failed",
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def _conversation(db: Session, tenant_id: int, conversation_id: int) -> Conversation:
    item = db.get(Conversation, conversation_id)
    if item is None or item.tenant_id != tenant_id:
        raise ActionHandlerError("Conversation does not exist", code="conversation_not_found")
    return item


def _contact(db: Session, tenant_id: int, contact_id: int) -> Contact:
    item = db.get(Contact, contact_id)
    if item is None or item.tenant_id != tenant_id:
        raise ActionHandlerError("Contact does not exist", code="contact_not_found")
    return item


def _before_conversation(item: Conversation) -> dict[str, Any]:
    return {
        "status": item.status,
        "priority": item.priority,
        "assigned_team_id": item.assigned_team_id,
        "assigned_user_id": item.assigned_user_id,
        "ai_enabled": item.ai_enabled,
    }


def conversation_handoff(db, execution, data, context) -> dict[str, Any]:
    del context
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    before = _before_conversation(conversation)
    if data.target_team_id is not None:
        team = db.get(Team, data.target_team_id)
        if team is None or team.tenant_id != execution.tenant_id:
            raise ActionHandlerError("Team does not exist", code="team_not_found")
        conversation.assigned_team_id = team.id
    conversation.ai_enabled = False
    conversation.ai_route = "handoff"
    conversation.status = ConversationStatus.PENDING.value
    conversation.priority = "high"
    conversation.updated_at = utcnow()
    from ..services.conversation_sessions import mark_context_session_paused

    context_session_id = mark_context_session_paused(db, conversation, reason="handoff")
    return {
        "before": before,
        "after": _before_conversation(conversation),
        "reason": data.reason,
        "context_session_id": context_session_id,
    }


def conversation_resume_ai(db, execution, data, context) -> dict[str, Any]:
    del context
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    before = _before_conversation(conversation)
    if conversation.status == ConversationStatus.BLOCKED.value or conversation.contact.is_blocked:
        raise ActionHandlerError(
            "AI cannot resume for a blocked contact", code="contact_blocked"
        )
    conversation.ai_enabled = True
    conversation.ai_route = None
    conversation.status = ConversationStatus.OPEN.value
    conversation.updated_at = utcnow()
    from ..services.agent_profiles import published_agent_configuration
    from ..services.conversation_sessions import mark_context_session_resumed

    context_session_id = mark_context_session_resumed(
        db,
        conversation,
        reason=data.reason,
    )
    form_session = db.scalar(
        select(AutomationFormSession)
        .where(
            AutomationFormSession.tenant_id == execution.tenant_id,
            AutomationFormSession.conversation_id == conversation.id,
            AutomationFormSession.status == "handed_off",
        )
        .order_by(AutomationFormSession.updated_at.desc())
    )
    resumed_form_session_id = None
    if form_session is not None:
        fields = list((form_session.definition_json or {}).get("fields") or [])
        if form_session.current_step < len(fields):
            profile = published_agent_configuration(db, execution.tenant_id) or {}
            timeout_minutes = max(
                5,
                min(1440, int(profile.get("automation_timeout_minutes") or 30)),
            )
            previous_status = form_session.status
            form_session.status = "active"
            form_session.paused_at = None
            form_session.completed_at = None
            form_session.expires_at = utcnow() + timedelta(minutes=timeout_minutes)
            form_session.updated_at = utcnow()
            db.add(
                AutomationFormEvent(
                    tenant_id=execution.tenant_id,
                    session_id=form_session.id,
                    source_message_id=execution.source_message_id,
                    event_type="resumed_after_handoff",
                    before_json={"status": previous_status},
                    after_json={"status": form_session.status},
                )
            )
            resumed_form_session_id = form_session.id
    return {
        "before": before,
        "after": _before_conversation(conversation),
        "reason": data.reason,
        "context_session_id": context_session_id,
        "resumed_form_session_id": resumed_form_session_id,
    }


def conversation_assign(db, execution, data, context) -> dict[str, Any]:
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    before = _before_conversation(conversation)
    team = None
    assignee = None
    update_team = "team_id" in data.model_fields_set
    update_user = "user_id" in data.model_fields_set
    if not update_team and not update_user:
        raise ActionHandlerError(
            "Team or agent assignment is required", code="invalid_assignment"
        )
    if update_team and data.team_id is not None:
        team = db.get(Team, data.team_id)
        if team is None or team.tenant_id != execution.tenant_id:
            raise ActionHandlerError("Team does not exist", code="team_not_found")
    if update_user and data.user_id is not None:
        assignee = db.get(User, data.user_id)
        if (
            assignee is None
            or assignee.tenant_id != execution.tenant_id
            or not assignee.is_active
        ):
            raise ActionHandlerError("Agent does not exist", code="agent_not_found")
        if context.caller_type == "model":
            raise ActionHandlerError(
                "The model may route to a team but cannot select an individual agent",
                code="model_assignment_forbidden",
            )
        membership_team = team if update_team else conversation.assigned_team
        if membership_team is not None:
            membership = db.scalar(
                select(TeamMember.id).where(
                    TeamMember.team_id == membership_team.id,
                    TeamMember.user_id == assignee.id,
                )
            )
            if membership is None:
                raise ActionHandlerError(
                    "Agent is not a member of the selected team",
                    code="agent_not_in_team",
                )
    if update_team:
        conversation.assigned_team_id = team.id if team else None
    if update_user:
        conversation.assigned_user_id = assignee.id if assignee else None
    conversation.updated_at = utcnow()
    return {"before": before, "after": _before_conversation(conversation)}


def contact_update_profile(db, execution, data, context) -> dict[str, Any]:
    del context
    contact = _contact(db, execution.tenant_id, data.contact_id)
    before = {
        "display_name": contact.display_name,
        "phone": contact.phone,
        "language": contact.language,
    }
    for key in ("display_name", "phone", "language"):
        value = getattr(data, key)
        if value is not None:
            setattr(contact, key, value)
    contact.updated_at = utcnow()
    return {
        "before": before,
        "after": {
            "display_name": contact.display_name,
            "phone": contact.phone,
            "language": contact.language,
        },
    }


def _normalize_tags(tags: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        value = " ".join(str(raw).split()).strip()[:80]
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            values.append(value)
    if not values:
        raise ActionHandlerError("At least one valid tag is required", code="invalid_tags")
    return values


def contact_tags_add(db, execution, data, context) -> dict[str, Any]:
    del context
    contact = _contact(db, execution.tenant_id, data.contact_id)
    before = list(contact.tags or [])
    additions = _normalize_tags(data.tags)
    existing = {item.casefold() for item in before}
    after = [*before, *(item for item in additions if item.casefold() not in existing)]
    if len(after) > 50:
        raise ActionHandlerError("A contact may have at most 50 tags", code="tag_limit")
    contact.tags = after
    contact.updated_at = utcnow()
    return {"before": before, "after": after}


def contact_tags_remove(db, execution, data, context) -> dict[str, Any]:
    del context
    contact = _contact(db, execution.tenant_id, data.contact_id)
    before = list(contact.tags or [])
    removals = {item.casefold() for item in _normalize_tags(data.tags)}
    contact.tags = [item for item in before if item.casefold() not in removals]
    contact.updated_at = utcnow()
    return {"before": before, "after": contact.tags}


def _validate_public_field_key(value: str) -> str:
    key = str(value).strip()
    if not key or len(key) > 120 or key.startswith(INTERNAL_CONTACT_ATTRIBUTE_PREFIX):
        raise ActionHandlerError(
            f"Invalid public custom field key: {key!r}", code="invalid_custom_field"
        )
    return key


def contact_custom_fields_set(db, execution, data, context) -> dict[str, Any]:
    del context
    contact = _contact(db, execution.tenant_id, data.contact_id)
    before = dict(contact.custom_attributes or {})
    after = dict(before)
    for raw_key, value in data.fields.items():
        after[_validate_public_field_key(raw_key)] = value
    contact.custom_attributes = after
    contact.updated_at = utcnow()
    return {"updated_keys": sorted(set(after) - set(before) | set(data.fields))}


def contact_custom_fields_remove(db, execution, data, context) -> dict[str, Any]:
    del context
    contact = _contact(db, execution.tenant_id, data.contact_id)
    after = dict(contact.custom_attributes or {})
    removed: list[str] = []
    for raw_key in data.keys:
        key = _validate_public_field_key(raw_key)
        if key in after:
            after.pop(key)
            removed.append(key)
    contact.custom_attributes = after
    contact.updated_at = utcnow()
    return {"removed_keys": removed}


def conversation_update(db, execution, data, context) -> dict[str, Any]:
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    before = _before_conversation(conversation)
    if context.caller_type == "model" and data.status in {"solved", "blocked", "expired"}:
        raise ActionHandlerError(
            "The model cannot close, block, or expire a conversation",
            code="model_status_forbidden",
        )
    if data.status is not None:
        conversation.status = data.status
        if data.status in {"solved", "blocked"}:
            from ..services.conversation_sessions import mark_context_session_closed

            mark_context_session_closed(
                db,
                conversation,
                reason=data.reason or f"action_status_{data.status}",
            )
    if data.priority is not None:
        conversation.priority = data.priority
    conversation.updated_at = utcnow()
    return {"before": before, "after": _before_conversation(conversation)}


def _active_identity_verification(
    db: Session,
    *,
    tenant_id: int,
    conversation: Conversation,
) -> IdentityVerification | None:
    return db.scalar(
        select(IdentityVerification)
        .where(
            IdentityVerification.tenant_id == tenant_id,
            IdentityVerification.conversation_id == conversation.id,
            IdentityVerification.contact_id == conversation.contact_id,
            IdentityVerification.status == "verified",
            IdentityVerification.expires_at > utcnow(),
        )
        .order_by(IdentityVerification.created_at.desc())
    )


def identity_verify(db, execution, data, context) -> dict[str, Any]:
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    if context.user_id is None:
        raise ActionHandlerError(
            "Identity verification must be recorded by a staff user",
            code="human_verifier_required",
        )
    verification = IdentityVerification(
        tenant_id=execution.tenant_id,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        status="verified",
        method=data.method,
        evidence_hash=data.evidence_hash,
        evidence_hint=data.evidence_hint.strip()[:120],
        verified_by_user_id=context.user_id,
        expires_at=utcnow() + timedelta(minutes=data.expires_minutes),
    )
    db.add(verification)
    db.flush()
    return {
        "identity_verification_id": verification.id,
        "conversation_id": conversation.id,
        "method": verification.method,
        "expires_at": verification.expires_at.isoformat(),
    }


def order_sensitive_request(db, execution, data, context) -> dict[str, Any]:
    del context
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    if execution.confirmed_by_user_id is None or execution.confirmed_at is None:
        raise ActionHandlerError(
            "Sensitive operation requires human confirmation",
            code="human_confirmation_required",
        )
    verification = _active_identity_verification(
        db,
        tenant_id=execution.tenant_id,
        conversation=conversation,
    )
    if verification is None:
        raise ActionHandlerError(
            "Customer identity must be verified before confirming this request",
            code="identity_verification_required",
        )
    request = SensitiveOperationRequest(
        tenant_id=execution.tenant_id,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        action_execution_id=execution.id,
        identity_verification_id=verification.id,
        operation=data.operation,
        order_number=data.order_number.strip(),
        requested_changes=dict(data.details or {}),
        status="approved_for_manual_execution",
        approved_by_user_id=execution.confirmed_by_user_id,
    )
    db.add(request)
    db.flush()
    conversation.ai_enabled = False
    conversation.ai_route = "handoff"
    conversation.status = ConversationStatus.PENDING.value
    conversation.priority = "high"
    conversation.updated_at = utcnow()
    return {
        "sensitive_operation_request_id": request.id,
        "identity_verification_id": verification.id,
        "operation": request.operation,
        "status": request.status,
        "external_operation_executed": False,
    }


def rest_api_call(db, execution, data, context) -> dict[str, Any]:
    del context
    endpoint = db.get(RestActionEndpoint, data.endpoint_id)
    if endpoint is None or endpoint.tenant_id != execution.tenant_id:
        raise ActionHandlerError("REST Action endpoint does not exist", code="endpoint_not_found")
    needs_identity = rest_action_requires_identity(
        endpoint,
        path=data.path,
        json_body=data.json_body,
    )
    if needs_identity:
        if data.conversation_id is None:
            raise ActionHandlerError(
                "Sensitive REST Action requires a conversation",
                code="conversation_required",
            )
        conversation = _conversation(db, execution.tenant_id, data.conversation_id)
        if _active_identity_verification(
            db,
            tenant_id=execution.tenant_id,
            conversation=conversation,
        ) is None:
            raise ActionHandlerError(
                "Identity verification is required for this REST Action",
                code="identity_verification_required",
            )
        if execution.confirmed_by_user_id is None:
            raise ActionHandlerError(
                "Sensitive REST Action requires human confirmation",
                code="human_confirmation_required",
            )
    try:
        return execute_rest_action(
            endpoint,
            method=data.method,
            path=data.path,
            query=dict(data.query or {}),
            json_body=data.json_body,
        )
    except RestActionSecurityError as exc:
        raise ActionHandlerError(
            str(exc), code=exc.code, retryable=exc.retryable
        ) from exc


def _recipient(db: Session, conversation: Conversation, account_id: int) -> str:
    identity = db.scalar(
        select(ChannelContactIdentity).where(
            ChannelContactIdentity.channel_account_id == account_id,
            ChannelContactIdentity.contact_id == conversation.contact_id,
        )
    )
    if identity is not None:
        return identity.address
    legacy = (conversation.contact.custom_attributes or {}).get(
        "_agentdesk_evolution_recipient_jid"
    )
    return str(legacy or conversation.contact.wa_id)


def _send_message(
    db: Session,
    execution,
    context,
    *,
    conversation: Conversation,
    outbound: OutboundMessage,
    sender_type: str,
    sender_name: str,
    body: str,
    content_type: str,
    metadata: dict[str, Any],
    existing_message_id: int | None = None,
) -> dict[str, Any]:
    if conversation.status == ConversationStatus.BLOCKED.value or conversation.contact.is_blocked:
        raise ActionHandlerError("Contact is blocked", code="contact_blocked")
    provider = get_channel_provider(
        db, execution.tenant_id, conversation.channel_account_id
    )
    conversation.channel_account_id = provider.account.id
    outbound.to = _recipient(db, conversation, provider.account.id)
    existing_message = None
    if existing_message_id is not None:
        existing_message = db.scalar(
            select(Message).where(
                Message.id == existing_message_id,
                Message.tenant_id == execution.tenant_id,
                Message.conversation_id == conversation.id,
                Message.direction == MessageDirection.OUTBOUND.value,
            )
        )
        if existing_message is None:
            raise ActionHandlerError(
                "Outbound message does not exist", code="message_not_found"
            )
        if existing_message.delivery_status not in {"failed", "pending"}:
            raise ActionHandlerError(
                "Only failed outbound messages may be retried",
                code="message_retry_conflict",
            )
    result = provider.send(outbound)
    now = utcnow()
    if existing_message is None:
        message = Message(
            tenant_id=execution.tenant_id,
            conversation_id=conversation.id,
            channel_account_id=provider.account.id,
            provider=result.provider,
            external_id=result.external_message_id,
            direction=MessageDirection.OUTBOUND.value,
            sender_type=sender_type,
            sender_name=sender_name,
            content_type=content_type,
            body=body,
            delivery_status=result.status,
            metadata_json={**metadata, "action_execution_id": execution.id},
            created_at=now,
        )
        db.add(message)
        db.flush()
        attempt_number = 1
    else:
        message = existing_message
        message.channel_account_id = provider.account.id
        message.provider = result.provider
        message.external_id = result.external_message_id
        message.delivery_status = result.status
        message.metadata_json = {
            **dict(message.metadata_json or {}),
            **metadata,
            "action_execution_id": execution.id,
        }
        attempt_number = int(
            db.scalar(
                select(func.max(MessageDeliveryAttempt.attempt_number)).where(
                    MessageDeliveryAttempt.message_id == message.id
                )
            )
            or 0
        ) + 1
    db.add(
        MessageDeliveryAttempt(
            tenant_id=execution.tenant_id,
            message_id=message.id,
            channel_account_id=provider.account.id,
            provider=result.provider,
            attempt_number=attempt_number,
            external_id=result.external_message_id,
            delivery_status=result.status,
        )
    )
    conversation.last_message_at = now
    conversation.updated_at = now
    if sender_type == MessageSender.AGENT.value and context.user_id is not None:
        conversation.assigned_user_id = context.user_id
        conversation.unread_count = 0
    return {
        "message_id": message.id,
        "external_message_id": result.external_message_id,
        "provider": result.provider,
        "delivery_status": result.status,
        "provider_request_id": result.external_message_id,
    }


def whatsapp_text_send(db, execution, data, context) -> dict[str, Any]:
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    sender_name = data.sender_name or (
        context.user_name if data.sender_type == "agent" else "RelayDesk AI"
    )
    return _send_message(
        db,
        execution,
        context,
        conversation=conversation,
        outbound=OutboundMessage(
            to="",
            kind="text",
            text=data.body,
            idempotency_key=execution.idempotency_key,
        ),
        sender_type=data.sender_type,
        sender_name=sender_name or "RelayDesk",
        body=data.body,
        content_type="text",
        metadata=dict(data.metadata),
        existing_message_id=data.existing_message_id,
    )


def whatsapp_template_send(db, execution, data, context) -> dict[str, Any]:
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    template = db.get(WhatsAppTemplate, data.template_id)
    if template is None or template.tenant_id != execution.tenant_id or not template.is_active:
        raise ActionHandlerError("Template does not exist", code="template_not_found")
    if template.status.upper() != "APPROVED":
        raise ActionHandlerError(
            "Only Meta-approved templates may be sent", code="template_not_approved"
        )
    account = db.get(ChannelAccount, template.channel_account_id)
    if account is None or account.tenant_id != execution.tenant_id or account.provider != "meta":
        raise ActionHandlerError(
            "Approved templates can only be sent through a Meta channel account",
            code="meta_provider_required",
        )
    if conversation.channel_account_id not in {None, template.channel_account_id}:
        raise ActionHandlerError(
            "Template belongs to a different channel account",
            code="template_channel_mismatch",
        )
    conversation.channel_account_id = template.channel_account_id
    return _send_message(
        db,
        execution,
        context,
        conversation=conversation,
        outbound=OutboundMessage(
            to="",
            kind="template",
            template_name=template.name,
            template_language=template.language,
            template_components=data.components,
            idempotency_key=execution.idempotency_key,
        ),
        sender_type=MessageSender.AGENT.value if context.user_id else MessageSender.SYSTEM.value,
        sender_name=data.sender_name or context.user_name or "RelayDesk",
        body=f"[WhatsApp template: {template.name} / {template.language}]",
        content_type="template",
        metadata={
            "template_id": template.id,
            "template_name": template.name,
            "template_language": template.language,
            "template_components": data.components,
        },
    )


def whatsapp_interactive_send(db, execution, data, context) -> dict[str, Any]:
    conversation = _conversation(db, execution.tenant_id, data.conversation_id)
    now = utcnow()
    expires = conversation.service_window_expires_at
    if expires is None or (
        expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
    ) <= now:
        raise ActionHandlerError(
            "Interactive messages require an open 24-hour service window",
            code="service_window_closed",
        )
    interactive: dict[str, Any] = {
        "body": data.body,
        "header": data.header,
        "footer": data.footer,
    }
    if data.kind == "buttons":
        interactive["buttons"] = [item.model_dump() for item in data.buttons]
    else:
        interactive["button_text"] = data.button_text
        interactive["sections"] = [item.model_dump() for item in data.sections]
    return _send_message(
        db,
        execution,
        context,
        conversation=conversation,
        outbound=OutboundMessage(
            to="",
            kind=data.kind,
            interactive=interactive,
            idempotency_key=execution.idempotency_key,
        ),
        sender_type=MessageSender.AGENT.value if context.user_id else MessageSender.SYSTEM.value,
        sender_name=data.sender_name or context.user_name or "RelayDesk",
        body=data.body,
        content_type=data.kind,
        metadata={"interactive": interactive},
    )


def whatsapp_templates_sync(db, execution, data, context) -> dict[str, Any]:
    del context
    provider = get_channel_provider(
        db, execution.tenant_id, data.channel_account_id
    )
    if provider.provider_name != "meta":
        raise ActionHandlerError(
            "Official templates can only be synchronized from Meta",
            code="meta_provider_required",
        )
    run = WhatsAppTemplateSyncRun(
        tenant_id=execution.tenant_id,
        channel_account_id=provider.account.id,
        status="running",
    )
    db.add(run)
    db.flush()
    run_id = run.id
    # The sync-run row is durable even when the provider request fails and the
    # Action attempt itself must be rolled back.
    db.commit()
    try:
        templates = provider.sync_templates()
    except Exception as exc:
        db.rollback()
        failed_run = db.get(WhatsAppTemplateSyncRun, run_id)
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.failure_reason = str(exc)[:2000]
            failed_run.completed_at = utcnow()
            db.commit()
        raise
    approved = 0
    seen: set[tuple[str, str]] = set()
    for value in templates:
        name = str(value.get("name") or "").strip()
        language = str(value.get("language") or "").strip()
        if not name or not language:
            continue
        seen.add((name, language))
        item = db.scalar(
            select(WhatsAppTemplate).where(
                WhatsAppTemplate.channel_account_id == provider.account.id,
                WhatsAppTemplate.name == name,
                WhatsAppTemplate.language == language,
            )
        )
        if item is None:
            item = WhatsAppTemplate(
                tenant_id=execution.tenant_id,
                channel_account_id=provider.account.id,
                name=name,
                language=language,
            )
            db.add(item)
        status = str(value.get("status") or "PENDING").upper()
        item.provider_template_id = str(value.get("id") or "") or None
        item.category = str(value.get("category") or "UTILITY")
        item.status = status
        item.parameter_format = str(value.get("parameter_format") or "POSITIONAL")
        item.components = value.get("components") or []
        quality = value.get("quality_score")
        item.quality_rating = str(quality) if quality is not None else None
        item.rejection_reason = str(value.get("rejected_reason") or "") or None
        item.is_active = True
        item.last_synced_at = utcnow()
        approved += int(status == "APPROVED")
    existing = db.scalars(
        select(WhatsAppTemplate).where(
            WhatsAppTemplate.channel_account_id == provider.account.id
        )
    ).all()
    for item in existing:
        if (item.name, item.language) not in seen:
            item.is_active = False
    run.status = "completed"
    run.template_count = len(seen)
    run.approved_count = approved
    run.completed_at = utcnow()
    return {
        "sync_run_id": run.id,
        "template_count": len(seen),
        "approved_count": approved,
        "channel_account_id": provider.account.id,
    }


HANDLERS = {
    name: value
    for name, value in globals().copy().items()
    if callable(value)
    and name
    in {
        "conversation_handoff",
        "conversation_resume_ai",
        "conversation_assign",
        "contact_update_profile",
        "contact_tags_add",
        "contact_tags_remove",
        "contact_custom_fields_set",
        "contact_custom_fields_remove",
        "conversation_update",
        "identity_verify",
        "order_sensitive_request",
        "rest_api_call",
        "whatsapp_text_send",
        "whatsapp_template_send",
        "whatsapp_interactive_send",
        "whatsapp_templates_sync",
    }
}
