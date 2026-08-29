from __future__ import annotations

from datetime import datetime
import hashlib
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actions import ActionContext, ActionExecutionError, propose_action
from ..database import get_db
from ..dependencies import get_current_user, require_roles
from ..models import (
    AuditLog,
    AutomationFormSession,
    Conversation,
    IdentityVerification,
    RestActionEndpoint,
    User,
    UserRole,
    utcnow,
)
from ..services.rest_actions import (
    RestActionSecurityError,
    normalize_action_path,
    validate_public_origin,
)
from ..services.secret_store import SecretStoreError, encrypt_secret


router = APIRouter(prefix="/api/automation", tags=["automation"])
admin_only = require_roles(UserRole.ADMIN)
support_staff = require_roles(UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)


_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,120}$")
_FORBIDDEN_SECRET_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
}


def _normalize_methods(values: list[str]) -> list[str]:
    methods = list(dict.fromkeys(str(item).strip().upper() for item in values))
    if not methods or any(item not in _HTTP_METHODS for item in methods):
        raise ValueError("仅支持 GET、POST、PUT、PATCH、DELETE")
    return methods


def _normalize_header(value: str | None) -> str | None:
    if value is None:
        return None
    name = value.strip()
    if not _HEADER_NAME.fullmatch(name) or name.casefold() in _FORBIDDEN_SECRET_HEADERS:
        raise ValueError("凭证 Header 名称无效或不允许")
    return name


class RestEndpointCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    base_url: str = Field(min_length=8, max_length=2048)
    path_pattern: str = Field(min_length=1, max_length=500)
    allowed_methods: list[str] = Field(min_length=1, max_length=5)
    timeout_seconds: int = Field(default=10, ge=2, le=30)
    requires_identity_verification: bool = False
    secret_header_name: str | None = None
    secret_value: SecretStr | None = None

    @field_validator("allowed_methods")
    @classmethod
    def validate_methods(cls, value: list[str]) -> list[str]:
        return _normalize_methods(value)

    @field_validator("secret_header_name")
    @classmethod
    def validate_header(cls, value: str | None) -> str | None:
        return _normalize_header(value)

    @model_validator(mode="after")
    def validate_secret_pair(self):
        if bool(self.secret_header_name) != bool(self.secret_value):
            raise ValueError("凭证 Header 与密钥必须同时提供")
        return self


class RestEndpointUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    base_url: str | None = Field(default=None, min_length=8, max_length=2048)
    path_pattern: str | None = Field(default=None, min_length=1, max_length=500)
    allowed_methods: list[str] | None = Field(default=None, min_length=1, max_length=5)
    timeout_seconds: int | None = Field(default=None, ge=2, le=30)
    requires_identity_verification: bool | None = None
    secret_header_name: str | None = None
    secret_value: SecretStr | None = None
    clear_secret: bool = False

    @field_validator("allowed_methods")
    @classmethod
    def validate_methods(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_methods(value) if value is not None else None

    @field_validator("secret_header_name")
    @classmethod
    def validate_header(cls, value: str | None) -> str | None:
        return _normalize_header(value)


class RestEndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    base_url: str
    path_pattern: str
    allowed_methods: list[str]
    timeout_seconds: int
    requires_identity_verification: bool
    secret_header_name: str | None
    secret_fingerprint: str | None
    has_secret: bool
    status: str
    created_by_user_id: int
    approved_by_user_id: int | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class IdentityVerificationRequest(BaseModel):
    method: Literal[
        "order_details",
        "registered_phone",
        "email_otp",
        "sms_otp",
        "staff_review",
    ]
    evidence_reference: SecretStr = Field(min_length=3, max_length=500)
    evidence_hint: str = Field(default="", max_length=120)
    expires_minutes: int = Field(default=30, ge=5, le=240)


class IdentityVerificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: int
    contact_id: int
    status: str
    method: str
    evidence_hint: str
    verified_by_user_id: int
    expires_at: datetime
    created_at: datetime


class AutomationSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: int
    contact_id: int
    workflow_key: str
    operation: str
    status: str
    current_step: int
    definition_json: dict[str, Any]
    answers_json: dict[str, Any]
    score: int | None
    grade: str | None
    expires_at: datetime
    paused_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _endpoint_payload(item: RestActionEndpoint) -> RestEndpointResponse:
    return RestEndpointResponse(
        id=item.id,
        name=item.name,
        description=item.description,
        base_url=item.base_url,
        path_pattern=item.path_pattern,
        allowed_methods=list(item.allowed_methods or []),
        timeout_seconds=item.timeout_seconds,
        requires_identity_verification=item.requires_identity_verification,
        secret_header_name=item.secret_header_name,
        secret_fingerprint=item.secret_fingerprint,
        has_secret=bool(item.secret_ciphertext),
        status=item.status,
        created_by_user_id=item.created_by_user_id,
        approved_by_user_id=item.approved_by_user_id,
        approved_at=item.approved_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _audit(db: Session, user: User, action: str, item: RestActionEndpoint) -> None:
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action=action,
            entity_type="rest_action_endpoint",
            entity_id=str(item.id),
            details={
                "name": item.name,
                "base_url": item.base_url,
                "path_pattern": item.path_pattern,
                "allowed_methods": item.allowed_methods,
                "status": item.status,
                "has_secret": bool(item.secret_ciphertext),
            },
        )
    )


