from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..actions import ActionContext, propose_action
from ..config import settings
from ..models import (
    AuditLog,
    ActionStatus,
    ChannelContactIdentity,
    Contact,
    Conversation,
    ConversationStatus,
    Message,
    MessageDeliveryAttempt,
    MessageDirection,
    MessageSender,
    Team,
    User,
    utcnow,
)
from ..channels import ensure_default_channel_account, get_channel_provider
from ..contact_attributes import EVOLUTION_RECIPIENT_JID_KEY
from ..schemas import ContactSummary, ConversationDetail, ConversationSummary, MessageResponse
from .agent import (
    AI_OUTBOUND_LANGUAGE,
    AgentResult,
    _fallback_classify,
    answer_has_language_mismatch,
    answer_implies_handoff,
    _detect_language,
    format_ai_customer_message,
    normalize_ai_outbound_text,
    support_agent_workflow,
)
from .business_automation import process_business_automation
from .action_messaging import send_text_action
from .conversation_sessions import (
    CONTEXT_SESSION_ID_KEY,
    latest_context_session_id,
    mark_context_session_closed,
    prepare_inbound_context,
)
from .realtime import publish_inbox_updated
from .delivery import (
    reconcile_delivery_receipts,
    record_delivery_attempt,
    record_delivery_receipt,
)
from .product_price_query import (
    RentalPeriod,
    is_rental_duration_addition,
    is_product_catalog_query,
    product_price_subject_score,
    rental_period_from_payload,
)


@dataclass(slots=True)
class InboundResult:
    conversation: Conversation
    agent_result: AgentResult | None


class DeliveryConflictError(Exception):
    """The requested retry/reconciliation is unsafe for the message state."""


_UNKNOWN_FALLBACK_REASON = "本地规则无法可靠分类"
_KNOWLEDGE_FOLLOWUP_OUT_OF_SCOPE_TERMS = (
    "天气",
    "天氣",
    "景点",
    "景點",
    "景区",
    "景區",
    "餐厅",
    "餐廳",
    "酒店",
    "住宿",
    "机票",
    "機票",
    "航班",
    "weather",
    "restaurant",
    "hotel",
    "accommodation",
    "flight",
)


def _is_knowledge_followup_message(
    message: str,
    *,
    fallback_intent: str,
    fallback_reason: str,
) -> bool:
    """Recognize short product follow-ups without widening support scope."""

    # Explicit handoffs, high-risk requests, and order mutations always win.
    if fallback_intent == "order":
        return False
    if fallback_intent == "handoff" and fallback_reason != _UNKNOWN_FALLBACK_REASON:
        return False
    normalized = re.sub(r"\s+", "", message.casefold())
    if any(term in normalized for term in _KNOWLEDGE_FOLLOWUP_OUT_OF_SCOPE_TERMS):
        return False
    if re.search(
        r"(?:链接|連結|网址|網址|购买|購買|下单|下單|选择|選擇|无限|無限|"
        r"用量|流量|容量|租借|租用|推荐|推薦|适合|適合)",
        normalized,
    ):
        return True
    if re.search(
        r"(?:\d+|[一二三四五六七八九十百两兩]+)\s*(?:天|日|人|位|部|個|个|套)",
        normalized,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:link|url|buy|purchase|checkout|choose|select|unlimited|"
            r"usage|data|days?|people|users?|devices?|suitable)\b",
            message,
            re.I,
        )
    )


