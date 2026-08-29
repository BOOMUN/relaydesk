from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Contact,
    Conversation,
    ConversationPriority,
    ConversationStatus,
    KnowledgeDocument,
    Message,
    MessageDeliveryAttempt,
    MessageDirection,
    MessageSender,
    QuickReply,
    Team,
    TeamMember,
    Tenant,
    User,
    UserRole,
    utcnow,
)
from .security import hash_password


def seed_database(db: Session) -> None:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == "default"))
    if tenant is None:
        tenant = Tenant(name="RelayDesk Demo", slug="default")
        db.add(tenant)
        db.flush()

    admin = db.scalar(
        select(User).where(User.tenant_id == tenant.id, User.email == settings.admin_email)
    )
    if admin is None:
        admin = User(
            tenant_id=tenant.id,
            name="系统管理员",
            email=settings.admin_email,
            password_hash=hash_password(settings.admin_password),
            role=UserRole.ADMIN.value,
        )
        db.add(admin)
        db.flush()

    support = db.scalar(
        select(Team).where(Team.tenant_id == tenant.id, Team.name == "客户支持")
    )
    if support is None:
        support = Team(
            tenant_id=tenant.id,
            name="客户支持",
            description="默认接收 AI 转交与人工服务请求",
            is_default=True,
        )
        db.add(support)
        db.flush()
        db.add(TeamMember(team_id=support.id, user_id=admin.id))

    if not db.scalar(select(QuickReply.id).where(QuickReply.tenant_id == tenant.id)):
        db.add_all(
            [
                QuickReply(
                    tenant_id=tenant.id,
                    shortcut="greeting",
                    title="首次问候",
                    body="您好，感谢联系客户服务。我来帮您处理这个问题。",
                    language="zh-CN",
                ),
                QuickReply(
                    tenant_id=tenant.id,
                    shortcut="waiting",
                    title="正在核实",
                    body="我正在为您核实信息，请稍等片刻。",
                    language="zh-CN",
                ),
                QuickReply(
                    tenant_id=tenant.id,
                    shortcut="resolved",
                    title="问题已处理",
                    body="这个问题已经为您处理完成。如有其他需要，欢迎继续留言。",
                    language="zh-CN",
                ),
                QuickReply(
                    tenant_id=tenant.id,
                    shortcut="greeting-tw",
                    title="首次問候",
                    body="您好，感謝聯絡客戶服務。我來幫您處理這個問題。",
                    language="zh-TW",
                ),
            ]
        )

    if settings.seed_demo_data:
        _seed_knowledge(db, tenant.id)
        _seed_demo_conversations(db, tenant.id, support.id, admin.id)
    from .channels.factory import ensure_default_channel_account

    channel_account = ensure_default_channel_account(db, tenant.id)
    db.query(Conversation).filter(
        Conversation.tenant_id == tenant.id,
        Conversation.channel_account_id.is_(None),
    ).update(
        {Conversation.channel_account_id: channel_account.id},
        synchronize_session=False,
    )
    db.query(Message).filter(
        Message.tenant_id == tenant.id,
        Message.channel_account_id.is_(None),
    ).update(
        {
            Message.channel_account_id: channel_account.id,
            Message.provider: channel_account.provider,
        },
        synchronize_session=False,
    )
    db.query(MessageDeliveryAttempt).filter(
        MessageDeliveryAttempt.tenant_id == tenant.id,
        MessageDeliveryAttempt.channel_account_id.is_(None),
    ).update(
        {MessageDeliveryAttempt.channel_account_id: channel_account.id},
        synchronize_session=False,
    )
    db.commit()


