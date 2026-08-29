from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..actions import ActionContext, ActionExecutionError, propose_action
from ..channels import ChannelProviderError
from ..config import settings
from ..database import get_db
from ..dependencies import get_current_user, require_roles
from ..models import ActionStatus, AuditLog, Contact, Conversation, Message, Team, User, UserRole, QuickReply, utcnow
from ..schemas import (
    AgentResponse,
    ConversationActivity,
    ConversationDetail,
    ConversationSummary,
    ConversationUpdate,
    InboxStats,
    MessageResponse,
    MessageTranslationResponse,
    QuickReplyCreate,
    QuickReplyResponse,
    QuickReplyUpdate,
    SendMessageRequest,
    SimulateInboundRequest,
    SimulateInboundResponse,
    TeamResponse,
    WorkspaceResponse,
)
from ..services.conversations import (
    DeliveryConflictError,
    load_conversation,
    receive_inbound,
    reconcile_outbound_message,
    retry_failed_message,
    send_agent_message,
    serialize_conversation,
)
from ..services.translation import (
    TranslationInputError,
    TranslationProviderError,
    TranslationUnavailableError,
    translate_english_to_traditional,
)


router = APIRouter(prefix="/api", tags=["inbox"])


def _run_user_action(
    db: Session,
    user: User,
    name: str,
    arguments: dict,
):
    try:
        execution = propose_action(
            db,
            ActionContext.for_user(user),
            name,
            arguments,
        )
    except ActionExecutionError as exc:
        status_code = 403 if exc.code.endswith("forbidden") or "permission" in exc.code else 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if execution.status != ActionStatus.SUCCEEDED.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": execution.error_code or execution.status,
                "message": execution.failure_reason or "Action failed",
                "action_execution_id": execution.id,
            },
        )
    return execution