def _previous_product_context_query(
    db: Session,
    tenant_id: int,
    visible: list[Message],
    last_ai_metadata: dict,
) -> str | None:
    """Find the latest customer query that established a product context."""

    stored = str(last_ai_metadata.get("context_query") or "").strip()
    if stored:
        return stored[:1000]
    customer_queries = [
        item.body.strip()
        for item in visible
        if item.sender_type == MessageSender.CUSTOMER.value and item.body.strip()
    ]
    if not customer_queries:
        return None
    # Prefer an actual destination/product match over a terse follow-up such as
    # “给我链接”.  Keep the scan bounded to avoid repeated catalogue queries on
    # very long conversations.
    for query in reversed(customer_queries[-10:]):
        try:
            if product_price_subject_score(db, tenant_id, query) >= 70:
                return query[:1000]
        except Exception:
            continue
    sources = last_ai_metadata.get("sources")
    if isinstance(sources, list) and any(
        isinstance(source, dict) and source.get("source_type") == "structured_product"
        for source in sources
    ):
        return customer_queries[-1][:1000]
    return None


def _recent_agent_context(
    db: Session,
    tenant_id: int,
    conversation_id: int,
    current_message_id: int,
) -> tuple[list[str], str | None, str | None, str | None]:
    """Return active-session history and any durable price continuation."""

    current_message = db.get(Message, current_message_id)
    current_metadata = dict(current_message.metadata_json or {}) if current_message else {}
    session_id = str(current_metadata.get(CONTEXT_SESSION_ID_KEY) or "")

    recent = list(
        reversed(
            db.scalars(
                select(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.conversation_id == conversation_id,
                    Message.id != current_message_id,
                    Message.direction != MessageDirection.INTERNAL.value,
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(min(settings.ai_context_max_messages * 3, 600))
            ).all()
        )
    )
    visible = [
        item
        for item in recent
        if item.delivery_status != "failed"
        and (
            not session_id
            or str((item.metadata_json or {}).get(CONTEXT_SESSION_ID_KEY) or "") == session_id
        )
    ]
    history: list[str] = []
    for item in visible[-settings.ai_context_max_messages :]:
        if item.sender_type == MessageSender.CUSTOMER.value:
            role = "customer"
        elif item.sender_type == MessageSender.AI.value:
            role = "assistant"
        else:
            role = "agent"
        history.append(f"{role}: {item.body[:4000]}")

    while history and sum(len(item) for item in history) > settings.ai_context_max_characters:
        history.pop(0)

    if not visible or visible[-1].sender_type != MessageSender.AI.value:
        return history, None, None, None
    last_ai = visible[-1]
    metadata = last_ai.metadata_json or {}
    awaiting = metadata.get("awaiting_input")
    # Compatibility with clarification replies written before the structured
    # pending-input metadata was introduced.
    clarification_markers = (
        "请告诉我目的地或商品名称",
        "請告訴我目的地或商品名稱",
        "Please tell me the destination or product name",
    )
    if not awaiting and metadata.get("route") == "pricing" and any(
        marker in last_ai.body for marker in clarification_markers
    ):
        awaiting = "pricing_filter"

    current_body = current_message.body if current_message is not None else ""
    fallback = _fallback_classify(current_body)
    normalized = re.sub(r"\s+", "", current_body)
    contextual_cue = bool(
        len(normalized) <= 80
        and re.search(
            r"(?:這|这|那|呢|改成|換成|换成|再|比較|比较)"
            r"|(?:(?:\d+|[一二兩两三四五六七八九十百]+)(?:位|個|个|人|天|日|份|套|部)?)",
            normalized,
        )
        or is_rental_duration_addition(current_body)
    )
    price_subject_score = product_price_subject_score(db, tenant_id, current_body)
    known_price_subject = price_subject_score > 0
    # Destination and exact product matches score at least 70 in the catalogue
    # matcher. Such a current request supersedes an older destination instead
    # of being appended to it. A bare answer to an active clarification (for
    # example just "韩国") remains a genuine continuation.
    pending_filter_short_reply = bool(
        awaiting == "pricing_filter"
        and len(normalized) <= 24
        and not re.search(
            r"(?:价格|價格|价钱|價錢|多少钱|多少錢|推荐|推薦|"
            r"哪(?:个|個|款)|套餐|方案|划算|便宜|改成|换成|換成)",
            normalized,
        )
    )
    standalone_current_request = bool(
        price_subject_score >= 70
        and not pending_filter_short_reply
        # A destination repeated alongside “add/extend N days” is still a
        # continuation of the existing quote, not a fresh  N-day request.
        and not is_rental_duration_addition(current_body)
        and (
            fallback.intent == "pricing"
            or fallback.reason == "本地规则无法可靠分类"
        )
    )
    language = str(metadata.get("language") or "").strip()
    if language not in {"zh-CN", "zh-TW", "en"}:
        language = None
    previous_customer = next(
        (
            item
            for item in reversed(visible[:-1])
            if item.sender_type == MessageSender.CUSTOMER.value
        ),
        None,
    )
    previous_customer_query = previous_customer.body.strip() if previous_customer else ""
    if metadata.get("route") != "pricing":
        # A short price follow-up can safely inherit a structured product or
        # destination from the immediately preceding knowledge question. This
        # supports “中国有哪些产品” -> “多少钱” without carrying unrelated FAQs.
        if (
            metadata.get("route") == "knowledge"
            and fallback.intent == "pricing"
            and price_subject_score < 70
            and previous_customer_query
            and product_price_subject_score(db, tenant_id, previous_customer_query) >= 70
        ):
            return history, "pricing", previous_customer_query[:1000], language
        if metadata.get("route") == "knowledge":
            # Knowledge answers can also have terse follow-ups (“给我链接”,
            # “用量大”, “选择无限的 10 日”).  Reuse the product context while
            # leaving intent classification to the normal catalogue/scope
            # checks.  A clearly new destination/product remains standalone.
            current_is_catalog_query = is_product_catalog_query(
                db,
                tenant_id,
                current_body,
            )
            product_context_query = _previous_product_context_query(
                db,
                tenant_id,
                visible,
                metadata,
            )
            if (
                not current_is_catalog_query
                and product_context_query
                and _is_knowledge_followup_message(
                    current_body,
                    fallback_intent=fallback.intent,
                    fallback_reason=fallback.reason,
                )
            ):
                return history, None, product_context_query, language
            # A bare known destination/model is a valid short continuation
            # even without a cue word (for example, replying just “日本”).
            if (
                not current_is_catalog_query
                and product_context_query
                and known_price_subject
                and len(normalized) <= 24
                and fallback.intent == "handoff"
                and fallback.reason == _UNKNOWN_FALLBACK_REASON
                and not any(
                    term in normalized
                    for term in _KNOWLEDGE_FOLLOWUP_OUT_OF_SCOPE_TERMS
                )
            ):
                return history, None, product_context_query, language
        return history, None, None, None

    if standalone_current_request:
        return history, "pricing", None, language

    continue_pricing = bool(
        awaiting == "pricing_filter"
        or (
            fallback.intent == "pricing"
            and (contextual_cue or not known_price_subject)
        )
        or (
            fallback.reason == "本地规则无法可靠分类"
            and (
                contextual_cue
                or known_price_subject
            )
        )
    )
    if not continue_pricing:
        return history, None, None, None

    context_query = str(metadata.get("context_query") or "").strip()
    if not context_query:
        context_query = previous_customer_query
    return history, "pricing", context_query[:1000] or None, language


def _latest_agent_rental_period(
    db: Session,
    conversation_id: int,
    current_message_id: int,
) -> RentalPeriod | None:
    """Read the latest quoted rental period for the active context session.

    Keep this lookup separate from ``_recent_agent_context`` so that the
    latter's private four-value return contract remains compatible with older
    callers while the durable period metadata can still be used for follow-up
    calculations.
    """

    current = db.get(Message, current_message_id)
    if current is None:
        return None
    session_id = str((current.metadata_json or {}).get(CONTEXT_SESSION_ID_KEY) or "")
    if not session_id:
        return None
    candidates = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.id != current_message_id,
            Message.direction == MessageDirection.OUTBOUND.value,
            Message.sender_type == MessageSender.AI.value,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(50)
    ).all()
    for message in candidates:
        metadata = message.metadata_json or {}
        if str(metadata.get(CONTEXT_SESSION_ID_KEY) or "") != session_id:
            continue
        if message.delivery_status == "failed":
            continue
        period = rental_period_from_payload(metadata.get("rental_period"))
        if period is not None:
            return period
    return None


