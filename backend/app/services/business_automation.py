from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..actions import ActionContext, propose_action
from ..models import (
    ActionStatus,
    AutomationFormEvent,
    AutomationFormSession,
    Contact,
    Conversation,
    utcnow,
)
from .agent_profiles import published_agent_configuration


ACTIVE_FORM_STATUSES = {"active", "paused"}
ORDER_NUMBER_PATTERN = re.compile(r"\b[A-Z0-9]{2,10}[-_][A-Z0-9-]{3,30}\b", re.I)
DATE_PATTERNS = (
    re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b"),
    re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]?"),
)

_PAUSE_TERMS = (
    "暂停填写",
    "暫停填寫",
    "先暂停",
    "先暫停",
    "稍后继续",
    "稍後繼續",
    "pause form",
    "pause this form",
)
_RESUME_TERMS = (
    "继续填写",
    "繼續填寫",
    "继续刚才",
    "繼續剛才",
    "恢复表单",
    "恢復表單",
    "resume form",
    "continue form",
)
_HANDOFF_TERMS = (
    "转人工",
    "轉人工",
    "人工客服",
    "真人客服",
    "找客服",
    "human agent",
    "live agent",
    "talk to a person",
)

_ORDER_OPERATION_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "change_address",
        (
            re.compile(r"(?:修改|更改|改).{0,10}(?:收货|收貨|配送|送货|送貨)?地址"),
            re.compile(r"change.{0,20}(?:shipping|delivery) address", re.I),
        ),
    ),
    (
        "update_sensitive_data",
        (
            re.compile(r"(?:修改|更改|改).{0,10}(?:电话|電話|手机|手機|邮箱|郵箱|收件人|证件|證件)"),
            re.compile(r"change.{0,20}(?:phone|email|recipient|identity)", re.I),
        ),
    ),
    (
        "cancel_order",
        (
            re.compile(r"(?:(?:我要|帮我|幫我|申请|申請|需要|請|请).{0,8})?(?:取消|撤销|撤銷).{0,12}(?:订单|訂單|預訂|预订)"),
            re.compile(r"(?:cancel|void).{0,20}(?:my\s+)?(?:order|booking)", re.I),
        ),
    ),
    (
        "refund",
        (
            re.compile(r"(?:(?:我要|帮我|幫我|申请|申請|需要|請|请).{0,8})?(?:退款|退費|退钱|退錢)"),
            re.compile(r"(?:refund my|want (?:a )?refund|request (?:a )?refund)", re.I),
        ),
    ),
    (
        "inquiry",
        (
            re.compile(r"(?:查|查询|查詢|追踪|追蹤).{0,12}(?:订单|訂單|物流|包裹)"),
            re.compile(r"(?:订单|訂單).{0,8}(?:状态|狀態|到哪|进度|進度)"),
            re.compile(r"(?:track|check|where is).{0,20}(?:order|booking|package)", re.I),
            re.compile(r"(?:order|booking) status", re.I),
        ),
    ),
)

_DESTINATION_TERMS = (
    "日本",
    "韓國",
    "韩国",
    "台灣",
    "台湾",
    "香港",
    "澳門",
    "澳门",
    "泰國",
    "泰国",
    "新加坡",
    "馬來西亞",
    "马来西亚",
    "美國",
    "美国",
    "加拿大",
    "英國",
    "英国",
    "法國",
    "法国",
    "德國",
    "德国",
    "澳洲",
    "Japan",
    "Korea",
    "Taiwan",
    "Hong Kong",
    "Macau",
    "Thailand",
    "Singapore",
    "Malaysia",
    "United States",
    "USA",
    "Canada",
    "United Kingdom",
    "UK",
    "France",
    "Germany",
    "Australia",
)


class FormGraphState(TypedDict, total=False):
    message: str
    language: str
    fields: list[dict[str, Any]]
    answers: dict[str, Any]
    current_step: int
    status: str
    new_session: bool
    command: str
    modified_field: str | None
    candidate: str | None
    error: str | None
    reply: str
    handoff: bool
    transition_event: str