def _get_endpoint(db: Session, user: User, endpoint_id: int) -> RestActionEndpoint:
    item = db.get(RestActionEndpoint, endpoint_id)
    if item is None or item.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="REST Action 连接器不存在")
    return item


def _validated_policy(base_url: str, path_pattern: str) -> tuple[str, str]:
    try:
        origin = validate_public_origin(base_url, resolve_dns=False)
        pattern = normalize_action_path(path_pattern, allow_wildcard=True)
    except RestActionSecurityError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    return origin.url, pattern


@router.get("/rest-endpoints", response_model=list[RestEndpointResponse])
def list_rest_endpoints(
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    rows = db.scalars(
        select(RestActionEndpoint)
        .where(RestActionEndpoint.tenant_id == user.tenant_id)
        .order_by(RestActionEndpoint.name)
    ).all()
    return [_endpoint_payload(item) for item in rows]


@router.post("/rest-endpoints", response_model=RestEndpointResponse)
def create_rest_endpoint(
    payload: RestEndpointCreate,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    base_url, pattern = _validated_policy(payload.base_url, payload.path_pattern)
    ciphertext = fingerprint = None
    if payload.secret_value is not None:
        try:
            ciphertext, fingerprint = encrypt_secret(payload.secret_value.get_secret_value())
        except SecretStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    item = RestActionEndpoint(
        tenant_id=user.tenant_id,
        name=payload.name.strip(),
        description=payload.description.strip(),
        base_url=base_url,
        path_pattern=pattern,
        allowed_methods=payload.allowed_methods,
        timeout_seconds=payload.timeout_seconds,
        requires_identity_verification=payload.requires_identity_verification,
        secret_header_name=payload.secret_header_name,
        secret_ciphertext=ciphertext,
        secret_fingerprint=fingerprint,
        status="draft",
        created_by_user_id=user.id,
    )
    db.add(item)
    db.flush()
    _audit(db, user, "rest_action_endpoint.created", item)
    db.commit()
    db.refresh(item)
    return _endpoint_payload(item)


@router.patch("/rest-endpoints/{endpoint_id}", response_model=RestEndpointResponse)
def update_rest_endpoint(
    endpoint_id: int,
    payload: RestEndpointUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    item = _get_endpoint(db, user, endpoint_id)
    values = payload.model_dump(exclude_unset=True, exclude={"secret_value", "clear_secret"})
    base_url, pattern = _validated_policy(
        str(values.get("base_url", item.base_url)),
        str(values.get("path_pattern", item.path_pattern)),
    )
    values["base_url"] = base_url
    values["path_pattern"] = pattern
    for key, value in values.items():
        setattr(item, key, value.strip() if isinstance(value, str) else value)
    if payload.clear_secret:
        item.secret_header_name = None
        item.secret_ciphertext = None
        item.secret_fingerprint = None
    elif payload.secret_value is not None:
        if not payload.secret_header_name and not item.secret_header_name:
            raise HTTPException(status_code=422, detail="保存密钥前必须指定凭证 Header")
        try:
            item.secret_ciphertext, item.secret_fingerprint = encrypt_secret(
                payload.secret_value.get_secret_value()
            )
        except SecretStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    item.status = "draft"
    item.approved_by_user_id = None
    item.approved_at = None
    item.updated_at = utcnow()
    _audit(db, user, "rest_action_endpoint.updated", item)
    db.commit()
    db.refresh(item)
    return _endpoint_payload(item)


@router.post("/rest-endpoints/{endpoint_id}/approve", response_model=RestEndpointResponse)
def approve_rest_endpoint(
    endpoint_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    item = _get_endpoint(db, user, endpoint_id)
    try:
        origin = validate_public_origin(item.base_url, resolve_dns=True)
        normalize_action_path(item.path_pattern, allow_wildcard=True)
    except RestActionSecurityError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    item.base_url = origin.url
    item.status = "approved"
    item.approved_by_user_id = user.id
    item.approved_at = utcnow()
    item.updated_at = utcnow()
    _audit(db, user, "rest_action_endpoint.approved", item)
    db.commit()
    db.refresh(item)
    return _endpoint_payload(item)


@router.post("/rest-endpoints/{endpoint_id}/disable", response_model=RestEndpointResponse)
def disable_rest_endpoint(
    endpoint_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    item = _get_endpoint(db, user, endpoint_id)
    item.status = "disabled"
    item.approved_by_user_id = None
    item.approved_at = None
    item.updated_at = utcnow()
    _audit(db, user, "rest_action_endpoint.disabled", item)
    db.commit()
    db.refresh(item)
    return _endpoint_payload(item)


@router.get("/sessions", response_model=list[AutomationSessionResponse])
def list_automation_sessions(
    conversation_id: int | None = Query(default=None, ge=1),
    status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    statement = select(AutomationFormSession).where(
        AutomationFormSession.tenant_id == user.tenant_id
    )
    if conversation_id is not None:
        statement = statement.where(AutomationFormSession.conversation_id == conversation_id)
    if status:
        statement = statement.where(AutomationFormSession.status == status)
    return db.scalars(
        statement.order_by(AutomationFormSession.updated_at.desc()).limit(limit)
    ).all()


@router.get(
    "/conversations/{conversation_id}/identity-verifications",
    response_model=list[IdentityVerificationResponse],
)
def list_identity_verifications(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(support_staff),
):
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return db.scalars(
        select(IdentityVerification)
        .where(
            IdentityVerification.tenant_id == user.tenant_id,
            IdentityVerification.conversation_id == conversation_id,
        )
        .order_by(IdentityVerification.created_at.desc())
    ).all()


@router.post(
    "/conversations/{conversation_id}/identity-verifications",
    response_model=IdentityVerificationResponse,
)
def verify_conversation_identity(
    conversation_id: int,
    payload: IdentityVerificationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(support_staff),
):
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    evidence = payload.evidence_reference.get_secret_value().strip()
    evidence_hash = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    hint = payload.evidence_hint.strip() or f"***{evidence[-4:]}"
    try:
        execution = propose_action(
            db,
            ActionContext.for_user(user),
            "identity.verify",
            {
                "conversation_id": conversation.id,
                "method": payload.method,
                "evidence_hash": evidence_hash,
                "evidence_hint": hint[:120],
                "expires_minutes": payload.expires_minutes,
            },
        )
    except ActionExecutionError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    verification = db.get(IdentityVerification, execution.result_json.get("identity_verification_id"))
    if verification is None:
        raise HTTPException(status_code=500, detail="身份核验记录未保存")
    return verification


__all__ = ["router"]