def load_conversation(db: Session, tenant_id: int, conversation_id: int) -> Conversation | None:
    conversation = db.scalar(
        select(Conversation)
        .options(
            selectinload(Conversation.contact),
            selectinload(Conversation.messages),
            selectinload(Conversation.assigned_team),
            selectinload(Conversation.assigned_user),
        )
        .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if conversation is not None:
        reconcile_delivery_receipts(db, conversation.messages)
    return conversation


def serialize_conversation(conversation: Conversation, detail: bool = False):
    last = next(
        (
            item
            for item in reversed(conversation.messages)
            if item.direction != MessageDirection.INTERNAL.value
        ),
        None,
    )
    values = {
        "id": conversation.id,
        "contact": ContactSummary.model_validate(conversation.contact),
        "status": conversation.status,
        "priority": conversation.priority,
        "subject": conversation.subject,
        "assigned_team_id": conversation.assigned_team_id,
        "assigned_user_id": conversation.assigned_user_id,
        "assigned_team": conversation.assigned_team.name if conversation.assigned_team else None,
        "assigned_user": conversation.assigned_user.name if conversation.assigned_user else None,
        "ai_enabled": conversation.ai_enabled,
        "ai_route": conversation.ai_route,
        "unread_count": conversation.unread_count,
        "last_message": last.body if last else "",
        "last_message_sender": last.sender_type if last else None,
        "last_message_at": conversation.last_message_at,
        "service_window_expires_at": conversation.service_window_expires_at,
    }
    if detail:
        return ConversationDetail(
            **values,
            messages=[MessageResponse.model_validate(item) for item in conversation.messages],
        )
    return ConversationSummary(**values)


def receive_inbound(
    db: Session,
    *,
    tenant_id: int,
    wa_id: str,
    phone: str,
    display_name: str,
    body: str,
    external_id: str | None = None,
    content_type: str = "text",
    evolution_recipient_jid: str | None = None,
    channel_account_id: int | None = None,
    provider: str | None = None,
    sender_address: str | None = None,
    provider_metadata: dict | None = None,
) -> InboundResult:
    account = ensure_default_channel_account(db, tenant_id) if channel_account_id is None else None
    if account is not None:
        channel_account_id = account.id
        provider = provider or account.provider
    if external_id:
        existing = db.scalar(
            select(Message).where(Message.tenant_id == tenant_id, Message.external_id == external_id)
        )
        if existing:
            conversation = load_conversation(db, tenant_id, existing.conversation_id)
            assert conversation is not None
            return InboundResult(conversation=conversation, agent_result=None)

    contact = db.scalar(
        select(Contact).where(Contact.tenant_id == tenant_id, Contact.wa_id == wa_id)
    )
    if contact is None:
        custom_attributes = {}
        if evolution_recipient_jid:
            custom_attributes[EVOLUTION_RECIPIENT_JID_KEY] = evolution_recipient_jid
        contact = Contact(
            tenant_id=tenant_id,
            wa_id=wa_id,
            phone=phone,
            display_name=display_name or "WhatsApp customer",
            custom_attributes=custom_attributes,
        )
        db.add(contact)
        db.flush()
    elif display_name and contact.display_name != display_name:
        contact.display_name = display_name
    if evolution_recipient_jid:
        attributes = dict(contact.custom_attributes or {})
        if attributes.get(EVOLUTION_RECIPIENT_JID_KEY) != evolution_recipient_jid:
            attributes[EVOLUTION_RECIPIENT_JID_KEY] = evolution_recipient_jid
            contact.custom_attributes = attributes

    if channel_account_id is not None:
        identity = db.scalar(
            select(ChannelContactIdentity).where(
                ChannelContactIdentity.channel_account_id == channel_account_id,
                ChannelContactIdentity.external_user_id == wa_id,
            )
        )
        identity_address = sender_address or evolution_recipient_jid or wa_id
        if identity is None:
            identity = ChannelContactIdentity(
                tenant_id=tenant_id,
                contact_id=contact.id,
                channel_account_id=channel_account_id,
                external_user_id=wa_id,
                address=identity_address,
                provider_metadata=dict(provider_metadata or {}),
            )
            db.add(identity)
        else:
            identity.contact_id = contact.id
            identity.address = identity_address
            identity.provider_metadata = dict(provider_metadata or identity.provider_metadata or {})
            identity.updated_at = utcnow()

    # Persist the latest detected language for staff visibility only. The
    # Agent always derives its reply language from the current message and
    # never treats this contact field as an instruction.
    detected_language = _detect_language(body) if content_type == "text" else None
    if detected_language in {"zh-CN", "zh-TW", "en"}:
        contact.language = detected_language

    conversation_query = select(Conversation).where(
        Conversation.tenant_id == tenant_id,
        Conversation.contact_id == contact.id,
    )
    if channel_account_id is not None:
        conversation_query = conversation_query.where(
            Conversation.channel_account_id == channel_account_id
        )
    conversation = db.scalar(
        conversation_query
        .order_by(Conversation.updated_at.desc())
    )
    force_new_context = conversation is None
    if conversation is None:
        default_team = db.scalar(
            select(Team).where(Team.tenant_id == tenant_id, Team.is_default.is_(True))
        )
        conversation = Conversation(
            tenant_id=tenant_id,
            contact_id=contact.id,
            channel_account_id=channel_account_id,
            subject=body[:120] or "WhatsApp message",
            assigned_team_id=default_team.id if default_team else None,
        )
        db.add(conversation)
        db.flush()
    elif conversation.status in {ConversationStatus.SOLVED.value, ConversationStatus.EXPIRED.value}:
        force_new_context = True
        conversation.status = ConversationStatus.OPEN.value

    now = utcnow()
    context_metadata = (
        prepare_inbound_context(
            db,
            conversation,
            now=now,
            force_new=force_new_context,
        )
        if conversation.ai_enabled and not contact.is_blocked
        else {}
    )
    inbound = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        channel_account_id=channel_account_id,
        provider=provider,
        external_id=external_id or f"local-in-{uuid4().hex}",
        direction=MessageDirection.INBOUND.value,
        sender_type=MessageSender.CUSTOMER.value,
        sender_name=contact.display_name,
        content_type=content_type,
        body=body,
        metadata_json={
            **context_metadata,
            "detected_language": detected_language,
        },
        created_at=now,
    )
    db.add(inbound)
    # A handoff is a durable human-work queue item. Further customer messages
    # must not silently remove it from the pending queue while AI is paused.
    is_pending_handoff = (
        conversation.status == ConversationStatus.PENDING.value
        and conversation.ai_route == "handoff"
        and not conversation.ai_enabled
    )
    if not is_pending_handoff:
        conversation.status = ConversationStatus.OPEN.value
    conversation.unread_count += 1
    conversation.last_message_at = now
    conversation.service_window_expires_at = now + timedelta(hours=24)
    conversation.updated_at = now
    db.commit()

    agent_result = None
    if conversation.ai_enabled and not contact.is_blocked and content_type == "text":
        automation = process_business_automation(
            db,
            conversation=conversation,
            contact=contact,
            message=body,
            source_message_id=inbound.id,
        )
        if automation is not None:
            automation_language = automation.language
            automation_reply = normalize_ai_outbound_text(
                automation.reply,
                language=automation_language,
            )
            agent_result = AgentResult(
                route=automation.route,
                answer=automation_reply,
                handoff=automation.handoff,
                sources=[],
                reply_parts=[automation_reply],
                awaiting_input=automation.awaiting_input,
                language=automation_language,
                agent_profile_version_id=automation.agent_profile_version_id,
            )
            _store_agent_result(
                db,
                conversation,
                contact,
                agent_result,
                source_message_id=inbound.id,
            )
        else:
            history, continuation_intent, context_query, preferred_language = _recent_agent_context(
                db,
                tenant_id,
                conversation.id,
                inbound.id,
            )
            previous_rental_period = (
                _latest_agent_rental_period(db, conversation.id, inbound.id)
                if continuation_intent == "pricing" and context_query
                else None
            )
            agent_kwargs = {
                "tenant_id": tenant_id,
                "conversation_id": conversation.id,
                "customer_name": contact.display_name,
                "customer_phone": contact.phone,
                "message": body,
                "history": history,
                "continuation_intent": continuation_intent,
                "context_query": context_query,
                "preferred_language": preferred_language,
            }
            if previous_rental_period is not None:
                agent_kwargs["previous_rental_period"] = previous_rental_period
            agent_result = support_agent_workflow.run(db, **agent_kwargs)
            _store_agent_result(
                db,
                conversation,
                contact,
                agent_result,
                source_message_id=inbound.id,
            )
    elif content_type != "text":
        agent_result = AgentResult(
            route="handoff",
            answer="我已收到媒體訊息，並轉交人工客服繼續處理。",
            handoff=True,
            sources=[],
            language=AI_OUTBOUND_LANGUAGE,
        )
        _store_agent_result(
            db,
            conversation,
            contact,
            agent_result,
            source_message_id=inbound.id,
        )

    loaded = load_conversation(db, tenant_id, conversation.id)
    assert loaded is not None
    publish_inbox_updated(
        tenant_id,
        activity="message",
        conversation_id=conversation.id,
        sender_type=MessageSender.CUSTOMER.value,
    )
    return InboundResult(conversation=loaded, agent_result=agent_result)