@dataclass(slots=True)
class AutomationOutcome:
    reply: str
    handoff: bool
    route: Literal["order", "knowledge", "handoff"]
    session_id: str
    language: str
    awaiting_input: str | None = None
    action_execution_ids: list[str] = field(default_factory=list)
    agent_profile_version_id: int | None = None


def _language(message: str) -> str:
    from .agent import _detect_language

    return _detect_language(message)


def _substantive_language(message: str) -> str | None:
    latin_words = re.findall(r"[A-Za-z][A-Za-z'-]+", message)
    if len(latin_words) >= 3:
        return "en"
    if len(re.findall(r"[\u3400-\u9fff]", message)) >= 3:
        return _language(message)
    return None


def _contains_term(message: str, terms: tuple[str, ...]) -> bool:
    normalized = message.casefold()
    return any(term.casefold() in normalized for term in terms)


def _detect_order_operation(message: str) -> str | None:
    policy_only = bool(
        re.search(r"(?:退款|退費|取消).{0,8}(?:政策|規則|规则|条件|條件|流程|多久)", message)
        or re.search(r"(?:refund|cancellation) (?:policy|rules?|eligibility|timeline)", message, re.I)
    )
    for operation, patterns in _ORDER_OPERATION_PATTERNS:
        if policy_only and operation in {"refund", "cancel_order"}:
            continue
        if any(pattern.search(message) for pattern in patterns):
            # A read-only lookup with an explicit reference belongs to the
            # existing order tool. The form handles incomplete lookups and all
            # sensitive mutations.
            if operation == "inquiry" and ORDER_NUMBER_PATTERN.search(message):
                return None
            return operation
    return None


