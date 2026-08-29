from __future__ import annotations

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.prebuilt import ToolNode

from backend.app.services.agent import (
    AI_REPLY_LABEL,
    AI_REPLY_FOOTER,
    AI_REPLY_FOOTER_CN,
    AI_REPLY_SEPARATOR,
    ORDER_WRITE_HANDOFF_REASON,
    VIP_PLAN_ANSWER_EN,
    VIP_PLAN_ANSWER_ZH,
    VIP_PLAN_SOURCE,
    _detect_language,
    _fallback_english_retrieval_query,
    _fallback_classify,
    _is_vip_query,
    _is_troubleshooting_query,
    _deterministic_knowledge_reply,
    _deterministic_evidence_supports_query,
    _normalize_answer_script,
    _reply_language,
    answer_has_language_mismatch,
    answer_implies_handoff,
    format_ai_customer_message,
    QueryRewrite,
    SupportAgentWorkflow,
    support_agent_workflow,
)


def test_detects_simplified_and_traditional_chinese() -> None:
    assert _detect_language("请问一家四口去日本，如何选择 WiFi 蛋？") == "zh-CN"
    assert _detect_language("請問一家四口去日本，如何選擇 WiFi 蛋？") == "zh-TW"


def test_detects_english_without_cjk_text() -> None:
    assert _detect_language("Which option is better for a family?") == "en"


def test_normalizes_answer_character_set() -> None:
    assert _normalize_answer_script("旅行時間與日數", "zh-CN") == "旅行时间与日数"
    assert _normalize_answer_script("旅行时间与日数", "zh-TW") == "旅行時間與日數"
    assert _normalize_answer_script("Travel time", "en") == "Travel time"


def test_customer_ai_message_uses_the_customer_visible_template() -> None:
    expected_simplified = (
        f"{AI_REPLY_LABEL}\n旅行时间\n"
        f"{AI_REPLY_SEPARATOR}\n{AI_REPLY_FOOTER_CN}"
    )
    expected_traditional = (
        f"{AI_REPLY_LABEL}\n旅行時間\n"
        f"{AI_REPLY_SEPARATOR}\n{AI_REPLY_FOOTER}"
    )
    assert format_ai_customer_message("旅行时间", language="zh-CN") == expected_simplified
    assert format_ai_customer_message("AI回答\n已標記", language="zh-TW") == (
        f"{AI_REPLY_LABEL}\n已標記\n{AI_REPLY_SEPARATOR}\n{AI_REPLY_FOOTER}"
    )
    assert (
        format_ai_customer_message(expected_traditional, language="zh-TW")
        == expected_traditional
    )
    assert format_ai_customer_message("Travel time", language="en") == "AI response\nTravel time"


def test_reply_language_tracks_english_and_respects_language_switches() -> None:
    assert _reply_language("How much is Japan WiFi?") == "en"
    assert _reply_language("5", preferred_language="en") == "en"
    assert _reply_language("日本 WiFi 多少錢？", preferred_language="en") == "zh-TW"
    assert _reply_language("日本 WiFi 多少钱？") == "zh-CN"


def test_reply_language_guard_allows_technical_tokens_but_rejects_mixed_prose() -> None:
    assert answer_has_language_mismatch("日本 WiFi 蛋支援 eSIM 與 FUP 說明。", "zh-TW") is False
    assert answer_has_language_mismatch("日本 WiFi is available and ready for pickup.", "zh-TW") is True
    assert answer_has_language_mismatch("Japan WiFi supports eSIM and FUP.", "en") is False
    assert answer_has_language_mismatch("Japan WiFi 可於機場領取。", "en") is True


def test_guard_turns_handoff_wording_into_real_handoff_state() -> None:
    guarded = support_agent_workflow._guard(
        {
            "reply_parts": ["If it is not listed, a human agent will continue to assist you."],
            "language": "en",
            "handoff": False,
        }
    )
    assert answer_implies_handoff(guarded["answer"]) is True
    assert guarded["handoff"] is True
    assert answer_implies_handoff("A human agent will confirm the counter for you.") is True