def _store_agent_result(
    db: Session,
    conversation: Conversation,
    contact: Contact,
    result: AgentResult,
    *,
    source_message_id: int,
) -> None:
    context_session_id = latest_context_session_id(db, conversation.id)
    language = result.language or AI_OUTBOUND_LANGUAGE
    raw_parts = [
        str(part).strip()
        for part in (result.reply_parts or [result.answer])
        if str(part).strip()
    ]
    safety_reason: str | None = None
    if any(answer_has_language_mismatch(part, language) for part in raw_parts):
        safe_answer = support_agent_workflow._handoff_answer(language, insufficient=True)
        raw_parts = [safe_answer]
        result.answer = safe_answer
        result.reply_parts = raw_parts
        result.sources = []
        result.handoff = True
        result.route = "handoff"
        safety_reason = "language_mismatch"
    elif not result.handoff and any(answer_implies_handoff(part) for part in raw_parts):
        result.handoff = True
        result.route = "handoff"
        safety_reason = "handoff_text"
    normalized_sources = [
        {
            **source,
            "title": normalize_ai_outbound_text(
                str(source.get("title", "")), language=language
            ),
        }
        for source in result.sources
    ]
    if not result.handoff and normalized_sources and raw_parts:
        citations: list[str] = []
        seen_urls: set[str] = set()
        for source in normalized_sources:
            url = str(source.get("source_url") or source.get("source") or "").strip()
            if not url.startswith(("http://", "https://")) or url in seen_urls:
                continue
            seen_urls.add(url)
            title = str(source.get("title") or "").strip()
            section = str(source.get("section_path") or "").strip()
            label = title if not section or section == title else f"{title} · {section}"
            citations.append(f"[{len(citations) + 1}] {label}\n{url}")
        if citations:
            heading = "Sources" if language == "en" else ("参考来源" if language == "zh-CN" else "參考來源")
            raw_parts[-1] = f"{raw_parts[-1]}\n\n{heading}\n" + "\n".join(citations[:5])
    parts = [
        normalize_ai_outbound_text(str(part), language=language)
        if result.handoff
        else format_ai_customer_message(str(part), language=language)
        for part in raw_parts
    ]
    if result.handoff:
        proposals = result.action_proposals or [
            {
                "name": "conversation.handoff",
                "arguments": {"reason": safety_reason or result.route or "agent_handoff"},
            }
        ]
        for proposal_index, proposal in enumerate(proposals):
            if proposal.get("name") != "conversation.handoff":
                raise RuntimeError("The AI proposed an unapproved state-changing action")
            supplied = proposal.get("arguments")
            arguments = dict(supplied) if isinstance(supplied, dict) else {}
            # Conversation ownership is supplied by trusted orchestration, not
            # accepted from model-generated arguments.
            arguments["conversation_id"] = conversation.id
            arguments["reason"] = str(
                arguments.get("reason") or safety_reason or result.route or "agent_handoff"
            )[:500]
            handoff = propose_action(
                db,
                ActionContext.for_model(
                    conversation.tenant_id,
                    source_message_id=source_message_id,
                ),
                "conversation.handoff",
                arguments,
                idempotency_key=f"handoff:{source_message_id}:{proposal_index + 1}",
            )
            if handoff.status != ActionStatus.SUCCEEDED.value:
                raise RuntimeError(
                    f"Human handoff action failed: {handoff.error_code or handoff.status}"
                )
    for index, body in enumerate(parts):
        message_metadata = {
            "route": result.route,
            "sources": normalized_sources,
            "part_index": index + 1,
            "part_count": len(parts),
            "awaiting_input": result.awaiting_input,
            "context_query": result.context_query,
            "language": result.language or AI_OUTBOUND_LANGUAGE,
            "safety_handoff_reason": safety_reason,
            CONTEXT_SESSION_ID_KEY: context_session_id,
            "context_closed": False,
            "context_paused": result.handoff,
            "agent_profile_version_id": result.agent_profile_version_id,
        }
        # Keep the legacy metadata shape for replies without a quote while
        # persisting the deterministic period whenever one was calculated.
        if result.rental_period is not None:
            message_metadata["rental_period"] = result.rental_period
        send_text_action(
            db,
            conversation,
            context=ActionContext.for_system(
                conversation.tenant_id,
                source_message_id=source_message_id,
            ),
            body=body,
            sender_type=MessageSender.AI.value,
            sender_name="RelayDesk AI",
            metadata=message_metadata,
            idempotency_key=f"ai-reply:{source_message_id}:{index + 1}",
        )
    conversation = db.get(Conversation, conversation.id)
    if conversation is not None and not result.handoff:
        conversation.ai_route = result.route
        conversation.updated_at = utcnow()
        db.commit()


