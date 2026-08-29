from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actions import ActionContext, ActionExecutionError, propose_action
from ..database import get_db
from ..dependencies import get_current_user
from ..models import (
    ActionStatus,
    ChannelAccount,
    Message,
    User,
    WhatsAppTemplate,
    WhatsAppTemplateSyncRun,
)
from ..schemas import MessageResponse


router = APIRouter(prefix="/api", tags=["channels"])


class ChannelAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    name: str
    external_account_id: str
    phone_number_id: str | None
    business_account_id: str | None
    instance_name: str | None
    capabilities: list[str]
    is_default: bool
    is_active: bool
    connection_state: str
    last_checked_at: datetime | None


class WhatsAppTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_account_id: int
    provider_template_id: str | None
    name: str
    language: str
    category: str
    status: str
    parameter_format: str
    components: list[dict[str, Any]]
    quality_rating: str | None
    rejection_reason: str | None
    is_active: bool
    last_synced_at: datetime


class TemplateSyncRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_account_id: int
    status: str
    template_count: int
    approved_count: int
    failure_reason: str | None
    started_at: datetime
    completed_at: datetime | None


class TemplateSyncRequest(BaseModel):
    channel_account_id: int | None = Field(default=None, ge=1)


class TemplateSendRequest(BaseModel):
    template_id: int = Field(ge=1)
    components: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    sender_name: str | None = Field(default=None, max_length=120)


class InteractiveButtonRequest(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=20)


class InteractiveRowRequest(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=24)
    description: str | None = Field(default=None, max_length=72)


class InteractiveSectionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=24)
    rows: list[InteractiveRowRequest] = Field(min_length=1, max_length=10)


class InteractiveSendRequest(BaseModel):
    kind: Literal["buttons", "list"]
    body: str = Field(min_length=1, max_length=1024)
    header: str | None = Field(default=None, max_length=60)
    footer: str | None = Field(default=None, max_length=60)
    buttons: list[InteractiveButtonRequest] = Field(default_factory=list, max_length=3)
    button_text: str | None = Field(default=None, max_length=20)
    sections: list[InteractiveSectionRequest] = Field(default_factory=list, max_length=10)
    sender_name: str | None = Field(default=None, max_length=120)


def _run_action(
    db: Session,
    user: User,
    name: str,
    arguments: dict[str, Any],
):
    try:
        execution = propose_action(
            db,
            ActionContext.for_user(user),
            name,
            arguments,
        )
    except ActionExecutionError as exc:
        status_code = 403 if "permission" in exc.code or "forbidden" in exc.code else 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if execution.status != ActionStatus.SUCCEEDED.value:
        raise HTTPException(
            status_code=502 if name.startswith("whatsapp.") else 409,
            detail={
                "code": execution.error_code or execution.status,
                "message": execution.failure_reason or "Action failed",
                "action_execution_id": execution.id,
            },
        )
    return execution


@router.get("/channels/accounts", response_model=list[ChannelAccountResponse])
def list_channel_accounts(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(ChannelAccount)
        .where(ChannelAccount.tenant_id == user.tenant_id)
        .order_by(ChannelAccount.is_default.desc(), ChannelAccount.name)
    ).all()


@router.get("/whatsapp/templates", response_model=list[WhatsAppTemplateResponse])
def list_whatsapp_templates(
    status: str | None = Query(default=None, max_length=40),
    language: str | None = Query(default=None, max_length=32),
    active_only: bool = True,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(WhatsAppTemplate).where(
        WhatsAppTemplate.tenant_id == user.tenant_id
    )
    if status:
        statement = statement.where(WhatsAppTemplate.status == status.upper())
    if language:
        statement = statement.where(WhatsAppTemplate.language == language)
    if active_only:
        statement = statement.where(WhatsAppTemplate.is_active.is_(True))
    return db.scalars(
        statement.order_by(WhatsAppTemplate.name, WhatsAppTemplate.language)
    ).all()


@router.get(
    "/whatsapp/templates/sync-runs",
    response_model=list[TemplateSyncRunResponse],
)
def list_template_sync_runs(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(WhatsAppTemplateSyncRun)
        .where(WhatsAppTemplateSyncRun.tenant_id == user.tenant_id)
        .order_by(WhatsAppTemplateSyncRun.started_at.desc())
        .limit(limit)
    ).all()


@router.post("/whatsapp/templates/sync", response_model=TemplateSyncRunResponse)
def sync_whatsapp_templates(
    payload: TemplateSyncRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    execution = _run_action(
        db,
        user,
        "whatsapp.templates.sync",
        payload.model_dump(exclude_unset=True),
    )
    run = db.get(WhatsAppTemplateSyncRun, execution.result_json.get("sync_run_id"))
    if run is None:
        raise HTTPException(status_code=500, detail="Template sync run was not recorded")
    return run


def _message_for_execution(db: Session, execution) -> Message:
    message = db.get(Message, execution.result_json.get("message_id"))
    if message is None:
        raise HTTPException(status_code=500, detail="Outbound message was not recorded")
    return message


@router.post(
    "/conversations/{conversation_id}/whatsapp/template",
    response_model=MessageResponse,
)
def send_whatsapp_template(
    conversation_id: int,
    payload: TemplateSendRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    execution = _run_action(
        db,
        user,
        "whatsapp.template.send",
        {"conversation_id": conversation_id, **payload.model_dump()},
    )
    return _message_for_execution(db, execution)


@router.post(
    "/conversations/{conversation_id}/whatsapp/interactive",
    response_model=MessageResponse,
)
def send_whatsapp_interactive(
    conversation_id: int,
    payload: InteractiveSendRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    execution = _run_action(
        db,
        user,
        "whatsapp.interactive.send",
        {
            "conversation_id": conversation_id,
            **payload.model_dump(mode="json"),
        },
    )
    return _message_for_execution(db, execution)