def _seed_knowledge(db: Session, tenant_id: int) -> None:
    if db.scalar(select(KnowledgeDocument.id).where(KnowledgeDocument.tenant_id == tenant_id)):
        return
    db.add_all(
        [
            KnowledgeDocument(
                tenant_id=tenant_id,
                title="客服服务时间",
                category="service",
                source="demo://service-hours",
                content=(
                    "在线人工客服为每天 24 小时服务。"
                    "客户要求人工客服时，系统会暂停 AI 并创建待处理会话。"
                ),
            ),
            KnowledgeDocument(
                tenant_id=tenant_id,
                title="退换货政策",
                category="policy",
                source="demo://returns",
                content=(
                    "商品签收后 7 天内，如保持完整且不影响二次销售，可以申请退货。"
                    "质量问题请提供订单号和商品照片，客服确认后安排处理。"
                    "涉及退款金额和最终审批时必须转交人工客服。"
                ),
            ),
            KnowledgeDocument(
                tenant_id=tenant_id,
                title="订单查询说明",
                category="orders",
                source="demo://orders",
                content=(
                    "订单查询需要提供订单号。演示订单 ORD-1001 已发货，承运商为顺丰，"
                    "预计两个工作日内送达。修改地址、取消订单或退款必须经过身份验证。"
                ),
            ),
            KnowledgeDocument(
                tenant_id=tenant_id,
                title="爽WiFi VIP计划",
                category="policy",
                source="https://songwifi.com.hk/vip-plan",
                content=(
                    "预订任何地区或国家的 WiFi 蛋累计租满 3 次即可免费升级为 VIP。"
                    "VIP 可享全单额外 9 折；银行转账付款可享免押金。"
                    "取机时出示 VIP 确认 WhatsApp 讯息，可任选一份免费外游小礼物：无线充电器、"
                    "四合一充电头、一拖三数据线或旅行收纳袋；礼物数量有限，送完即止。"
                    "网上下单时在优惠券栏输入登记电话号并按验证，即可套用 VIP 优惠价。"
                ),
            ),
        ]
    )


def _seed_demo_conversations(db: Session, tenant_id: int, team_id: int, user_id: int) -> None:
    if db.scalar(select(Conversation.id).where(Conversation.tenant_id == tenant_id)):
        return
    now = utcnow()
    samples = [
        {
            "wa_id": "8613800001001",
            "name": "陈女士",
            "body": "请问你们的退货期限是多久？",
            "answer": "商品簽收後 7 天內，在符合退貨條件時可以申請退貨。",
            "status": ConversationStatus.OPEN.value,
            "route": "knowledge",
            "minutes": 4,
            "tags": ["售后", "退换货"],
        },
        {
            "wa_id": "8613800001002",
            "name": "Jason Lee",
            "body": "I need a human agent for a damaged item.",
            "answer": "已轉交人工客服，團隊會繼續處理您的商品損壞問題。",
            "status": ConversationStatus.PENDING.value,
            "route": "handoff",
            "minutes": 23,
            "tags": ["投诉", "待人工"],
        },
        {
            "wa_id": "8613800001003",
            "name": "王先生",
            "body": "查询订单 ORD-1001",
            "answer": "訂單 ORD-1001 已發貨，預計兩個工作日內送達。",
            "status": ConversationStatus.SOLVED.value,
            "route": "order",
            "minutes": 58,
            "tags": ["订单"],
        },
    ]
    for index, sample in enumerate(samples):
        timestamp = now - timedelta(minutes=sample["minutes"])
        contact = Contact(
            tenant_id=tenant_id,
            wa_id=sample["wa_id"],
            phone=f"+{sample['wa_id']}",
            display_name=sample["name"],
            language="en" if index == 1 else "zh-CN",
            tags=sample["tags"],
        )
        db.add(contact)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant_id,
            contact_id=contact.id,
            status=sample["status"],
            priority=(
                ConversationPriority.HIGH.value if index == 1 else ConversationPriority.NORMAL.value
            ),
            subject=sample["body"][:80],
            assigned_team_id=team_id,
            assigned_user_id=user_id if index == 2 else None,
            ai_enabled=index != 1,
            ai_route=sample["route"],
            unread_count=1 if index == 0 else 0,
            service_window_expires_at=timestamp + timedelta(hours=24),
            last_message_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
        )
        db.add(conversation)
        db.flush()
        db.add_all(
            [
                Message(
                    tenant_id=tenant_id,
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND.value,
                    sender_type=MessageSender.CUSTOMER.value,
                    sender_name=sample["name"],
                    body=sample["body"],
                    created_at=timestamp - timedelta(seconds=20),
                ),
                Message(
                    tenant_id=tenant_id,
                    conversation_id=conversation.id,
                    direction=MessageDirection.OUTBOUND.value,
                    sender_type=(MessageSender.AGENT.value if index == 1 else MessageSender.AI.value),
                    sender_name="系统管理员" if index == 1 else "RelayDesk AI",
                    body=sample["answer"],
                    delivery_status="delivered",
                    metadata_json={
                        "route": sample["route"],
                        **({"language": "zh-TW"} if index != 1 else {}),
                    },
                    created_at=timestamp,
                ),
            ]
        )