@pytest.mark.parametrize(
    "answer",
    (
        "目前資料未提及租借日本 WiFi 蛋是否有贈禮，會由專人為你跟進確認。",
        "資料不足，會由客服專員跟進確認。",
    ),
)
def test_guard_detects_chinese_human_follow_up_promises(answer: str) -> None:
    guarded = support_agent_workflow._guard(
        {"reply_parts": [answer], "language": "zh-TW", "handoff": False}
    )
    assert guarded["handoff"] is True
    assert answer_implies_handoff(guarded["answer"]) is True


def test_guard_keeps_verified_catalog_context_ai_owned() -> None:
    guarded = support_agent_workflow._guard(
        {
            "reply_parts": ["資料不足，會由客服專員跟進確認。"],
            "language": "zh-TW",
            "handoff": False,
            "context": [
                {
                    "source_type": "structured_product",
                    "title": "泰國 eSIM",
                    "content": "產品：泰國 eSIM\n價格方案：HK$35",
                }
            ],
        }
    )
    assert guarded["handoff"] is False
    assert "泰國 eSIM" in guarded["answer"]


def test_knowledge_navigation_text_does_not_imply_a_real_handoff() -> None:
    assert answer_implies_handoff("請確認客服回覆時間及聯絡方法。") is False


def test_troubleshooting_query_is_kept_out_of_product_catalog_routing() -> None:
    assert _is_troubleshooting_query("The battery drains very quickly.") is True
    assert _is_troubleshooting_query("手机显示 SIM 卡无效") is True
    assert _is_troubleshooting_query("我想买 Type-C 数据线") is False


def test_verified_airport_evidence_has_an_english_deterministic_reply() -> None:
    parts = _deterministic_knowledge_reply(
        "Where do I pick up the device at Hong Kong airport?",
        [
            {
                "title": "Hong Kong airport pickup guide",
                "content": (
                    "香港機場可安排 WiFi 蛋取機。出發前應確認櫃檯位置、"
                    "營業時間、預訂編號或身份證明，以及還機方式。"
                ),
                "source_type": "knowledge",
            }
        ],
        "en",
    )
    assert len(parts) == 1
    assert "Hong Kong airport" in parts[0]
    assert "human agent" not in parts[0].casefold()


def test_guard_fails_closed_when_mixed_language_cannot_be_rewritten() -> None:
    guarded = support_agent_workflow._guard(
        {
            "reply_parts": ["Japan WiFi 可以在机场领取。"],
            "language": "en",
            "handoff": False,
        }
    )
    assert guarded["handoff"] is True
    assert guarded["sources"] == []
    assert answer_has_language_mismatch(guarded["answer"], "en") is False


def test_evidence_validator_rejects_shared_product_words_without_feature_support() -> None:
    document = Document(
        page_content="WiFi 蛋租借、機場取還及多人共享說明。",
        metadata={
            "title": "WiFi 蛋常見問題",
            "source": "https://help.example.com/wifi-faq",
        },
    )
    assert _deterministic_evidence_supports_query(
        "WiFi 蛋支持量子衛星加密協議嗎？",
        document,
    ) is False
    returns = Document(
        page_content="客戶簽收商品後七天內可以申請退貨。",
        metadata={"title": "退換貨政策", "source": "https://help.example.com/returns"},
    )
    assert _deterministic_evidence_supports_query("商品退貨期限是多久？", returns) is True


def test_language_guard_allows_catalog_carrier_and_network_tokens() -> None:
    answer = "產品說明：曼谷、布吉、清邁全境覆蓋 AIS / dtac 4G/5G 網絡。"
    assert answer_has_language_mismatch(answer, "zh-TW") is False


def test_retrieve_uses_one_bounded_langgraph_query_rewrite_retry(monkeypatch) -> None:
    class FakeRewriteModel:
        def with_structured_output(self, schema):
            assert schema is QueryRewrite
            return RunnableLambda(lambda _: QueryRewrite(query="泰國 eSIM 產品價格"))

    monkeypatch.setattr(support_agent_workflow, "model", FakeRewriteModel())
    assert support_agent_workflow._route_after_retrieve(
        {"context": [], "query_rewrite_count": 0}
    ) == "rewrite_query"
    assert support_agent_workflow._route_after_retrieve(
        {"context": [], "query_rewrite_count": 1}
    ) == "generate"
    assert support_agent_workflow._route_after_retrieve(
        {"context": [{"source_type": "knowledge"}], "query_rewrite_count": 0}
    ) == "generate"

    rewritten = support_agent_workflow._rewrite_query(
        {
            "message": "這個多少錢",
            "effective_message": "這個多少錢",
            "history": ["customer: 泰國 eSIM"],
            "query_rewrite_count": 0,
            "product_intent": False,
        }
    )
    assert rewritten["effective_message"] == "泰國 eSIM 產品價格"
    assert rewritten["rewritten_query"] == "泰國 eSIM 產品價格"
    assert rewritten["query_rewrite_count"] == 1


