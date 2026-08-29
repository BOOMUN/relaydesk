from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..channels import ChannelProviderError
from ..models import (
    ActionAttempt,
    ActionExecution,
    ActionStatus,
    AuditLog,
    User,
    utcnow,
)
from .definitions import ACTION_REGISTRY, ActionDefinition
from .handlers import ActionHandlerError, HANDLERS


@dataclass(frozen=True, slots=True)
class ActionContext:
    tenant_id: int
    caller_type: str
    user_id: int | None = None
    user_role: str | None = None
    user_name: str | None = None
    source_message_id: int | None = None

    @classmethod
    def for_user(cls, user: User) -> "ActionContext":
        return cls(
            tenant_id=user.tenant_id,
            caller_type="user",
            user_id=user.id,
            user_role=user.role,
            user_name=user.name,
        )

    @classmethod
    def for_system(
        cls, tenant_id: int, *, source_message_id: int | None = None
    ) -> "ActionContext":
        return cls(
            tenant_id=tenant_id,
            caller_type="system",
            source_message_id=source_message_id,
        )

    @classmethod
    def for_model(
        cls, tenant_id: int, *, source_message_id: int | None = None
    ) -> "ActionContext":
        return cls(
            tenant_id=tenant_id,
            caller_type="model",
            source_message_id=source_message_id,
        )


ActionHandler = Callable[[Session, ActionExecution, Any, ActionContext], dict[str, Any]]


class ActionExecutionError(RuntimeError):
    def __init__(self, message: str, *, code: str = "action_error") -> None:
        self.code = code
        super().__init__(message)


def _audit(
    db: Session,
    execution: ActionExecution,
    action: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            tenant_id=execution.tenant_id,
            user_id=execution.requested_by_user_id,
            action=action,
            entity_type="action_execution",
            entity_id=execution.id,
            details={
                "action_name": execution.action_name,
                "status": execution.status,
                **(details or {}),
            },
        )
    )


def _authorize(definition: ActionDefinition, context: ActionContext) -> None:
    if context.caller_type not in definition.allowed_callers:
        raise ActionExecutionError(
            f"{context.caller_type} cannot request {definition.name}",
            code="action_caller_forbidden",
        )
    if context.caller_type == "user" and context.user_role not in definition.allowed_roles:
        raise ActionExecutionError(
            f"Role {context.user_role!r} cannot request {definition.name}",
            code="action_permission_denied",
        )