def send_agent_message(
    db: Session,
    conversation: Conversation,
    user: User,
    body: str,
    *,
    internal: bool = False,
) -> Message:
    if not internal:
        message = send_text_action(
            db,
            conversation,
            context=ActionContext.for_user(user),
            body=body,
            sender_type=MessageSender.AGENT.value,
            sender_name=user.name,
        )
        db.add(
            AuditLog(
                tenant_id=user.tenant_id,
                user_id=user.id,
                action="conversation.message_sent",
                entity_type="conversation",
                entity_id=str(conversation.id),
                details={
                    "internal": False,
                    "delivery_status": message.delivery_status,
                    "action_execution_id": (message.metadata_json or {}).get(
                        "action_execution_id"
                    ),
                },
            )
        )
        db.commit()
        db.refresh(message)
        return message
    now = utcnow()
    message = Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        direction=MessageDirection.INTERNAL.value,
        sender_type=MessageSender.AGENT.value,
        sender_name=user.name,
        body=body,
        delivery_status="internal",
        metadata_json={"internal": True},
        created_at=now,
    )
    db.add(message)
    conversation.assigned_user_id = user.id
    conversation.unread_count = 0
    conversation.updated_at = now
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="conversation.note_added",
            entity_type="conversation",
            entity_id=str(conversation.id),
            details={"internal": True},
        )
    )
    db.commit()
    db.refresh(message)
    publish_inbox_updated(
        conversation.tenant_id,
        activity="state",
        conversation_id=conversation.id,
        sender_type=MessageSender.AGENT.value,
    )
    return message