def test_retrieve_does_not_retry_after_rewrite_when_model_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(support_agent_workflow, "model", None)
    assert support_agent_workflow._route_after_retrieve(
        {"context": [], "query_rewrite_count": 0}
    ) == "generate"


def test_graph_uses_prebuilt_tool_node_and_hides_injected_runtime() -> None:
    graph = support_agent_workflow.graph.get_graph()
    assert set(graph.nodes) == {"__start__", "agent", "tools", "guard", "__end__"}
    assert isinstance(graph.nodes["tools"].data, ToolNode)
    schemas = {
        item.name: set(item.tool_call_schema.model_json_schema()["properties"])
        for item in support_agent_workflow.tools
    }
    assert schemas == {
        "search_support_knowledge": {"query"},
        "query_product_prices": {"query"},
        "lookup_customer_order": {"order_reference"},
        "get_vip_plan": set(),
        "transfer_to_human": {"reason"},
    }


def test_model_tool_call_is_executed_by_tool_node() -> None:
    class FakeToolCallingModel:
        def bind_tools(self, tools, **kwargs):
            assert "lookup_customer_order" in {item.name for item in tools}
            assert kwargs["parallel_tool_calls"] is False
            return self

        @staticmethod
        def invoke(_messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "lookup_customer_order",
                        "args": {"order_reference": "ORD-1001"},
                        "id": "test-order-tool-call",
                    }
                ],
            )

    from backend.app.database import SessionLocal

    workflow = SupportAgentWorkflow()
    workflow.model = FakeToolCallingModel()
    with SessionLocal() as db:
        result = workflow.run(
            db,
            tenant_id=1,
            conversation_id=981900,
            customer_name="ToolNode test",
            customer_phone="+85260000000",
            message="tracking ORD-1001",
            history=[],
        )

    assert result.route == "order"
    assert result.handoff is False
    assert "ORD-1001" in result.answer
    assert "shipped" in result.answer


def test_english_knowledge_query_gets_deterministic_chinese_search_terms() -> None:
    assert _fallback_english_retrieval_query("How do I install an eSIM?") == "eSIM 安装 设置"
    airport = _fallback_english_retrieval_query(
        "Where can I pick up the WiFi device at the airport?"
    )
    assert "WiFi 上网设备" in airport
    assert "取机 领取" in airport
    assert "机场 柜台" in airport
    assert "WiFi" in airport
    assert "营业时间 服务时间" in _fallback_english_retrieval_query(
        "What are your business hours?"
    )
    comparison = _fallback_english_retrieval_query(
        "For a multi-country Europe trip, should I use one WiFi device or several SIM cards?"
    )
    assert "欧洲" in comparison
    assert "多国旅行" in comparison
    assert "选择 比较" in comparison
    assert "SIM 卡 电话卡" in comparison


@pytest.mark.parametrize(
    "message, intent",
    (
        ("Hi", "greeting"),
        ("How much is Japan 5G WiFi?", "pricing"),
        ("How much?", "pricing"),
        ("How do I install an eSIM?", "knowledge"),
        ("Where can I pick up the WiFi device at the airport?", "knowledge"),
    ),
)
def test_english_fallback_routes_common_support_questions(message: str, intent: str) -> None:
    decision = _fallback_classify(message)
    assert decision.intent == intent
    assert decision.language == "en"


@pytest.mark.parametrize(
    "message",
    (
        "取消订单 ORD-1001",
        "請幫我取消訂單 ORD-1001",
        "修改订单地址 ORD-1001",
        "我要更改收貨地址",
        "订单里删除一件商品",
        "cancel order ORD-1001",
        "change the delivery address for order ORD-1001",
    ),
)
def test_order_write_actions_are_deterministic_handoffs(message: str) -> None:
    decision = _fallback_classify(message)
    assert decision.intent == "handoff"
    assert decision.reason == ORDER_WRITE_HANDOFF_REASON