@router.get("/workspace", response_model=WorkspaceResponse)
def workspace(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    active_agents = db.scalar(
        select(func.count(User.id)).where(
            User.tenant_id == user.tenant_id,
            User.is_active.is_(True),
        )
    ) or 0
    return WorkspaceResponse(
        max_agent_seats=settings.max_agent_seats,
        active_agents=active_agents,
        supported_locales=["zh-TW"],
        default_locale="zh-TW",
    )


@router.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(
    response: Response,
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = None,
    view: str = Query(default="all", pattern="^(all|mine|unassigned|unread|waiting)$"),
    team_id: int | None = Query(default=None, ge=1),
    assigned_user_id: int | None = Query(default=None, ge=1),
    priority: str | None = Query(default=None, pattern="^(low|normal|high|urgent)$"),
    sort: str = Query(default="newest", pattern="^(newest|oldest|priority)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    predicates = [Conversation.tenant_id == user.tenant_id]
    if status_filter and status_filter != "all":
        predicates.append(Conversation.status == status_filter)
    if view == "mine":
        predicates.append(Conversation.assigned_user_id == user.id)
    elif view == "unassigned":
        predicates.append(Conversation.assigned_user_id.is_(None))
    elif view == "unread":
        predicates.append(Conversation.unread_count > 0)
    elif view == "waiting":
        predicates.append(Conversation.status == "pending")
    if team_id is not None:
        predicates.append(Conversation.assigned_team_id == team_id)
    if assigned_user_id is not None:
        predicates.append(Conversation.assigned_user_id == assigned_user_id)
    if priority:
        predicates.append(Conversation.priority == priority)
    if search:
        pattern = f"%{search.strip()}%"
        predicates.append(
            or_(
                Contact.display_name.ilike(pattern),
                Contact.phone.ilike(pattern),
                Conversation.subject.ilike(pattern),
            )
        )
    statement = (
        select(Conversation)
        .join(Contact)
        .options(
            selectinload(Conversation.contact),
            selectinload(Conversation.messages),
            selectinload(Conversation.assigned_team),
            selectinload(Conversation.assigned_user),
        )
        .where(*predicates)
    )
    handoff_first = case(
        (
            and_(
                Conversation.status == "pending",
                Conversation.ai_route == "handoff",
                Conversation.ai_enabled.is_(False),
            ),
            0,
        ),
        else_=1,
    )
    if sort == "oldest":
        statement = statement.order_by(
            handoff_first,
            Conversation.last_message_at.asc(),
            Conversation.id.asc(),
        )
    elif sort == "priority":
        statement = statement.order_by(
            handoff_first,
            case(
                (Conversation.priority == "urgent", 0),
                (Conversation.priority == "high", 1),
                (Conversation.priority == "normal", 2),
                else_=3,
            ),
            Conversation.last_message_at.desc(),
            Conversation.id.desc(),
        )
    else:
        statement = statement.order_by(
            handoff_first,
            Conversation.last_message_at.desc(),
            Conversation.id.desc(),
        )
    total = db.scalar(select(func.count(Conversation.id)).join(Contact).where(*predicates)) or 0
    records = db.scalars(statement.offset((page - 1) * page_size).limit(page_size)).unique().all()
    response.headers["X-Total-Count"] = str(total)
    return [serialize_conversation(item) for item in records]


@router.get("/inbox/stats", response_model=InboxStats)
def inbox_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    base = select(Conversation).where(Conversation.tenant_id == user.tenant_id)
    def count(*extra):
        return int(db.scalar(select(func.count()).select_from(base.where(*extra).subquery())) or 0)

    return InboxStats(
        all=count(),
        open=count(Conversation.status == "open"),
        pending=count(Conversation.status == "pending"),
        solved=count(Conversation.status == "solved"),
        unread=count(Conversation.unread_count > 0),
        unassigned=count(Conversation.assigned_user_id.is_(None)),
        mine=count(Conversation.assigned_user_id == user.id),
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = load_conversation(db, user.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return serialize_conversation(conversation, detail=True)


@router.patch("/conversations/{conversation_id}", response_model=ConversationDetail)
def update_conversation(
    conversation_id: int,
    payload: ConversationUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = load_conversation(db, user.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    values = payload.model_dump(exclude_unset=True, exclude={"mark_read"})
    assignment = {}
    if "assigned_team_id" in values:
        assignment["team_id"] = values["assigned_team_id"]
    if "assigned_user_id" in values:
        assignment["user_id"] = values["assigned_user_id"]
    if assignment:
        _run_user_action(
            db,
            user,
            "conversation.assign",
            {"conversation_id": conversation.id, **assignment},
        )
    if "ai_enabled" in values:
        _run_user_action(
            db,
            user,
            "conversation.resume_ai" if values["ai_enabled"] else "conversation.handoff",
            {
                "conversation_id": conversation.id,
                "reason": "manual_ai_resume" if values["ai_enabled"] else "manual_ai_pause",
            },
        )
    state_changes = {
        key: values[key]
        for key in ("status", "priority")
        if key in values
    }
    if state_changes:
        _run_user_action(
            db,
            user,
            "conversation.update",
            {
                "conversation_id": conversation.id,
                **state_changes,
                "reason": (
                    f"manual_status_{state_changes['status']}"
                    if state_changes.get("status") in {"solved", "blocked"}
                    else None
                ),
            },
        )
    conversation = db.get(Conversation, conversation.id)
    assert conversation is not None
    if payload.mark_read:
        conversation.unread_count = 0
        conversation.updated_at = utcnow()
        db.add(
            AuditLog(
                tenant_id=user.tenant_id,
                user_id=user.id,
                action="conversation.read",
                entity_type="conversation",
                entity_id=str(conversation.id),
                details={},
            )
        )
        db.commit()
    refreshed = load_conversation(db, user.tenant_id, conversation_id)
    assert refreshed is not None
    return serialize_conversation(refreshed, detail=True)


@router.post("/conversations/{conversation_id}/messages", response_model=MessageResponse)
def create_agent_message(
    conversation_id: int,
    payload: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = load_conversation(db, user.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not payload.internal and (conversation.status == "blocked" or conversation.contact.is_blocked):
        raise HTTPException(status_code=409, detail="联系人已被阻止")
    message = send_agent_message(
        db,
        conversation,
        user,
        payload.body.strip(),
        internal=payload.internal,
    )
    return MessageResponse.model_validate(message)


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/translate",
    response_model=MessageTranslationResponse,
)
def translate_message_to_traditional(
    conversation_id: int,
    message_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    message = db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
            Message.tenant_id == user.tenant_id,
        )
    )
    if message is None:
        raise HTTPException(status_code=404, detail="訊息不存在")
    if message.content_type != "text" or not message.body.strip():
        raise HTTPException(status_code=422, detail="只支援翻譯文字訊息")
    try:
        translated = translate_english_to_traditional(message.body)
    except TranslationInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TranslationUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TranslationProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return MessageTranslationResponse(message_id=message.id, translated_text=translated)


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/retry",
    response_model=MessageResponse,
)
def retry_message(
    conversation_id: int,
    message_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = load_conversation(db, user.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if conversation.status == "blocked" or conversation.contact.is_blocked:
        raise HTTPException(status_code=409, detail="联系人已被阻止")
    try:
        message = retry_failed_message(db, conversation, message_id, user)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="外发消息不存在") from exc
    except DeliveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return MessageResponse.model_validate(message)


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/delivery/reconcile",
    response_model=MessageResponse,
)
def reconcile_message_delivery(
    conversation_id: int,
    message_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = load_conversation(db, user.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        message = reconcile_outbound_message(db, conversation, message_id, user)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="外发消息不存在") from exc
    except DeliveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ChannelProviderError as exc:
        raise HTTPException(status_code=502, detail="无法从 WhatsApp 通道查询消息状态") from exc
    return MessageResponse.model_validate(message)


@router.post("/demo/inbound", response_model=SimulateInboundResponse, status_code=status.HTTP_201_CREATED)
def simulate_inbound(
    payload: SimulateInboundRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if settings.whatsapp_provider != "demo":
        raise HTTPException(status_code=409, detail="真实 WhatsApp 通道下禁止模拟入站消息")
    normalized = "".join(character for character in payload.phone if character.isdigit())
    result = receive_inbound(
        db,
        tenant_id=user.tenant_id,
        wa_id=normalized,
        phone=payload.phone,
        display_name=payload.display_name,
        body=payload.body.strip(),
    )
    return SimulateInboundResponse(
        conversation=serialize_conversation(result.conversation, detail=True),
        agent_route=result.agent_result.route if result.agent_result else "duplicate",
        agent_answered=result.agent_result is not None,
    )


@router.get("/teams", response_model=list[TeamResponse])
def list_teams(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.scalars(select(Team).where(Team.tenant_id == user.tenant_id).order_by(Team.name)).all()


@router.get("/agents", response_model=list[AgentResponse])
def list_agents(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(User).where(User.tenant_id == user.tenant_id, User.is_active.is_(True)).order_by(User.name)
    ).all()


@router.get("/conversations/{conversation_id}/activity", response_model=list[ConversationActivity])
def conversation_activity(
    conversation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = load_conversation(db, user.tenant_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    logs = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == user.tenant_id,
            AuditLog.entity_type == "conversation",
            AuditLog.entity_id == str(conversation_id),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    ).all()
    users = {
        item.id: item.name
        for item in db.scalars(select(User).where(User.tenant_id == user.tenant_id)).all()
    }
    return [
        ConversationActivity(
            id=item.id,
            action=item.action,
            user_name=users.get(item.user_id) if item.user_id else None,
            details=item.details or {},
            created_at=item.created_at,
        )
        for item in logs
    ]


@router.get("/quick-replies", response_model=list[QuickReplyResponse])
def list_quick_replies(
    language: str | None = Query(default=None, max_length=16),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(QuickReply).where(
        QuickReply.tenant_id == user.tenant_id,
        QuickReply.is_active.is_(True),
    )
    if language:
        statement = statement.where(QuickReply.language == language)
    return db.scalars(statement.order_by(QuickReply.title)).all()


@router.post(
    "/quick-replies",
    response_model=QuickReplyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_quick_reply(
    payload: QuickReplyCreate,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
    db: Session = Depends(get_db),
):
    item = QuickReply(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="快捷回复标识已存在") from exc
    db.refresh(item)
    return item


@router.patch("/quick-replies/{reply_id}", response_model=QuickReplyResponse)
def update_quick_reply(
    reply_id: int,
    payload: QuickReplyUpdate,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
    db: Session = Depends(get_db),
):
    item = db.get(QuickReply, reply_id)
    if item is None or item.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="快捷回复不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="快捷回复标识已存在") from exc
    db.refresh(item)
    return item


@router.delete("/quick-replies/{reply_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quick_reply(
    reply_id: int,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
    db: Session = Depends(get_db),
):
    item = db.get(QuickReply, reply_id)
    if item is None or item.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="快捷回复不存在")
    item.is_active = False
    db.commit()