def retry_failed_message(
    db: Session,
    conversation: Conversation,
    message_id: int,
    user: User,
) -> Message:
    """Retry only a definitively failed outbound message.

    Pending or sent messages are intentionally rejected because a missing
    receipt does not prove non-delivery and retrying could duplicate a customer
    message.
    """

    message = db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.tenant_id == conversation.tenant_id,
            Message.conversation_id == conversation.id,
            Message.direction == MessageDirection.OUTBOUND.value,
        )
    )
    if message is None:
        raise LookupError("Outbound message not found")
    if message.delivery_status != "failed":
        raise DeliveryConflictError("Only failed messages can be retried")

    _ensure_delivery_attempt(db, message)
    claimed = db.execute(
        update(Message)
        .where(Message.id == message.id, Message.delivery_status == "failed")
        .values(delivery_status="pending")
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise DeliveryConflictError("Message is already being retried")
    db.commit()
    db.refresh(message)

    message = send_text_action(
        db,
        conversation,
        context=ActionContext.for_user(user),
        body=message.body,
        sender_type=message.sender_type,
        sender_name=message.sender_name or user.name,
        metadata=dict(message.metadata_json or {}),
        idempotency_key=f"manual-retry:{message.id}:{uuid4().hex}",
        existing_message=message,
    )
    attempt = db.scalar(
        select(MessageDeliveryAttempt)
        .where(MessageDeliveryAttempt.message_id == message.id)
        .order_by(MessageDeliveryAttempt.attempt_number.desc())
        .limit(1)
    )
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action="conversation.message_retried",
            entity_type="message",
            entity_id=str(message.id),
            details={
                "attempt_number": attempt.attempt_number if attempt else None,
                "accepted": message.delivery_status != "failed",
            },
        )
    )
    db.commit()
    db.refresh(message)
    return message