def _normalize_date(value: str, *, today: date | None = None) -> str | None:
    raw = value.strip()
    base = today or datetime.now(timezone.utc).date()
    relative = raw.casefold()
    if relative in {"今天", "今日", "today"}:
        return base.isoformat()
    if relative in {"明天", "明日", "tomorrow"}:
        return (base + timedelta(days=1)).isoformat()
    match = DATE_PATTERNS[0].search(raw)
    if match:
        values = tuple(int(item) for item in match.groups())
        try:
            return date(values[0], values[1], values[2]).isoformat()
        except ValueError:
            return None
    match = DATE_PATTERNS[1].search(raw)
    if match:
        day, month, year = (int(item) for item in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    match = DATE_PATTERNS[2].search(raw)
    if match:
        month, day = (int(item) for item in match.groups())
        try:
            candidate = date(base.year, month, day)
            if candidate < base - timedelta(days=1):
                candidate = date(base.year + 1, month, day)
            return candidate.isoformat()
        except ValueError:
            return None
    for pattern in ("%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _order_fields(operation: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = [
        {
            "key": "order_number",
            "kind": "order_number",
            "required": True,
            "prompt": "請提供訂單號。",
            "prompt_en": "Please provide the order number.",
            "aliases": ["订单号", "訂單號", "订单", "訂單", "order number", "order id"],
        },
        {
            "key": "departure_date",
            "kind": "date",
            "required": True,
            "prompt": "請提供出發日期，例如 2026-09-15。",
            "prompt_en": "Please provide the departure date, for example 2026-09-15.",
            "aliases": ["出发日期", "出發日期", "日期", "departure date", "travel date"],
        },
        {
            "key": "destination",
            "kind": "text",
            "required": True,
            "prompt": "請提供目的地國家或地區。",
            "prompt_en": "Please provide the destination country or region.",
            "aliases": ["目的地", "国家", "國家", "destination", "country"],
        },
    ]
    if operation in {"refund", "cancel_order"}:
        fields.append(
            {
                "key": "reason",
                "kind": "text",
                "required": True,
                "prompt": "請簡要說明申請原因。",
                "prompt_en": "Please briefly state the reason for the request.",
                "aliases": ["原因", "reason"],
            }
        )
    elif operation == "change_address":
        fields.append(
            {
                "key": "requested_change",
                "kind": "text",
                "required": True,
                "prompt": "請提供需要改成的新地址；系統只會記錄，不會自動修改。",
                "prompt_en": "Please provide the new address. It will be recorded but not changed automatically.",
                "aliases": ["新地址", "地址", "new address", "shipping address"],
            }
        )
    elif operation == "update_sensitive_data":
        fields.append(
            {
                "key": "requested_change",
                "kind": "text",
                "required": True,
                "prompt": "請說明需要修改的資料；請勿在聊天中發送密碼或完整證件號碼。",
                "prompt_en": "Describe the requested change. Do not send passwords or full identity-document numbers in chat.",
                "aliases": ["修改内容", "修改內容", "资料", "資料", "requested change"],
            }
        )
    return fields


def _field_prompt(field: dict[str, Any], language: str) -> str:
    prompt = str(field.get("prompt_en") if language == "en" else field.get("prompt") or "").strip()
    options = list(field.get("options") or [])
    if options:
        labels = [str(item.get("label") or item.get("value")) for item in options]
        prompt = f"{prompt} ({' / '.join(labels)})"
    return prompt


def _validate_answer(field: dict[str, Any], value: str) -> tuple[bool, Any, str | None]:
    raw = value.strip()
    if not raw:
        return False, None, "答案不能為空"
    kind = str(field.get("kind") or "text")
    if kind == "order_number":
        match = ORDER_NUMBER_PATTERN.search(raw)
        if match:
            return True, match.group(0).upper(), None
        compact = re.sub(r"\s+", "", raw)
        if 5 <= len(compact) <= 40 and re.fullmatch(r"[A-Za-z0-9_-]+", compact):
            return True, compact.upper(), None
        return False, None, "訂單號格式不正確"
    if kind == "date":
        normalized = _normalize_date(raw)
        return (True, normalized, None) if normalized else (False, None, "日期格式不正確")
    if kind == "number":
        try:
            number = float(raw)
        except ValueError:
            return False, None, "請輸入數字"
        return True, int(number) if number.is_integer() else number, None
    if kind == "single_choice":
        for option in field.get("options") or []:
            if raw.casefold() in {
                str(option.get("value") or "").casefold(),
                str(option.get("label") or "").casefold(),
            }:
                return True, str(option.get("value")), None
        return False, None, "請選擇列出的選項"
    if len(raw) > 500:
        return False, None, "答案不能超過 500 個字符"
    return True, raw, None


def _first_missing(fields: list[dict[str, Any]], answers: dict[str, Any]) -> int:
    for index, item in enumerate(fields):
        if item.get("required", True) and item.get("key") not in answers:
            return index
    return len(fields)


def _extract_modification(message: str, fields: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    normalized = message.strip()
    if not re.search(r"(?:修改|更改|改成|change|update|edit)", normalized, re.I):
        return None, None
    for index, item in enumerate(fields):
        aliases = [str(item.get("key")), *(str(value) for value in item.get("aliases") or [])]
        aliases.extend((f"第{index + 1}题", f"第{index + 1}題", f"question {index + 1}"))
        for alias in aliases:
            match = re.search(
                rf"(?:修改|更改|改成|change|update|edit)\s*{re.escape(alias)}\s*(?:为|為|成|to|:|：|=)?\s*(.+)$",
                normalized,
                re.I,
            )
            if match:
                return str(item.get("key")), match.group(1).strip()
    return "", None


class BusinessFormGraph:
    def __init__(self) -> None:
        graph = StateGraph(FormGraphState)
        graph.add_node("interpret", self._interpret)
        graph.add_node("transition", self._transition)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "interpret")
        graph.add_edge("interpret", "transition")
        graph.add_edge("transition", "finalize")
        graph.add_edge("finalize", END)
        self.graph = graph.compile()

    @staticmethod
    def _interpret(state: FormGraphState) -> dict[str, Any]:
        message = str(state.get("message") or "").strip()
        if state.get("new_session"):
            return {"command": "start"}
        if _contains_term(message, _HANDOFF_TERMS):
            return {"command": "handoff"}
        if _contains_term(message, _PAUSE_TERMS):
            return {"command": "pause"}
        if _contains_term(message, _RESUME_TERMS):
            return {"command": "resume"}
        field_key, candidate = _extract_modification(message, state.get("fields", []))
        if field_key is not None:
            return {
                "command": "modify",
                "modified_field": field_key,
                "candidate": candidate,
            }
        return {"command": "answer", "candidate": message}

    @staticmethod
    def _transition(state: FormGraphState) -> dict[str, Any]:
        command = state.get("command")
        status = state.get("status", "active")
        fields = list(state.get("fields") or [])
        answers = dict(state.get("answers") or {})
        current = int(state.get("current_step") or 0)
        if command == "handoff":
            return {"status": "handed_off", "handoff": True, "transition_event": "handoff"}
        if command == "pause":
            return {"status": "paused", "transition_event": "paused"}
        if command == "resume":
            if status != "paused":
                return {"error": "表單目前沒有暫停", "transition_event": "resume_ignored"}
            return {"status": "active", "transition_event": "resumed"}
        if command == "start":
            return {"status": "active", "transition_event": "started"}
        if status == "paused":
            return {"error": "表單已暫停", "transition_event": "paused_message"}
        if command == "modify":
            key = state.get("modified_field")
            if not key:
                return {"error": "請說明要修改哪一項及新答案", "transition_event": "modify_invalid"}
            field = next((item for item in fields if item.get("key") == key), None)
            if field is None:
                return {"error": "找不到要修改的欄位", "transition_event": "modify_invalid"}
            valid, value, error = _validate_answer(field, str(state.get("candidate") or ""))
            if not valid:
                return {"error": error, "transition_event": "modify_invalid"}
            answers[str(key)] = value
            return {
                "answers": answers,
                "current_step": _first_missing(fields, answers),
                "transition_event": "answer_modified",
            }
        if current >= len(fields):
            return {"transition_event": "already_complete"}
        field = fields[current]
        valid, value, error = _validate_answer(field, str(state.get("candidate") or ""))
        if not valid:
            return {"error": error, "transition_event": "answer_invalid"}
        answers[str(field.get("key"))] = value
        return {
            "answers": answers,
            "current_step": _first_missing(fields, answers),
            "transition_event": "answer_recorded",
        }

    @staticmethod
    def _finalize(state: FormGraphState) -> dict[str, Any]:
        language = state.get("language", "zh-TW")
        status = state.get("status", "active")
        fields = list(state.get("fields") or [])
        answers = dict(state.get("answers") or {})
        current = _first_missing(fields, answers)
        error = state.get("error")
        if status == "handed_off":
            return {
                "reply": "I have stopped the form and transferred you to a human agent." if language == "en" else "已停止資料收集並為您轉交人工客服。",
                "handoff": True,
            }
        if status == "paused":
            return {
                "reply": "The form is paused. Send 'resume form' when you are ready." if language == "en" else "資料收集已暫停，準備好後發送「繼續填寫」即可恢復。",
                "handoff": False,
            }
        if error and state.get("command") == "resume":
            prefix = "" if language == "en" else f"{error}。"
        elif error:
            prefix = "That answer is not valid. " if language == "en" else f"{error}。"
        else:
            prefix = ""
        if current >= len(fields):
            return {
                "status": "completed",
                "current_step": current,
                "reply": "The required information has been collected." if language == "en" else "所需資料已收集完成。",
                "handoff": False,
            }
        prompt = _field_prompt(fields[current], language)
        if status == "active" and state.get("command") == "resume":
            prefix = "Form resumed. " if language == "en" else "已恢復資料收集。"
        return {
            "current_step": current,
            "reply": f"{prefix}{prompt}",
            "handoff": False,
        }

    def invoke(self, state: FormGraphState) -> FormGraphState:
        return self.graph.invoke(state)


form_graph = BusinessFormGraph()


def _prefill_order_answers(message: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    order_match = ORDER_NUMBER_PATTERN.search(message)
    if order_match:
        answers["order_number"] = order_match.group(0).upper()
    normalized_date = _normalize_date(message)
    if normalized_date:
        answers["departure_date"] = normalized_date
    for destination in sorted(_DESTINATION_TERMS, key=len, reverse=True):
        if destination.casefold() in message.casefold():
            answers["destination"] = destination
            break
    return answers


def _event(
    db: Session,
    session: AutomationFormSession,
    event_type: str,
    source_message_id: int | None,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    changed = [key for key in after if before.get(key) != after.get(key)]
    db.add(
        AutomationFormEvent(
            tenant_id=session.tenant_id,
            session_id=session.id,
            source_message_id=source_message_id,
            event_type=event_type,
            field_key=changed[0] if len(changed) == 1 else None,
            before_json={key: before.get(key) for key in changed},
            after_json={key: after.get(key) for key in changed},
        )
    )


def _active_session(db: Session, conversation: Conversation) -> AutomationFormSession | None:
    return db.scalar(
        select(AutomationFormSession)
        .where(
            AutomationFormSession.tenant_id == conversation.tenant_id,
            AutomationFormSession.conversation_id == conversation.id,
            AutomationFormSession.status.in_(ACTIVE_FORM_STATUSES),
        )
        .order_by(AutomationFormSession.updated_at.desc())
    )


def _lead_trigger(message: str, configuration: dict[str, Any]) -> bool:
    if not configuration.get("enabled"):
        return False
    normalized = message.casefold()
    return any(
        str(term).strip().casefold() in normalized
        for term in configuration.get("trigger_terms") or []
        if str(term).strip()
    )


def _lead_definition(configuration: dict[str, Any]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    for item in configuration.get("questions") or []:
        fields.append(
            {
                "key": str(item.get("id")),
                "kind": str(item.get("kind") or "text"),
                "required": bool(item.get("required", True)),
                "prompt": str(item.get("prompt") or ""),
                "prompt_en": str(item.get("prompt_en") or item.get("prompt") or ""),
                "default_score": int(item.get("default_score") or 0),
                "options": list(item.get("options") or []),
                "aliases": [str(item.get("id"))],
            }
        )
    return {"fields": fields, "grades": list(configuration.get("grades") or [])}


def _score_lead(definition: dict[str, Any], answers: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    score = 0
    for field in definition.get("fields") or []:
        score += int(field.get("default_score") or 0)
        selected = answers.get(str(field.get("key")))
        for option in field.get("options") or []:
            if str(option.get("value")) == str(selected):
                score += int(option.get("score") or 0)
                break
    grades = sorted(
        (dict(item) for item in definition.get("grades") or []),
        key=lambda item: int(item.get("min_score") or 0),
    )
    eligible = [item for item in grades if score >= int(item.get("min_score") or 0)]
    grade = eligible[-1] if eligible else (grades[0] if grades else {"name": "ungraded"})
    return score, grade


def _require_action_success(execution) -> str:
    if execution.status != ActionStatus.SUCCEEDED.value:
        raise RuntimeError(execution.failure_reason or execution.error_code or execution.status)
    return execution.id


def _apply_lead_actions(
    db: Session,
    session: AutomationFormSession,
    grade: dict[str, Any],
    source_message_id: int,
) -> list[str]:
    context = ActionContext.for_system(session.tenant_id, source_message_id=source_message_id)
    action_ids: list[str] = []
    fields = {
        "lead_score": session.score,
        "lead_grade": session.grade,
        "lead_qualified_at": utcnow().isoformat(),
    }
    action_ids.append(
        _require_action_success(
            propose_action(
                db,
                context,
                "contact.custom_fields.set",
                {"contact_id": session.contact_id, "fields": fields},
                idempotency_key=f"lead:{session.id}:fields",
            )
        )
    )
    if grade.get("tag"):
        action_ids.append(
            _require_action_success(
                propose_action(
                    db,
                    context,
                    "contact.tags.add",
                    {"contact_id": session.contact_id, "tags": [str(grade["tag"])]},
                    idempotency_key=f"lead:{session.id}:tag",
                )
            )
        )
    if grade.get("priority"):
        action_ids.append(
            _require_action_success(
                propose_action(
                    db,
                    context,
                    "conversation.update",
                    {
                        "conversation_id": session.conversation_id,
                        "priority": str(grade["priority"]),
                        "reason": "lead_qualification",
                    },
                    idempotency_key=f"lead:{session.id}:priority",
                )
            )
        )
    if grade.get("team_id") is not None or grade.get("user_id") is not None:
        arguments: dict[str, Any] = {"conversation_id": session.conversation_id}
        if grade.get("team_id") is not None:
            arguments["team_id"] = int(grade["team_id"])
        if grade.get("user_id") is not None:
            arguments["user_id"] = int(grade["user_id"])
        action_ids.append(
            _require_action_success(
                propose_action(
                    db,
                    context,
                    "conversation.assign",
                    arguments,
                    idempotency_key=f"lead:{session.id}:assignment",
                )
            )
        )
    return action_ids


def _complete_session(
    db: Session,
    session: AutomationFormSession,
    *,
    source_message_id: int,
    language: str,
) -> AutomationOutcome:
    if session.workflow_key == "lead_qualification":
        score, grade = _score_lead(session.definition_json, session.answers_json)
        session.score = score
        session.grade = str(grade.get("name") or "ungraded")[:80]
        db.commit()
        try:
            action_ids = _apply_lead_actions(db, session, grade, source_message_id)
        except Exception:
            return AutomationOutcome(
                reply="Lead details were saved, but routing needs a human agent." if language == "en" else "線索資料已保存，但自動分配未能完成，已轉交人工客服。",
                handoff=True,
                route="handoff",
                session_id=session.id,
                language=language,
                agent_profile_version_id=session.agent_profile_version_id,
            )
        return AutomationOutcome(
            reply="Thank you. Your details have been recorded." if language == "en" else "謝謝，您的資料已記錄。",
            handoff=False,
            route="knowledge",
            session_id=session.id,
            language=language,
            action_execution_ids=action_ids,
            agent_profile_version_id=session.agent_profile_version_id,
        )

    action_ids: list[str] = []
    if session.operation != "inquiry":
        execution = propose_action(
            db,
            ActionContext.for_system(
                session.tenant_id, source_message_id=source_message_id
            ),
            "order.sensitive.request",
            {
                "conversation_id": session.conversation_id,
                "operation": session.operation,
                "order_number": str(session.answers_json.get("order_number") or ""),
                "details": {
                    key: value
                    for key, value in session.answers_json.items()
                    if key != "order_number"
                },
            },
            idempotency_key=f"order-sensitive:{session.id}",
            auto_execute=False,
        )
        action_ids.append(execution.id)
        reply = (
            "The details are complete. A staff member must verify your identity and explicitly approve the request. No refund, cancellation, address change, or account-data change has been executed yet."
            if language == "en"
            else "資料已收集完成。客服必須先核驗身份並人工確認；退款、取消、地址或敏感資料修改目前尚未執行。"
        )
    else:
        reply = (
            "The order details are complete. I have transferred them to a human agent for the verified order lookup."
            if language == "en"
            else "訂單資料已收集完成，已轉交人工客服進行核實查詢。"
        )
    return AutomationOutcome(
        reply=reply,
        handoff=True,
        route="handoff",
        session_id=session.id,
        language=language,
        action_execution_ids=action_ids,
        agent_profile_version_id=session.agent_profile_version_id,
    )


def process_business_automation(
    db: Session,
    *,
    conversation: Conversation,
    contact: Contact,
    message: str,
    source_message_id: int,
) -> AutomationOutcome | None:
    profile = published_agent_configuration(db, conversation.tenant_id) or {}
    timeout_minutes = max(5, min(1440, int(profile.get("automation_timeout_minutes") or 30)))
    now = utcnow()
    session = _active_session(db, conversation)
    detected_language = _language(message)
    saved_language = (
        str((session.definition_json or {}).get("language") or "")
        if session is not None
        else ""
    )
    language = _substantive_language(message) or saved_language or detected_language
    if session is not None and language != saved_language:
        definition = dict(session.definition_json or {})
        definition["language"] = language
        session.definition_json = definition
    new_session = False

    session_expiry = session.expires_at if session is not None else None
    if session_expiry is not None and session_expiry.tzinfo is None:
        session_expiry = session_expiry.replace(tzinfo=timezone.utc)
    if session is not None and session_expiry is not None and session_expiry <= now:
        before = dict(session.answers_json or {})
        session.status = "timed_out"
        session.completed_at = now
        session.updated_at = now
        _event(db, session, "timed_out", source_message_id, before, before)
        db.commit()
        return AutomationOutcome(
            reply="The form timed out and has been transferred to a human agent." if language == "en" else "資料收集已超時，已為您轉交人工客服。",
            handoff=True,
            route="handoff",
            session_id=session.id,
            language=language,
            agent_profile_version_id=session.agent_profile_version_id,
        )

    if session is None:
        operation = _detect_order_operation(message) if profile.get("order_intake_enabled", True) else None
        lead_configuration = dict(profile.get("lead_qualification") or {})
        if operation is not None:
            definition = {
                "fields": _order_fields(operation),
                "grades": [],
                "language": language,
            }
            workflow_key = "order_intake"
            answers = _prefill_order_answers(message, definition["fields"])
        elif _lead_trigger(message, lead_configuration):
            definition = _lead_definition(lead_configuration)
            definition["language"] = language
            workflow_key = "lead_qualification"
            operation = "qualification"
            answers = {}
        else:
            return None
        session = AutomationFormSession(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            contact_id=contact.id,
            agent_profile_version_id=(
                int(profile["version_id"]) if profile.get("version_id") is not None else None
            ),
            workflow_key=workflow_key,
            operation=str(operation),
            status="active",
            current_step=_first_missing(definition["fields"], answers),
            definition_json=definition,
            answers_json=answers,
            expires_at=now + timedelta(minutes=timeout_minutes),
        )
        db.add(session)
        db.flush()
        new_session = True

    fields = list((session.definition_json or {}).get("fields") or [])
    before_answers = dict(session.answers_json or {})
    output = form_graph.invoke(
        {
            "message": message,
            "language": language,
            "fields": fields,
            "answers": before_answers,
            "current_step": session.current_step,
            "status": session.status,
            "new_session": new_session,
            "handoff": False,
        }
    )
    session.answers_json = dict(output.get("answers") or before_answers)
    session.current_step = int(output.get("current_step", session.current_step))
    session.status = str(output.get("status") or session.status)
    session.updated_at = now
    if session.status == "paused":
        session.paused_at = now
    elif session.status == "active":
        session.paused_at = None
        session.expires_at = now + timedelta(minutes=timeout_minutes)
    elif session.status in {"completed", "handed_off"}:
        session.completed_at = now
    _event(
        db,
        session,
        str(output.get("transition_event") or "updated"),
        source_message_id,
        before_answers,
        session.answers_json,
    )
    db.commit()
    db.refresh(session)

    if session.status == "completed":
        return _complete_session(
            db,
            session,
            source_message_id=source_message_id,
            language=language,
        )
    if session.status == "handed_off" or output.get("handoff"):
        return AutomationOutcome(
            reply=str(output.get("reply") or "已轉交人工客服。"),
            handoff=True,
            route="handoff",
            session_id=session.id,
            language=language,
            agent_profile_version_id=session.agent_profile_version_id,
        )
    awaiting = None
    if 0 <= session.current_step < len(fields):
        awaiting = str(fields[session.current_step].get("key") or "") or None
    return AutomationOutcome(
        reply=str(output.get("reply") or ""),
        handoff=False,
        route="order" if session.workflow_key == "order_intake" else "knowledge",
        session_id=session.id,
        language=language,
        awaiting_input=awaiting,
        agent_profile_version_id=session.agent_profile_version_id,
    )


def expire_automation_sessions(db: Session, *, now: datetime | None = None) -> int:
    current = now or utcnow()
    sessions = db.scalars(
        select(AutomationFormSession).where(
            AutomationFormSession.status.in_(ACTIVE_FORM_STATUSES),
            AutomationFormSession.expires_at <= current,
        )
    ).all()
    for session in sessions:
        before = dict(session.answers_json or {})
        session.status = "timed_out"
        session.completed_at = current
        session.updated_at = current
        _event(db, session, "timed_out", None, before, before)
    if sessions:
        db.commit()
        for session in sessions:
            propose_action(
                db,
                ActionContext.for_system(session.tenant_id),
                "conversation.handoff",
                {
                    "conversation_id": session.conversation_id,
                    "reason": "automation_form_timed_out",
                },
                idempotency_key=f"automation-timeout-handoff:{session.id}",
            )
    return len(sessions)


__all__ = [
    "AutomationOutcome",
    "BusinessFormGraph",
    "expire_automation_sessions",
    "form_graph",
    "process_business_automation",
]