@pytest.mark.parametrize(
    "message",
    (
        "查询订单 ORD-1001",
        "物流怎么查",
        "tracking ORD-9999",
    ),
)
def test_read_only_order_queries_still_use_order_tool(message: str) -> None:
    assert _fallback_classify(message).intent == "order"


@pytest.mark.parametrize(
    "message",
    (
        "怎么样才能成为VIP",
        "如何升级会员",
        "VIP优惠怎么用",
        "租三次有什么奖励",
        "常客有优惠吗",
        "我租了三次能升级吗",
        "How can I become a VIP?",
        "What are the VIP benefits?",
        "Do repeat renters get a discount?",
        "Are there perks for frequent customers?",
        "What are the loyalty rewards?",
        "I rented three times; am I eligible?",
    ),
)
def test_vip_aliases_are_recognized_as_verified_knowledge(message: str) -> None:
    decision = _fallback_classify(message)
    assert decision.intent == "knowledge"
    assert _is_vip_query(message) is True


def test_viper_is_not_a_vip_alias() -> None:
    assert _is_vip_query("viper camera") is False


def test_vip_question_uses_fixed_official_answer_without_handoff() -> None:
    from backend.app.database import SessionLocal

    with SessionLocal() as db:
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=981001,
            customer_name="VIP test",
            customer_phone="",
            message="怎么样才能成为VIP",
            history=[],
        )

    assert result.route == "knowledge"
    assert result.handoff is False
    assert result.answer == _normalize_answer_script(VIP_PLAN_ANSWER_ZH, "zh-CN")
    assert "累计租满3次" in result.answer
    assert "额外9折" in result.answer
    assert "免费外游小礼物" in result.answer
    assert "送完即止" in result.answer
    assert "WhatsApp" in result.answer
    assert result.sources[0]["source"] == VIP_PLAN_SOURCE
    assert result.sources[0]["deterministic"] is True


@pytest.mark.parametrize(
    "message",
    (
        "租三次有什么奖励",
        "常客有优惠吗",
        "我租了三次能升级吗",
        "Do repeat renters get a discount?",
        "Are there perks for frequent customers?",
        "What are the loyalty rewards?",
        "I rented three times; am I eligible?",
    ),
)
def test_implicit_vip_paraphrases_use_the_same_fixed_answer(message: str) -> None:
    from backend.app.database import SessionLocal

    with SessionLocal() as db:
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=981100 + abs(hash(message)) % 1000,
            customer_name="VIP paraphrase test",
            customer_phone="",
            message=message,
            history=[],
        )

    language = _detect_language(message)
    expected = VIP_PLAN_ANSWER_EN if language == "en" else _normalize_answer_script(
        VIP_PLAN_ANSWER_ZH,
        language,
    )
    assert result.route == "knowledge"
    assert result.handoff is False
    assert result.answer == expected
    assert result.sources[0]["source"] == VIP_PLAN_SOURCE


def test_english_vip_question_gets_english_fixed_answer() -> None:
    from backend.app.database import SessionLocal

    with SessionLocal() as db:
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=981002,
            customer_name="VIP test",
            customer_phone="",
            message="How can I become a VIP?",
            history=[],
        )

    assert result.route == "knowledge"
    assert result.handoff is False
    assert result.language == "en"
    assert result.answer == VIP_PLAN_ANSWER_EN
    assert "at least 3 times" in result.answer
    assert "free travel gift" in result.answer
    assert "while supplies last" in result.answer
    assert "human agent" not in result.answer.lower()
    assert result.sources[0]["title"] == "SongWiFi VIP Plan"


def test_explicit_handoff_still_wins_over_vip_policy() -> None:
    from backend.app.database import SessionLocal

    with SessionLocal() as db:
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=981003,
            customer_name="VIP test",
            customer_phone="",
            message="VIP问题请转人工客服",
            history=[],
        )

    assert result.route == "handoff"
    assert result.handoff is True
    assert result.sources == []
