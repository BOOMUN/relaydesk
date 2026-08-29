from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..dependencies import get_current_user
from ..models import Contact, Conversation, Message, User
from ..schemas import DashboardMetric, DashboardResponse
from ..services.conversations import serialize_conversation


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardResponse)
def dashboard(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = user.tenant_id
    status_rows = db.execute(
        select(Conversation.status, func.count(Conversation.id))
        .where(Conversation.tenant_id == tenant_id)
        .group_by(Conversation.status)
    ).all()
    route_rows = db.execute(
        select(Conversation.ai_route, func.count(Conversation.id))
        .where(Conversation.tenant_id == tenant_id, Conversation.ai_route.is_not(None))
        .group_by(Conversation.ai_route)
    ).all()
    total_contacts = db.scalar(select(func.count(Contact.id)).where(Contact.tenant_id == tenant_id)) or 0
    total_messages = db.scalar(select(func.count(Message.id)).where(Message.tenant_id == tenant_id)) or 0
    ai_messages = db.scalar(
        select(func.count(Message.id)).where(Message.tenant_id == tenant_id, Message.sender_type == "ai")
    ) or 0
    open_count = sum(count for status, count in status_rows if status in {"open", "pending"})
    handoff_count = sum(count for route, count in route_rows if route == "handoff")
    routed_total = sum(count for _, count in route_rows) or 1
    recent = db.scalars(
        select(Conversation)
        .options(
            selectinload(Conversation.contact),
            selectinload(Conversation.messages),
            selectinload(Conversation.assigned_team),
            selectinload(Conversation.assigned_user),
        )
        .where(Conversation.tenant_id == tenant_id)
        .order_by(Conversation.updated_at.desc())
        .limit(5)
    ).all()
    return DashboardResponse(
        metrics=[
            DashboardMetric(label="待处理会话", value=open_count),
            DashboardMetric(label="客户总数", value=total_contacts),
            DashboardMetric(label="消息总量", value=total_messages),
            DashboardMetric(label="AI 回复", value=ai_messages),
            DashboardMetric(
                label="转人工率",
                value=round(handoff_count * 100 / routed_total, 1),
                unit="%",
            ),
        ],
        status_counts={status: count for status, count in status_rows},
        route_counts={(route or "unclassified"): count for route, count in route_rows},
        recent_conversations=[serialize_conversation(item) for item in recent],
    )
