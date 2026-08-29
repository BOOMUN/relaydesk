from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actions import (
    ACTION_REGISTRY,
    ActionContext,
    ActionExecutionError,
    confirm_action,
    propose_action,
    reject_action,
)
from ..database import get_db
from ..dependencies import get_current_user
from ..models import (
    ActionExecution,
    Conversation,
    IdentityVerification,
    RestActionEndpoint,
    User,
    utcnow,
)
from ..services.rest_actions import rest_action_requires_identity


router = APIRouter(prefix="/api/actions", tags=["actions"])


class ActionDefinitionResponse(BaseModel):
    name: str
    purpose: str
    input_schema: dict[str, Any]
    permission_scope: str
    risk_level: str
    timeout_seconds: int
    max_attempts: int
    allowed_callers: list[str]
    allowed_roles: list[str]
    confirmation_callers: list[str]


class ActionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any]


class ActionRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ActionExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: int
    conversation_id: int | None
    contact_id: int | None
    source_message_id: int | None
    action_name: str
    action_version: int
    purpose: str
    input_json: dict[str, Any]
    requested_by_type: str
    requested_by_user_id: int | None
    risk_level: str
    permission_scope: str
    status: str
    requires_confirmation: bool
    confirmation_reason: str | None
    confirmed_by_user_id: int | None
    confirmed_at: datetime | None
    idempotency_key: str
    timeout_seconds: int
    max_attempts: int
    attempt_count: int
    result_json: dict[str, Any]
    error_code: str | None
    failure_reason: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    requires_identity_verification: bool = False
    identity_verified: bool = False


def _identity_prerequisite(db: Session, item: ActionExecution) -> tuple[bool, bool]:
    required = item.action_name == "order.sensitive.request"
    if item.action_name == "rest.api.call":
        endpoint_id = item.input_json.get("endpoint_id")
        endpoint = db.get(RestActionEndpoint, endpoint_id) if endpoint_id else None
        if endpoint is not None and endpoint.tenant_id == item.tenant_id:
            required = rest_action_requires_identity(
                endpoint,
                path=str(item.input_json.get("path") or ""),
                json_body=item.input_json.get("json_body"),
            )
    verified = False
    conversation_id = item.conversation_id or item.input_json.get("conversation_id")
    contact_id = item.contact_id
    if required and conversation_id is not None and contact_id is None:
        conversation = db.get(Conversation, int(conversation_id))
        if conversation is not None and conversation.tenant_id == item.tenant_id:
            contact_id = conversation.contact_id
    if required and conversation_id is not None and contact_id is not None:
        verified = db.scalar(
            select(IdentityVerification.id).where(
                IdentityVerification.tenant_id == item.tenant_id,
                IdentityVerification.conversation_id == int(conversation_id),
                IdentityVerification.contact_id == contact_id,
                IdentityVerification.status == "verified",
                IdentityVerification.expires_at > utcnow(),
            )
        ) is not None
    return required, verified


def _execution_payload(db: Session, item: ActionExecution) -> ActionExecutionResponse:
    required, verified = _identity_prerequisite(db, item)
    return ActionExecutionResponse.model_validate(item).model_copy(
        update={
            "requires_identity_verification": required,
            "identity_verified": verified,
        }
    )


def _http_error(exc: ActionExecutionError) -> HTTPException:
    status = {
        "unknown_action": 404,
        "action_not_found": 404,
        "invalid_action_input": 422,
        "action_permission_denied": 403,
        "action_caller_forbidden": 403,
        "action_confirmation_required": 409,
        "action_not_pending_confirmation": 409,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@router.get("/definitions", response_model=list[ActionDefinitionResponse])
def list_action_definitions(user: User = Depends(get_current_user)):
    values: list[ActionDefinitionResponse] = []
    for item in ACTION_REGISTRY.values():
        if user.role not in item.allowed_roles or "user" not in item.allowed_callers:
            continue
        values.append(
            ActionDefinitionResponse(
                name=item.name,
                purpose=item.purpose,
                input_schema=item.input_schema,
                permission_scope=item.permission_scope,
                risk_level=item.risk_level,
                timeout_seconds=item.timeout_seconds,
                max_attempts=item.max_attempts,
                allowed_callers=sorted(item.allowed_callers),
                allowed_roles=sorted(item.allowed_roles),
                confirmation_callers=sorted(item.confirmation_callers),
            )
        )
    return values


@router.get("", response_model=list[ActionExecutionResponse])
def list_action_executions(
    status: str | None = Query(default=None, max_length=30),
    conversation_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(ActionExecution).where(ActionExecution.tenant_id == user.tenant_id)
    if status:
        statement = statement.where(ActionExecution.status == status)
    if conversation_id:
        statement = statement.where(ActionExecution.conversation_id == conversation_id)
    rows = db.scalars(
        statement.order_by(ActionExecution.created_at.desc()).limit(limit)
    ).all()
    return [_execution_payload(db, item) for item in rows]


@router.post("", response_model=ActionExecutionResponse)
def request_action(
    payload: ActionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        item = propose_action(
            db,
            ActionContext.for_user(user),
            payload.name,
            payload.arguments,
            idempotency_key=idempotency_key,
        )
        return _execution_payload(db, item)
    except ActionExecutionError as exc:
        raise _http_error(exc) from exc


@router.post("/{execution_id}/confirm", response_model=ActionExecutionResponse)
def confirm_action_request(
    execution_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return _execution_payload(db, confirm_action(db, execution_id, user))
    except ActionExecutionError as exc:
        raise _http_error(exc) from exc


@router.post("/{execution_id}/reject", response_model=ActionExecutionResponse)
def reject_action_request(
    execution_id: str,
    payload: ActionRejectRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return _execution_payload(db, reject_action(db, execution_id, user, payload.reason))
    except ActionExecutionError as exc:
        raise _http_error(exc) from exc