def reconcile_outbound_message(
    db: Session,
    conversation: Conversation,
    message_id: int,
    user: User,
) -> Message:
    """Fetch persisted Evolution updates when a webhook was missed or malformed."""

    provider = get_channel_provider(
        db,
        conversation.tenant_id,
        conversation.channel_account_id,
    )
    if provider.provider_name != "evolution":
        raise DeliveryConflictError("Delivery reconciliation requires Evolution")
    message = db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.tenant_id == conversation.tenant_id,
            Message.conversation_id == conversation.id,
            Message.direction == MessageDirection.OUTBOUND.value,
        )
    )
    if message is None:
        raise LookupError("Outbound message not found")
    if not message.external_id:
        raise DeliveryConflictError("Message has no provider ID to reconcile")

    _ensure_delivery_attempt(db, message)
    previous = message.delivery_status
    for status in provider.delivery_statuses(message.external_id):
        record_delivery_receipt(db, message.external_id, status)
    db.refresh(message)
    if message.delivery_status != previous:
        db.add(
            AuditLog(
                tenant_id=user.tenant_id,
                user_id=user.id,
                action="conversation.delivery_reconciled",
                entity_type="message",
                entity_id=str(message.id),
                details={"before": previous, "after": message.delivery_status},
            )
        )
        db.commit()
        db.refresh(message)
    return message


def _ensure_delivery_attempt(db: Session, message: Message) -> None:
    existing = db.scalar(
        select(MessageDeliveryAttempt.id).where(
            MessageDeliveryAttempt.message_id == message.id
        )
    )
    if existing is None:
        record_delivery_attempt(
            db,
            message,
            provider=message.provider or settings.whatsapp_provider,
            external_id=message.external_id,
            delivery_status=message.delivery_status,
        )