def _default_idempotency_key(
    definition: ActionDefinition,
    arguments: dict[str, Any],
    context: ActionContext,
) -> str:
    if context.source_message_id is None:
        return str(uuid4())
    encoded = json.dumps(
        {
            "action": definition.name,
            "arguments": arguments,
            "source_message_id": context.source_message_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def propose_action(
    db: Session,
    context: ActionContext,
    action_name: str,
    arguments: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    auto_execute: bool = True,
) -> ActionExecution:
    definition = ACTION_REGISTRY.get(action_name)
    if definition is None:
        raise ActionExecutionError(
            f"Unknown action: {action_name}", code="unknown_action"
        )
    if context.tenant_id <= 0:
        raise ActionExecutionError("Tenant is required", code="tenant_required")
    _authorize(definition, context)
    try:
        validated = definition.input_model.model_validate(arguments)
    except ValidationError as exc:
        raise ActionExecutionError(str(exc), code="invalid_action_input") from exc
    normalized_input = validated.model_dump(mode="json", exclude_unset=True)
    key = (idempotency_key or "").strip()[:255] or _default_idempotency_key(
        definition, normalized_input, context
    )
    existing = db.scalar(
        select(ActionExecution).where(
            ActionExecution.tenant_id == context.tenant_id,
            ActionExecution.action_name == action_name,
            ActionExecution.idempotency_key == key,
        )
    )
    if existing is not None:
        return existing
    requires_confirmation = definition.requires_confirmation(context)
    execution = ActionExecution(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        conversation_id=getattr(validated, "conversation_id", None),
        contact_id=getattr(validated, "contact_id", None),
        source_message_id=context.source_message_id,
        action_name=definition.name,
        action_version=1,
        purpose=definition.purpose,
        input_json=normalized_input,
        requested_by_type=context.caller_type,
        requested_by_user_id=context.user_id,
        risk_level=definition.risk_level,
        permission_scope=definition.permission_scope,
        status=(
            ActionStatus.PENDING_CONFIRMATION.value
            if requires_confirmation
            else ActionStatus.PROPOSED.value
        ),
        requires_confirmation=requires_confirmation,
        confirmation_reason=(
            "This model-proposed action changes customer or outbound channel state."
            if requires_confirmation
            else None
        ),
        idempotency_key=key,
        timeout_seconds=definition.timeout_seconds,
        max_attempts=definition.max_attempts,
    )
    db.add(execution)
    _audit(db, execution, "action.requested")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(ActionExecution).where(
                ActionExecution.tenant_id == context.tenant_id,
                ActionExecution.action_name == action_name,
                ActionExecution.idempotency_key == key,
            )
        )
        if existing is None:
            raise
        return existing
    db.refresh(execution)
    if execution.status == ActionStatus.PENDING_CONFIRMATION.value:
        from ..services.realtime import publish_inbox_updated

        publish_inbox_updated(
            execution.tenant_id,
            activity="state",
            conversation_id=execution.conversation_id,
        )
    if auto_execute and not requires_confirmation:
        return execute_action(db, execution.id, context)
    return execution


def _failure(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ChannelProviderError):
        return exc.code, exc.retryable
    if isinstance(exc, ActionHandlerError):
        return exc.code, exc.retryable
    if isinstance(exc, ActionExecutionError):
        return exc.code, False
    return "unhandled_action_error", False


def execute_action(
    db: Session,
    execution_id: str,
    context: ActionContext,
) -> ActionExecution:
    execution = db.get(ActionExecution, execution_id)
    if execution is None or execution.tenant_id != context.tenant_id:
        raise ActionExecutionError("Action does not exist", code="action_not_found")
    if execution.status == ActionStatus.SUCCEEDED.value:
        return execution
    if execution.status == ActionStatus.PENDING_CONFIRMATION.value:
        raise ActionExecutionError(
            "Action requires confirmation", code="action_confirmation_required"
        )
    definition = ACTION_REGISTRY.get(execution.action_name)
    if definition is None:
        raise ActionExecutionError("Action definition is unavailable", code="unknown_action")
    _authorize(definition, context)
    handler = HANDLERS[definition.handler_name]
    validated = definition.input_model.model_validate(execution.input_json)

    while execution.attempt_count < execution.max_attempts:
        execution.status = ActionStatus.RUNNING.value
        execution.started_at = execution.started_at or utcnow()
        execution.attempt_count += 1
        attempt = ActionAttempt(
            tenant_id=execution.tenant_id,
            action_execution_id=execution.id,
            attempt_number=execution.attempt_count,
            status="running",
        )
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
        started = perf_counter()
        try:
            result = handler(db, execution, validated, context)
            completed = utcnow()
            duration_ms = int((perf_counter() - started) * 1000)
            execution.status = ActionStatus.SUCCEEDED.value
            execution.result_json = result
            execution.error_code = None
            execution.failure_reason = None
            execution.completed_at = completed
            attempt.status = "succeeded"
            attempt.result_json = result
            attempt.provider_request_id = str(result.get("provider_request_id") or "") or None
            attempt.duration_ms = duration_ms
            attempt.completed_at = completed
            _audit(db, execution, "action.succeeded", details={"duration_ms": duration_ms})
            db.commit()
            db.refresh(execution)
            from ..services.realtime import publish_inbox_updated

            publish_inbox_updated(
                execution.tenant_id,
                activity=(
                    "message"
                    if execution.action_name.startswith("whatsapp.")
                    and execution.action_name.endswith(".send")
                    else "state"
                ),
                conversation_id=execution.conversation_id,
                sender_type=(
                    str(execution.input_json.get("sender_type") or "system")
                    if execution.action_name == "whatsapp.text.send"
                    else None
                ),
            )
            return execution
        except Exception as exc:
            db.rollback()
            execution = db.get(ActionExecution, execution_id)
            attempt = db.get(ActionAttempt, attempt.id)
            assert execution is not None and attempt is not None
            code, retryable = _failure(exc)
            completed = utcnow()
            attempt.status = "failed"
            attempt.error_code = code
            attempt.failure_reason = str(exc)[:2000]
            attempt.duration_ms = int((perf_counter() - started) * 1000)
            attempt.completed_at = completed
            execution.error_code = code
            execution.failure_reason = str(exc)[:2000]
            if not retryable or execution.attempt_count >= execution.max_attempts:
                execution.status = ActionStatus.FAILED.value
                execution.completed_at = completed
                _audit(db, execution, "action.failed", details={"error_code": code})
                db.commit()
                db.refresh(execution)
                from ..services.realtime import publish_inbox_updated

                publish_inbox_updated(
                    execution.tenant_id,
                    activity="state",
                    conversation_id=execution.conversation_id,
                )
                return execution
            execution.status = ActionStatus.PROPOSED.value
            db.commit()
    return execution


def confirm_action(
    db: Session,
    execution_id: str,
    user: User,
) -> ActionExecution:
    execution = db.get(ActionExecution, execution_id)
    if execution is None or execution.tenant_id != user.tenant_id:
        raise ActionExecutionError("Action does not exist", code="action_not_found")
    if execution.status != ActionStatus.PENDING_CONFIRMATION.value:
        raise ActionExecutionError(
            "Action is not waiting for confirmation", code="action_not_pending_confirmation"
        )
    definition = ACTION_REGISTRY[execution.action_name]
    context = ActionContext.for_user(user)
    _authorize(definition, context)
    execution.confirmed_by_user_id = user.id
    execution.confirmed_at = utcnow()
    execution.status = ActionStatus.PROPOSED.value
    _audit(db, execution, "action.confirmed", details={"confirmed_by": user.id})
    db.commit()
    return execute_action(db, execution.id, context)


def reject_action(
    db: Session,
    execution_id: str,
    user: User,
    reason: str,
) -> ActionExecution:
    execution = db.get(ActionExecution, execution_id)
    if execution is None or execution.tenant_id != user.tenant_id:
        raise ActionExecutionError("Action does not exist", code="action_not_found")
    if execution.status != ActionStatus.PENDING_CONFIRMATION.value:
        raise ActionExecutionError(
            "Action is not waiting for confirmation", code="action_not_pending_confirmation"
        )
    execution.status = ActionStatus.REJECTED.value
    execution.failure_reason = reason.strip()[:2000] or "Rejected by staff"
    execution.completed_at = utcnow()
    execution.confirmed_by_user_id = user.id
    execution.confirmed_at = utcnow()
    _audit(db, execution, "action.rejected", details={"reason": execution.failure_reason})
    db.commit()
    db.refresh(execution)
    return execution
