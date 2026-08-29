from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from sqlalchemy import func, select

from backend.app.channels import SendResult
from backend.app.database import SessionLocal
from backend.app.models import (
    Contact,
    Conversation,
    KnowledgeDocument,
    Message,
    Product,
    ProductPriceHistory,
    ProductPriceOffer,
    ProductPriceSource,
    ProductPriceSyncRun,
    utcnow,
)
from backend.app.services.agent import (
    AgentResult,
    IntentDecision,
    format_ai_customer_message,
    support_agent_workflow,
)
from backend.app.services.conversations import _store_agent_result, receive_inbound
from backend.app.services.knowledge_ingestion import rebuild_document_chunks
from backend.app.services.product_price_ingestion import (
    ScrapedOffer,
    ScrapedProduct,
    _generic_page_products,
    _songwifi_shop_catalog,
    persist_product_catalog,
)
from backend.app.services.product_price_query import (
    RentalPeriod,
    is_product_recommendation_query,
    is_product_catalog_query,
    parse_rental_period,
    query_product_catalog_documents,
    query_product_price_catalog,
    resolve_rental_period,
)
from backend.app.services.knowledge import should_prioritize_product_catalog


def install_demo_send_spy(monkeypatch, sent: list[str], prefix: str) -> None:
    def fake_send(self, outbound):
        assert outbound.to
        sent.append(outbound.text)
        return SendResult(
            provider="demo",
            external_message_id=f"{prefix}-{len(sent)}",
            status="sent",
        )

    monkeypatch.setattr("backend.app.channels.demo.DemoChannelProvider.send", fake_send)


def sample_product(
    key: str,
    name: str,
    price: str,
    *,
    category: str = "wifi_5g",
    destination: str | None = "日本",
    network: str | None = "5G",
    product_type: str = "wifi_rental",
    offers: int = 1,
) -> ScrapedProduct:
    return ScrapedProduct(
        external_key=key,
        canonical_url=f"https://prices.example.com/{key}",
        name=name,
        name_translations={"zh-CN": name, "zh-TW": name},
        aliases=[name, destination],
        category=category,
        product_type=product_type,
        destination=destination,
        network=network,
        description="测试商品",
        metadata={},
        offers=[
            ScrapedOffer(
                external_key=f"offer-{index}",
                label=f"规格 {index + 1}",
                currency="HKD",
                price_amount=Decimal(price) + index,
                original_amount=Decimal(price) + index + 10,
                unit="day" if product_type == "wifi_rental" else "plan",
                duration_days=None if product_type == "wifi_rental" else 5,
                data_label=None if product_type == "wifi_rental" else f"{index + 1}GB",
            )
            for index in range(offers)
        ],
    )


def create_source() -> int:
    with SessionLocal() as db:
        source = ProductPriceSource(
            tenant_id=1,
            created_by_user_id=1,
            name="测试价格站",
            root_url="https://prices.example.com/",
            domain="prices.example.com",
            adapter="schema_org",
            status="completed",
        )
        db.add(source)
        db.commit()
        db.refresh(source)
        return source.id


def test_schema_org_product_parser_extracts_offer():
    markup = """
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Product",
      "name": "Japan eSIM",
      "sku": "JP-5-7",
      "category": "eSIM",
      "offers": {
        "@type": "Offer",
        "name": "5GB / 7 days",
        "price": "68",
        "priceCurrency": "HKD",
        "availability": "https://schema.org/InStock"
      }
    }
    </script></head></html>
    """
    products = _generic_page_products(markup, "https://shop.example.com/esim/jp")
    assert len(products) == 1
    assert products[0].name == "Japan eSIM"
    assert products[0].offers[0].price_amount == Decimal("68.00")
    assert products[0].offers[0].currency == "HKD"


def test_rental_period_parser_supports_days_and_inclusive_date_ranges():
    assert parse_rental_period("日本 WiFi 租三天多少钱") == RentalPeriod(
        days=3,
        source="duration",
    )
    assert parse_rental_period(
        "日本 WiFi 从8月1号开始到8月5号结束多少钱",
        today=date(2026, 8, 27),
    ) == RentalPeriod(
        days=5,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 5),
        source="date_range",
    )
    assert parse_rental_period(
        "How much for Aug 1-5 Japan WiFi?",
        today=date(2026, 8, 27),
    ) == RentalPeriod(
        days=5,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 5),
        source="date_range",
    )
    # A package validity such as eSIM 5日 is a duration, not a calendar date.
    assert parse_rental_period("日本 eSIM 5日多少钱") == RentalPeriod(
        days=5,
        source="duration",
    )


def test_rental_period_followup_adds_days_without_overriding_the_previous_quote():
    previous = RentalPeriod(days=7)
    assert resolve_rental_period("我多加两天", previous=previous) == RentalPeriod(
        days=9,
        source="duration_adjusted",
    )
    assert resolve_rental_period("日本七天，多加两天", previous=None) == RentalPeriod(
        days=9,
        source="duration_adjusted",
    )
    # A normal explicit duration is a replacement, not an addition.
    assert resolve_rental_period("改成五天", previous=previous) == RentalPeriod(
        days=5,
        source="duration",
    )
    dated = resolve_rental_period(
        "再加两天",
        previous=RentalPeriod(
            days=5,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 5),
            source="date_range",
        ),
    )
    assert dated == RentalPeriod(
        days=7,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 7),
        source="date_range_adjusted",
    )
    assert resolve_rental_period(
        "日本从2026年8月1号到2026年8月5号，多加两天",
        today=date(2026, 8, 27),
    ) == dated
    assert resolve_rental_period(
        "add two more days",
        previous=previous,
    ) == RentalPeriod(days=9, source="duration_adjusted")
    assert resolve_rental_period("延长至十天", previous=previous) == RentalPeriod(
        days=10,
        source="duration",
    )


def test_daily_rental_quote_includes_deterministic_total_and_disclaimer(
    authenticated_client: TestClient,
):
    del authenticated_client
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(
            db,
            source,
            [sample_product("jp-wifi-quote", "5G 日本", "48")],
        )
        db.commit()

    with SessionLocal() as db:
        result = query_product_price_catalog(
            db,
            1,
            "日本 WiFi 从2026年8月1号到2026年8月5号多少钱",
            language="zh-CN",
        )

    text = "\n".join(result["segments"])
    assert result["rental_period"] == {
        "days": 5,
        "start_date": "2026-08-01",
        "end_date": "2026-08-05",
        "source": "date_range",
    }
    assert "HK$48/日 × 5日 = HK$240" in text
    assert "（实际以网址结算为准）" in text
    assert result["estimates"][0]["amount"] == "240.00"


def test_esim_quote_selects_shortest_plan_covering_requested_days(
    authenticated_client: TestClient,
):
    del authenticated_client
    source_id = create_source()
    product = sample_product(
        "th-esim-quote",
        "泰國 eSIM",
        "35",
        category="esim",
        destination="泰國",
        network="4G/5G",
        product_type="esim",
        offers=3,
    )
    product.offers[0].label = "泰國 3GB 5日"
    product.offers[0].duration_days = 5
    product.offers[0].data_label = "3GB"
    product.offers[1].label = "泰國 5GB 8日"
    product.offers[1].duration_days = 8
    product.offers[1].data_label = "5GB"
    product.offers[1].price_amount = Decimal("48")
    product.offers[2].label = "泰國 無限數據 10日"
    product.offers[2].duration_days = 10
    product.offers[2].data_label = "無限"
    product.offers[2].price_amount = Decimal("148")

    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [product])
        db.commit()
        result = query_product_price_catalog(
            db,
            1,
            "泰國 eSIM 七天多少錢",
            language="zh-TW",
        )

    text = "\n".join(result["segments"])
    assert result["rental_period"] == {
        "days": 7,
        "start_date": None,
        "end_date": None,
        "source": "duration",
    }
    assert "可覆蓋方案費用（7日）：HK$48（8日套餐，覆蓋7日需求）" in text
    assert "可覆蓋方案費用（7日）：HK$35" not in text
    assert "可覆蓋方案費用（7日）：HK$148" not in text
    assert result["estimates"] == [
        {
            "product_id": result["estimates"][0]["product_id"],
            "offer_id": result["estimates"][0]["offer_id"],
            "currency": "HKD",
            "amount": "48.00",
            "days": 7,
            "kind": "package",
        }
    ]


def test_songwifi_eshop_parser_keeps_variants_sale_price_and_stock():
    payload = {
        "data": [
            {
                "product_id": 145,
                "name": "全球通用轉換插頭",
                "description": "<p>旅行充電配件</p>",
                "brand_name": "Song WiFi",
                "category_name": "旅行用品",
                "price": "230",
                "onsale": "119",
                "variants": [
                    {
                        "variant_id": 501,
                        "option_name": "黑色",
                        "sku": "ADAPTER-BLK",
                        "price": "230",
                        "onsale": "119",
                        "total_available": 8,
                        "unlimited_inventory": "0",
                    },
                    {
                        "variant_id": 502,
                        "option_name": "白色",
                        "sku": "ADAPTER-WHT",
                        "price": "230",
                        "onsale": "119",
                        "total_available": 0,
                        "unlimited_inventory": "0",
                    },
                ],
            }
        ]
    }
    products = _songwifi_shop_catalog(payload, "https://songwifi.com.hk/")
    assert len(products) == 1
    product = products[0]
    assert product.external_key == "eshop-product:145"
    assert product.category == "eshop"
    assert product.product_type == "eshop_product"
    assert product.name_translations["zh-CN"] == "全球通用转换插头"
    assert product.canonical_url == "https://songwifi.com.hk/eshop/product/145"
    assert len(product.offers) == 2
    assert product.offers[0].price_amount == Decimal("119.00")
    assert product.offers[0].original_amount == Decimal("230.00")
    assert product.offers[0].availability == "in_stock"
    assert product.offers[1].availability == "out_of_stock"


def test_catalog_query_matches_short_eshop_product_phrase(authenticated_client: TestClient):
    del authenticated_client
    source_id = create_source()
    product = sample_product(
        "fan",
        "REMAX 便攜式桌面風扇",
        "49",
        category="eshop",
        destination=None,
        network=None,
        product_type="eshop_product",
    )
    product.name_translations = {"zh-CN": "REMAX 便携式桌面风扇", "zh-TW": product.name}
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [product])
        db.commit()
    with SessionLocal() as db:
        result = query_product_price_catalog(db, 1, "风扇价格")
    assert result["found"] is True
    assert result["count"] == 1
    assert "便携式桌面风扇" in result["segments"][0]


def test_non_price_product_intent_uses_catalog_before_relevant_knowledge(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    source_id = create_source()
    gopro = sample_product(
        "gopro-13",
        "GoPro HERO 13",
        "40",
        category="travel_gadget",
        destination=None,
        network=None,
        product_type="travel_gadget",
    )
    gopro.offers[0].unit = "day"
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [gopro])
        db.commit()

    monkeypatch.setattr(
        "backend.app.services.agent.retrieve_knowledge",
        lambda *_args, **_kwargs: [
            Document(
                page_content="GoPro 租用前請先檢查電池及配件。",
                metadata={
                    "document_id": 99,
                    "title": "GoPro 使用提示",
                    "source": "https://help.example.com/gopro",
                },
            )
        ],
    )
    with SessionLocal() as db:
        assert is_product_catalog_query(db, 1, "我想租GoPro") is True
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=98001,
            customer_name="产品测试客户",
            customer_phone="+85260000001",
            message="我想租GoPro",
            history=[],
        )
    assert result.route == "knowledge"
    assert result.handoff is False
    assert "GoPro HERO 13" in result.answer
    assert "HK$40/日" in result.answer
    assert [source["source_type"] for source in result.sources] == [
        "structured_product",
        "knowledge",
    ]


def test_thailand_product_list_is_not_escalated_by_network_tokens(
    authenticated_client: TestClient,
    monkeypatch,
):
    """Carrier/4G/5G text in a valid catalogue answer must stay AI-owned."""

    del authenticated_client
    source_id = create_source()
    products = [
        sample_product(
            "th-esim",
            "泰國 eSIM",
            "35",
            category="esim",
            destination="泰國",
            network="4G/5G",
            product_type="esim",
        ),
        sample_product(
            "th-5g",
            "5G 泰國",
            "48",
            destination="泰國",
            network="5G",
        ),
    ]
    products[0].description = "曼谷、布吉、清邁全境覆蓋 AIS / dtac 4G/5G 網絡。"
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, products)
        db.commit()
        monkeypatch.setattr(support_agent_workflow, "model", None)
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=980015,
            customer_name="泰國產品測試",
            customer_phone="+85260000015",
            message="泰国有哪些产品",
            history=[],
        )

    assert result.route == "knowledge"
    assert result.handoff is False
    assert "泰国 eSIM" in result.answer
    assert "AIS" in result.answer
    assert any(source["source_type"] == "structured_product" for source in result.sources)


def test_model_handoff_guess_is_overridden_for_verified_catalog_query(
    authenticated_client: TestClient,
    monkeypatch,
):
    """An uncertain model route must retrieve an in-scope catalogue answer first."""

    del authenticated_client
    source_id = create_source()
    product = sample_product(
        "th-5g-handoff-override",
        "5G 泰國",
        "48",
        destination="泰國",
        network="5G",
    )
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [product])
        db.commit()

        class HandoffGuessModel:
            def with_structured_output(self, schema):
                assert schema is IntentDecision
                return RunnableLambda(
                    lambda _input: IntentDecision(
                        intent="handoff",
                        language="zh-CN",
                        reason="uncertain",
                        standalone_query="泰国有哪些产品",
                    )
                )

        monkeypatch.setattr(support_agent_workflow, "model", HandoffGuessModel())
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=980016,
            customer_name="路由覆寫測試",
            customer_phone="+85260000016",
            message="WiFi怎么设置",
            history=[],
        )

    assert result.route == "knowledge"
    assert result.handoff is False
    assert "5G 泰国" in result.answer


def test_informational_comparison_prefers_guide_over_structured_product(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    source_id = create_source()
    korea_wifi = sample_product(
        "korea-5g",
        "5G 韓國",
        "35",
        category="wifi_5g",
        destination="韓國",
        network="5G",
        product_type="wifi_rental",
    )
    korea_wifi.offers[0].unit = "day"
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [korea_wifi])
        db.commit()

    monkeypatch.setattr(
        "backend.app.services.agent.retrieve_knowledge",
        lambda *_args, **_kwargs: [
            Document(
                page_content=(
                    "韓國多人同行可選 WiFi 蛋，單人只用一部手機可考慮 SIM 卡。"
                    "應按同行人數、裝置數量和取還安排選擇。"
                ),
                metadata={
                    "document_id": 100,
                    "title": "韓國 WiFi 蛋與 SIM 卡比較",
                    "source": "https://help.example.com/korea-wifi-vs-sim",
                },
            )
        ],
    )
    query = "韓國旅行用 WiFi 蛋還是 SIM 卡？"
    with SessionLocal() as db:
        assert is_product_catalog_query(db, 1, query) is True
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=98002,
            customer_name="Comparison test",
            customer_phone="+85260000002",
            message=query,
            history=[],
        )
    assert result.route == "knowledge"
    assert result.handoff is False
    assert [source["source_type"] for source in result.sources] == ["knowledge"]
    assert result.sources[0]["title"] == "韓國 WiFi 蛋與 SIM 卡比較"


def test_non_price_out_of_stock_product_is_returned_as_existing(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    source_id = create_source()
    smart_cup = sample_product(
        "smart-cup",
        "REMAX智能保溫杯 RT-IG02 黑色",
        "198",
        category="eshop",
        destination=None,
        network=None,
        product_type="eshop_product",
    )
    smart_cup.offers[0].availability = "out_of_stock"
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [smart_cup])
        db.commit()

        documents = query_product_catalog_documents(db, 1, "保溫杯有嗎")
        assert len(documents) == 1
        assert documents[0].metadata["availability"] == "產品存在，目前缺貨"
        assert documents[0].metadata["availability_code"] == "out_of_stock"
        assert "供應狀態：產品存在，目前缺貨" in documents[0].page_content

        # The deterministic stock template must run before any generation model.
        monkeypatch.setattr(support_agent_workflow, "model", object())
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=98002,
            customer_name="缺貨測試客戶",
            customer_phone="+85260000002",
            message="保溫杯有嗎",
            history=[],
        )
        model_alias_result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=98003,
            customer_name="缺貨型號測試客戶",
            customer_phone="+85260000003",
            message="REMAX RT-IG02 黑色可不可以買？",
            history=[],
        )

    for item in (result, model_alias_result):
        assert item.route == "knowledge"
        assert item.handoff is False
        assert item.answer == (
            "「REMAX智能保溫杯 RT-IG02 黑色」產品存在，目前缺貨。\n"
            "商品連結：https://prices.example.com/smart-cup"
        )
        assert "可以買" not in item.answer
        assert "HK$" not in item.answer
        assert item.sources[0]["source_type"] == "structured_product"


def test_structured_catalog_keeps_each_offer_link_with_its_variant(
    authenticated_client: TestClient,
):
    """A generated answer must be able to pair an eSIM plan with its URL."""

    del authenticated_client
    source_id = create_source()
    product = sample_product(
        "jp-esim-links",
        "日本 eSIM",
        "48",
        category="esim",
        product_type="esim",
        network="4G/5G",
        offers=2,
    )
    product.offers[0].metadata = {
        "order_url": "https://checkout.example.com/esim?plan=jp-3-5"
    }
    product.offers[0].label = "日本 3GB 5日"
    product.offers[0].duration_days = 5
    product.offers[0].data_label = "3GB"
    product.offers[1].label = "日本 無限數據 10日"
    product.offers[1].duration_days = 10
    product.offers[1].data_label = "無限"
    product.offers[1].metadata = {
        "order_url": "https://checkout.example.com/esim?plan=jp-unl-10"
    }

    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [product])
        db.commit()
        document = query_product_catalog_documents(
            db,
            1,
            "日本 eSIM",
            language="zh-TW",
        )[0]

    content = document.page_content
    first_url = "https://checkout.example.com/esim?plan=jp-3-5"
    second_url = "https://checkout.example.com/esim?plan=jp-unl-10"
    assert first_url in content
    assert second_url in content
    assert content.index("3GB") < content.index(first_url)
    assert content.index("無限") < content.index(second_url)


def test_out_of_scope_questions_trigger_real_handoff_before_model(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    monkeypatch.setattr(support_agent_workflow, "model", object())
    questions = (
        "今天香港天氣怎麼樣？",
        "明天香港會不會下雨？",
        "幫我查一下今天美元匯率",
        "推薦一家尖沙咀餐廳",
        "比特幣價格是多少？",
    )
    with SessionLocal() as db:
        results = [
            support_agent_workflow.run(
                db,
                tenant_id=1,
                conversation_id=98100 + index,
                customer_name="範圍外測試客戶",
                customer_phone=f"+85260001{index:03d}",
                message=question,
                history=[],
            )
            for index, question in enumerate(questions, start=1)
        ]

    assert all(result.route == "handoff" for result in results)
    assert all(result.handoff is True for result in results)
    assert all(result.answer == "這邊給你轉接人工客服，請稍後" for result in results)


def test_camera_category_aliases_return_structured_rental_products(
    authenticated_client: TestClient,
):
    del authenticated_client
    source_id = create_source()
    products = [
        sample_product(
            "gopro-13",
            "GoPro HERO 13",
            "40",
            category="travel_gadget",
            destination=None,
            network=None,
            product_type="travel_gadget",
        ),
        sample_product(
            "insta-x3",
            "INSTA 360 X3",
            "40",
            category="travel_gadget",
            destination=None,
            network=None,
            product_type="travel_gadget",
        ),
        sample_product(
            "osmo-pocket-3",
            "Osmo Pocket 3",
            "80",
            category="travel_gadget",
            destination=None,
            network=None,
            product_type="travel_gadget",
        ),
    ]
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, products)
        db.commit()

    with SessionLocal() as db:
        documents = query_product_catalog_documents(db, 1, "我想租相机、摄影机")
        titles = {str(document.metadata["title"]) for document in documents}
        assert titles == {"GoPro HERO 13", "INSTA 360 X3", "Osmo Pocket 3"}

        camcorder = query_product_price_catalog(db, 1, "摄影机价格")
        assert camcorder["found"] is True
        assert "Osmo Pocket 3" in camcorder["segments"][0]
        assert "GoPro HERO 13" not in camcorder["segments"][0]
        assert "INSTA 360 X3" not in camcorder["segments"][0]


def test_common_product_alias_regression_and_out_of_stock_exact_match(
    authenticated_client: TestClient,
):
    del authenticated_client
    source_id = create_source()
    specs = [
        ("gopro", "GoPro HERO 13", "40", "travel_gadget", "travel_gadget"),
        ("insta", "INSTA 360 X3", "40", "travel_gadget", "travel_gadget"),
        ("osmo", "Osmo Pocket 3", "80", "travel_gadget", "travel_gadget"),
        ("wash", "便攜分裝洗護洗漱杯套裝", "99", "eshop", "eshop_product"),
        ("lightning", "REMAX 速捷數據線 RC-134i (Lightning)", "20", "eshop", "eshop_product"),
        ("thermos", "REMAX未來系列智能保溫杯RT-IG02黑色", "138", "eshop", "eshop_product"),
        ("child-camera", "兒童攝影相機", "198", "eshop", "eshop_product"),
        ("type-c", "REMAX 速捷數據線 RC-134a (Type-C)", "20", "eshop", "eshop_product"),
        ("dual-fan", "REMAXLIFE 便攜式折疊雙頭小風扇", "43", "eshop", "eshop_product"),
        ("mini-fan", "REMAXLIFE 便攜迷你折疊小風扇", "34", "eshop", "eshop_product"),
        ("other-remax", "REMAX 極限一拖三充電線 RC-131th", "35", "eshop", "eshop_product"),
    ]
    products = [
        sample_product(
            key,
            name,
            price,
            category=category,
            destination=None,
            network=None,
            product_type=product_type,
        )
        for key, name, price, category, product_type in specs
    ]
    for product in products:
        if product.external_key in {"thermos", "other-remax"}:
            product.aliases.append("REMAX")
        if product.external_key == "thermos":
            product.offers[0].availability = "out_of_stock"

    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, products)
        db.commit()

    cases = {
        "Insta360全景相机价格": "INSTA 360 X3",
        "360度全景相機價格": "INSTA 360 X3",
        "摄影机价格": "Osmo Pocket 3",
        "攝影機價格": "Osmo Pocket 3",
        "旅行洗漱套装价格": "便攜分裝洗護洗漱杯套裝",
        "洗護套裝價格": "便攜分裝洗護洗漱杯套裝",
        "苹果Lightning数据线价格": "RC-134i",
        "保温杯价格": "智能保溫杯",
        "儿童相机价格": "兒童攝影相機",
        "小童相機價格": "兒童攝影相機",
        "Type-C数据线价格": "RC-134a",
        "Type-C 數據線價格": "RC-134a",
        "折叠双头风扇价格": "折疊雙頭小風扇",
        "迷你折叠风扇价格": "迷你折疊小風扇",
    }
    with SessionLocal() as db:
        for query, expected in cases.items():
            result = query_product_price_catalog(db, 1, query, language="zh-TW")
            assert result["found"] is True, query
            assert expected in result["segments"][0], query

        for query in ("运动摄影机", "運動攝影機"):
            documents = query_product_catalog_documents(db, 1, query)
            assert {str(document.metadata["title"]) for document in documents} == {
                "GoPro HERO 13",
                "INSTA 360 X3",
            }

        exact_stock = query_product_price_catalog(
            db,
            1,
            "REMAX未來系列智能保溫杯RT-IG02黑色价格",
            language="zh-TW",
        )
    assert exact_stock["found"] is True
    assert exact_stock["out_of_stock_count"] == 1
    assert "產品存在，目前缺貨" in exact_stock["segments"][0]
    assert "RC-131th" not in exact_stock["segments"][0]

    with SessionLocal() as db:
        complete = query_product_price_catalog(
            db,
            1,
            "完整價目表",
            language="zh-TW",
            full_catalog=True,
        )
    complete_text = "\n".join(complete["segments"])
    assert complete["found"] is True
    assert complete["out_of_stock_count"] == 1
    assert "HK$" in complete_text
    assert "REMAX未來系列智能保溫杯RT-IG02黑色｜產品存在，目前缺貨" in complete_text


def test_english_destination_aliases_are_exact_and_how_much_is_pricing(
    authenticated_client: TestClient,
):
    del authenticated_client
    source_id = create_source()
    products = [
        sample_product("jp-5g", "5G 日本", "48", destination="日本"),
        sample_product(
            "kr-4g",
            "4G 南韓",
            "28",
            category="wifi_4g",
            destination="南韓",
            network="4G",
        ),
        sample_product(
            "kr-esim",
            "韓國 eSIM",
            "45",
            category="esim",
            destination="韓國",
            network="4G/5G",
            product_type="esim",
        ),
        sample_product("th-5g", "5G 泰國", "46", destination="泰國"),
    ]
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, products)
        db.commit()

        assert is_product_catalog_query(db, 1, "Hi") is False
        assert is_product_catalog_query(
            db, 1, "Which internet option is best for Japan?"
        ) is True

        japan = query_product_price_catalog(
            db,
            1,
            "How much is Japan 5G WiFi?",
            language="en",
        )
        korea_esim = query_product_price_catalog(
            db,
            1,
            "What is the price of Korea eSIM?",
            language="en",
        )
        korea_options = query_product_catalog_documents(
            db,
            1,
            "Which internet option is best for Korea?",
            language="en",
        )

    japan_text = "\n".join(japan["segments"])
    assert japan["found"] is True
    assert "5G 日本" in japan_text
    assert "HK$48/day" in japan_text
    assert "Travel" not in japan_text
    assert "南韓" not in japan_text
    assert "泰國" not in japan_text
    assert "Current product prices" in japan_text

    korea_text = "\n".join(korea_esim["segments"])
    assert korea_esim["found"] is True
    assert "韓國 eSIM" in korea_text
    assert "5G 日本" not in korea_text
    assert "4G 南韓" not in korea_text
    assert {document.metadata["title"] for document in korea_options} == {
        "4G 南韓",
        "韓國 eSIM",
    }


def test_chinese_destination_rental_recommendation_uses_catalog_before_rag(
    authenticated_client: TestClient,
):
    """The conversational ``去日本租哪个比较好`` form must not hand off."""

    del authenticated_client
    source_id = create_source()
    products = [
        sample_product("jp-4g", "4G 日本", "28", network="4G"),
        sample_product("jp-5g", "5G 日本", "48", network="5G"),
        sample_product(
            "jp-esim",
            "日本 eSIM",
            "38",
            category="esim",
            product_type="esim",
            network="4G/5G",
        ),
        sample_product("kr-5g", "5G 韩国", "45", destination="韩国"),
    ]
    query = "我去日本租哪个比较好"
    traditional_query = "我去日本租哪個比較好"

    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, products)
        db.commit()

        assert is_product_recommendation_query(query) is True
        assert is_product_recommendation_query(traditional_query) is True
        assert is_product_catalog_query(db, 1, query) is True
        assert should_prioritize_product_catalog(query) is True
        # An unrelated destination recommendation must remain out of scope.
        assert is_product_catalog_query(db, 1, "去日本哪个景点好") is False

        documents = query_product_catalog_documents(
            db,
            1,
            query,
            language="zh-TW",
        )
        assert [document.metadata["title"] for document in documents] == [
            "4G 日本",
            "5G 日本",
        ]
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=98007,
            customer_name="目的地推薦測試",
            customer_phone="+85260000007",
            message=query,
            history=[],
        )

    assert result.route == "knowledge"
    assert result.handoff is False
    assert [source["source_type"] for source in result.sources[:2]] == [
        "structured_product",
        "structured_product",
    ]
    assert "4G 日本" in result.answer


def test_destination_recommendation_after_vip_context_stays_ai_owned(
    authenticated_client: TestClient,
):
    """A prior VIP answer must not poison the next destination route."""

    del authenticated_client
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(
            db,
            source,
            [
                sample_product("jp-4g", "4G 日本", "28", network="4G"),
                sample_product("jp-5g", "5G 日本", "48", network="5G"),
                sample_product(
                    "jp-esim",
                    "日本 eSIM",
                    "38",
                    category="esim",
                    product_type="esim",
                    network="4G/5G",
                ),
            ],
        )
        db.commit()

        first = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000097",
            phone="+85260000097",
            display_name="VIP后续测试",
            body="VIP有什么福利",
        )
        second = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000097",
            phone="+85260000097",
            display_name="VIP后续测试",
            body="我去日本租哪个比较好",
        )

        assert first.agent_result is not None
        assert first.agent_result.handoff is False
        assert second.agent_result is not None
        assert second.agent_result.route == "knowledge"
        assert second.agent_result.handoff is False
        assert "4G 日本" in second.agent_result.answer
        assert second.conversation.ai_enabled is True


def test_short_product_followups_reuse_the_previous_knowledge_context(
    authenticated_client: TestClient,
):
    """Links and usage clarifications stay on the active product session."""

    del authenticated_client
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(
            db,
            source,
            [
                sample_product("jp-4g", "4G 日本", "28", network="4G"),
                sample_product("jp-5g", "5G 日本", "48", network="5G"),
                sample_product(
                    "jp-esim",
                    "日本 eSIM",
                    "38",
                    category="esim",
                    product_type="esim",
                    network="4G/5G",
                ),
            ],
        )
        db.commit()

        first = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000098",
            phone="+85260000098",
            display_name="产品跟进测试",
            body="我去日本租哪个比较好",
        )
        second = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000098",
            phone="+85260000098",
            display_name="产品跟进测试",
            body="选择无限的10日 给我链接",
        )
        third = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000098",
            phone="+85260000098",
            display_name="产品跟进测试",
            body="我的用量大",
        )
        out_of_scope = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000098",
            phone="+85260000098",
            display_name="产品跟进测试",
            body="日本天气怎么样",
        )

    assert first.agent_result is not None and first.agent_result.handoff is False
    assert second.agent_result is not None
    assert second.agent_result.route == "knowledge"
    assert second.agent_result.handoff is False
    assert "日本" in second.agent_result.answer
    assert third.agent_result is not None
    assert third.agent_result.route == "knowledge"
    assert third.agent_result.handoff is False
    assert "日本" in third.agent_result.answer
    assert out_of_scope.agent_result is not None
    assert out_of_scope.agent_result.handoff is True


def test_english_common_product_aliases_match_structured_catalog(
    authenticated_client: TestClient,
):
    del authenticated_client
    source_id = create_source()
    specs = [
        ("gopro", "GoPro HERO 13", "40", "travel_gadget", "travel_gadget"),
        ("insta", "INSTA 360 X3", "40", "travel_gadget", "travel_gadget"),
        ("wash", "便攜分裝洗護洗漱杯套裝", "99", "eshop", "eshop_product"),
        ("thermos", "REMAX未來系列智能保溫杯RT-IG02黑色", "138", "eshop", "eshop_product"),
        ("child-camera", "兒童攝影相機", "198", "eshop", "eshop_product"),
        ("type-c", "REMAX 速捷數據線 RC-134a (Type-C)", "20", "eshop", "eshop_product"),
    ]
    products = [
        sample_product(
            key,
            name,
            price,
            category=category,
            destination=None,
            network=None,
            product_type=product_type,
        )
        for key, name, price, category, product_type in specs
    ]
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, products)
        db.commit()

        cases = {
            "How much is a Type-C cable?": "RC-134a",
            "Do you have a children's camera?": "Children's Camera",
            "Is the smart thermos available?": "REMAX Smart Thermos",
            "How much is the travel wash kit?": "Portable Travel Toiletry Kit",
        }
        for query, expected in cases.items():
            result = query_product_price_catalog(db, 1, query, language="en")
            assert result["found"] is True, query
            assert expected in "\n".join(result["segments"]), query

        camera_documents = query_product_catalog_documents(
            db,
            1,
            "I want to rent an action camera",
            language="en",
        )
        panoramic_documents = query_product_catalog_documents(
            db,
            1,
            "Do you rent a panoramic camera?",
            language="en",
        )

    assert {document.metadata["title"] for document in camera_documents} == {
        "GoPro HERO 13",
        "INSTA 360 X3",
    }
    assert {document.metadata["title"] for document in panoramic_documents} == {
        "INSTA 360 X3"
    }


def test_english_query_is_translated_before_chinese_knowledge_retrieval(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    with SessionLocal() as db:
        document = KnowledgeDocument(
            tenant_id=1,
            title="eSIM 安裝與啟用",
            content="eSIM 安裝步驟：掃描電郵中的二維碼，按畫面指示加入流動網絡並啟用。",
            source="test://esim-install",
            category="faq",
            is_active=True,
        )
        db.add(document)
        db.flush()
        rebuild_document_chunks(db, document, prefer_local=True)
        db.commit()

        def fake_model(prompt_value):
            prompt_text = prompt_value.to_string()
            if "Translate the customer's English support question" in prompt_text:
                return AIMessage(content="eSIM 安装 激活 二维码")
            return AIMessage(
                content="Scan the QR code from your email, then follow the on-screen steps to add and activate the eSIM."
            )

        monkeypatch.setattr(
            support_agent_workflow,
            "model",
            RunnableLambda(fake_model),
        )
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=98901,
            customer_name="English retrieval test",
            customer_phone="+85260009901",
            message="How do I install and activate an eSIM?",
            history=[],
        )

    assert result.route == "knowledge"
    assert result.handoff is False
    assert result.language == "en"
    assert result.answer.startswith("Scan the QR code")
    assert result.sources[0]["source"] == "test://esim-install"


def test_price_change_creates_history_without_duplicate_unchanged_snapshot(authenticated_client: TestClient):
    del authenticated_client
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        first = persist_product_catalog(db, source, [sample_product("jp-5g", "5G 日本", "48")])
        db.commit()
        assert first.new_products == 1
        assert first.new_offers == 1

    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        unchanged = persist_product_catalog(db, source, [sample_product("jp-5g", "5G 日本", "48")])
        db.commit()
        assert unchanged.unchanged_offers == 1
        assert db.scalar(select(func.count(ProductPriceHistory.id))) == 1

    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        changed = persist_product_catalog(db, source, [sample_product("jp-5g", "5G 日本", "52")])
        db.commit()
        assert changed.changed_offers == 1
        offer = db.scalar(select(ProductPriceOffer))
        assert offer.price_amount == Decimal("52.00")
        snapshots = db.scalars(
            select(ProductPriceHistory).order_by(ProductPriceHistory.observed_at)
        ).all()
        assert [item.change_type for item in snapshots] == ["created", "changed"]


def test_catalog_query_filters_and_segments_full_catalog(authenticated_client: TestClient):
    del authenticated_client
    source_id = create_source()
    products = [
        sample_product("jp-5g", "5G 日本", "48"),
        sample_product("kr-5g", "5G 韩国", "48", destination="韩国"),
        sample_product(
            "jp-esim",
            "日本 eSIM",
            "45",
            category="esim",
            product_type="esim",
            network="4G/5G",
            offers=21,
        ),
    ]
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, products)
        db.commit()

    with SessionLocal() as db:
        focused = query_product_price_catalog(db, 1, "日本 5G WiFi 价格")
        assert focused["found"] is True
        assert focused["count"] == 1
        assert "5G 日本" in focused["segments"][0]
        assert "韩国" not in focused["segments"][0]
        assert "购买链接：https://prices.example.com/jp-5g" in focused["segments"][0]

        clarification = query_product_price_catalog(db, 1, "价格")
        assert clarification["needs_clarification"] is True

        complete = query_product_price_catalog(db, 1, "请发全部价格")
        assert complete["full_catalog"] is True
        assert complete["count"] == 23
        assert len(complete["segments"]) == 2
        assert "（1/2）" in complete["segments"][0]


def test_langgraph_routes_price_request_to_read_only_catalog(authenticated_client: TestClient):
    del authenticated_client
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [sample_product("jp-5g", "5G 日本", "48")])
        db.commit()

    with SessionLocal() as db:
        result = support_agent_workflow.run(
            db,
            tenant_id=1,
            conversation_id=98765,
            customer_name="测试客户",
            customer_phone="+85260000000",
            message="日本5G價格多少？",
            history=[],
        )
    assert result.route == "pricing"
    assert result.handoff is False
    assert "HK$48" in result.answer
    assert "最新商品價格" in result.answer
    assert "購買連結：https://prices.example.com/jp-5g" in result.answer


def test_price_reply_prefers_offer_checkout_url(authenticated_client: TestClient):
    del authenticated_client
    source_id = create_source()
    product = sample_product(
        "kr-esim",
        "韩国 eSIM",
        "45",
        category="esim",
        destination="韩国",
        network="4G/5G",
        product_type="esim",
    )
    product.offers[0].metadata = {
        "order_url": "https://checkout.example.com/esim?plan=kr-3-5"
    }
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(db, source, [product])
        db.commit()

    with SessionLocal() as db:
        result = query_product_price_catalog(db, 1, "韩国 eSIM 多少钱")
    assert result["found"] is True
    assert "购买链接：https://checkout.example.com/esim?plan=kr-3-5" in result["segments"][0]
    assert "购买链接：https://prices.example.com/kr-esim" not in result["segments"][0]


def test_price_clarification_followup_reuses_previous_question(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    sent: list[str] = []

    install_demo_send_spy(monkeypatch, sent, "context-reply")
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(
            db,
            source,
            [sample_product("kr-5g", "5G 韩国", "48", destination="韩国")],
        )
        db.commit()

        first = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000088",
            phone="+85260000088",
            display_name="上下文测试",
            body="四人去哪个划算",
        )
        assert first.agent_result is not None
        assert first.agent_result.route == "pricing"
        assert first.agent_result.awaiting_input == "pricing_filter"
        assert "目的地" in first.agent_result.answer

        second = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000088",
            phone="+85260000088",
            display_name="上下文测试",
            body="韩国",
        )
        assert second.agent_result is not None
        assert second.agent_result.route == "pricing"
        assert second.agent_result.handoff is False
        assert "5G 韩国" in second.agent_result.answer
        assert "购买链接：https://prices.example.com/kr-5g" in second.agent_result.answer
        assert second.agent_result.language == "zh-CN"

        third = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000088",
            phone="+85260000088",
            display_name="上下文测试",
            body="一個人去七天要多少錢",
        )
        assert third.agent_result is not None
        assert third.agent_result.route == "pricing"
        assert third.agent_result.handoff is False
        assert "5G 韓國" in third.agent_result.answer
        assert "一個人去七天要多少錢" in (third.agent_result.context_query or "")

        inbound_messages = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == third.conversation.id,
                Message.direction == "inbound",
            )
            .order_by(Message.id)
        ).all()
        session_ids = {
            item.metadata_json.get("ai_context_session_id")
            for item in inbound_messages
        }
        assert len(session_ids) == 1
        assert None not in session_ids
        assert "HK$48" in second.agent_result.answer
        outbound = [
            item
            for item in second.conversation.messages
            if item.sender_type == "ai"
        ]
        assert outbound[0].metadata_json["awaiting_input"] == "pricing_filter"
        assert outbound[0].metadata_json["context_query"] == "四人去哪个划算"
        assert outbound[-1].metadata_json["awaiting_input"] is None

    assert len(sent) == 3


def test_price_duration_addition_followup_recalculates_total(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    sent: list[str] = []

    install_demo_send_spy(monkeypatch, sent, "duration-followup")
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(
            db,
            source,
            [sample_product("jp-5g-duration", "5G 日本", "48")],
        )
        db.commit()

        first = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000091",
            phone="+85260000091",
            display_name="租期追问测试",
            body="我去日本七天租5gwifi需要多少钱",
        )
        assert first.agent_result is not None
        assert "HK$48/天 × 7天 = HK$336" in first.agent_result.answer

        second = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000091",
            phone="+85260000091",
            display_name="租期追问测试",
            body="我多加两天 需要多少钱",
        )
        assert second.agent_result is not None
        assert second.agent_result.route == "pricing"
        assert second.agent_result.handoff is False
        assert "HK$48/天 × 9天 = HK$432" in second.agent_result.answer
        assert "HK$48/天 × 7天 = HK$336" not in second.agent_result.answer
        assert second.agent_result.rental_period == {
            "days": 9,
            "start_date": None,
            "end_date": None,
            "source": "duration_adjusted",
        }
        combined = query_product_price_catalog(
            db,
            1,
            "我去日本七天租5gwifi需要多少钱 我多加两天需要多少钱",
            language="zh-TW",
        )
        assert combined["rental_period"]["days"] == 9
        assert "HK$48/日 × 9日 = HK$432" in "\n".join(combined["segments"])

        outbound = [
            item
            for item in second.conversation.messages
            if item.sender_type == "ai"
        ]
        assert outbound[-1].metadata_json["rental_period"]["days"] == 9

        # Repeating the destination must not reset the active quote context.
        third = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000091",
            phone="+85260000091",
            display_name="租期追问测试",
            body="日本多加一天 需要多少钱",
        )
        assert third.agent_result is not None
        assert "HK$48/天 × 10天 = HK$480" in third.agent_result.answer
        assert "HK$48/天 × 9天 = HK$432" not in third.agent_result.answer
        assert third.agent_result.rental_period["days"] == 10

    assert len(sent) == 3


def test_explicit_new_destination_replaces_previous_price_context(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    sent: list[str] = []

    install_demo_send_spy(monkeypatch, sent, "destination-switch")
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(
            db,
            source,
            [
                sample_product(
                    "jp-esim",
                    "日本 eSIM",
                    "48",
                    category="esim",
                    product_type="esim",
                    network="4G/5G",
                ),
                sample_product(
                    "kr-esim",
                    "韩国 eSIM",
                    "45",
                    category="esim",
                    destination="韩国",
                    product_type="esim",
                    network="4G/5G",
                ),
            ],
        )
        db.commit()

        first = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000089",
            phone="+85260000089",
            display_name="目的地切换测试",
            body="日本 eSIM 多少钱",
        )
        assert first.agent_result is not None
        assert "日本 eSIM" in first.agent_result.answer

        second_query = "去韩国推荐哪个套餐"
        second = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000089",
            phone="+85260000089",
            display_name="目的地切换测试",
            body=second_query,
        )
        assert second.agent_result is not None
        assert second.agent_result.route == "pricing"
        assert "韩国 eSIM" in second.agent_result.answer
        assert "日本 eSIM" not in second.agent_result.answer
        assert second.agent_result.context_query == second_query

        third = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000089",
            phone="+85260000089",
            display_name="目的地切换测试",
            body="五天呢",
        )
        assert third.agent_result is not None
        assert "韩国 eSIM" in third.agent_result.answer
        assert "日本 eSIM" not in third.agent_result.answer
        assert second_query in (third.agent_result.context_query or "")
        assert "五天呢" in (third.agent_result.context_query or "")

    assert len(sent) == 3


def test_price_followup_after_product_knowledge_reuses_country(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    sent: list[str] = []

    install_demo_send_spy(monkeypatch, sent, "knowledge-price")
    source_id = create_source()
    with SessionLocal() as db:
        source = db.get(ProductPriceSource, source_id)
        persist_product_catalog(
            db,
            source,
            [
                sample_product(
                    "cn-wifi",
                    "5G 中国 自动翻墙",
                    "55",
                    destination="中国 自动翻墙",
                ),
                sample_product(
                    "cn-esim",
                    "中国内地 eSIM",
                    "45",
                    category="esim",
                    destination="中国内地",
                    product_type="esim",
                    network="4G/5G",
                ),
                sample_product("jp-5g", "5G 日本", "48"),
            ],
        )
        document = KnowledgeDocument(
            tenant_id=1,
            title="中国内地上网产品",
            content=(
                "中国内地可选择 eSIM 或 WiFi 蛋。eSIM 适合单人手机使用，"
                "WiFi 蛋适合多人和多部设备共享。"
            ),
            source="test://china-products",
            category="product",
            is_active=True,
        )
        db.add(document)
        db.flush()
        rebuild_document_chunks(db, document, prefer_local=True)
        db.commit()

        first_query = "中国的有哪些产品"
        first = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000090",
            phone="+85260000090",
            display_name="知识转价格测试",
            body=first_query,
        )
        assert first.agent_result is not None
        assert first.agent_result.route == "knowledge"
        assert "中国内地" in first.agent_result.answer

        second = receive_inbound(
            db,
            tenant_id=1,
            wa_id="85260000090",
            phone="+85260000090",
            display_name="知识转价格测试",
            body="多少钱",
        )
        assert second.agent_result is not None
        assert second.agent_result.route == "pricing"
        assert second.agent_result.awaiting_input is None
        assert "中国" in second.agent_result.answer
        assert "日本" not in second.agent_result.answer
        assert first_query in (second.agent_result.context_query or "")
        assert "多少钱" in (second.agent_result.context_query or "")

        direct = query_product_price_catalog(db, 1, "中国的产品多少钱")
        assert direct["found"] is True
        assert direct["needs_clarification"] is False
        assert "中国" in direct["segments"][0]
        assert "日本" not in direct["segments"][0]

    assert len(sent) == 2


def test_product_price_source_api_groups_products_by_url(
    authenticated_client: TestClient,
    monkeypatch,
):
    def fake_normalize(value: str) -> str:
        assert value
        return "https://catalog.example.com/"

    def fake_sync(source_id: int, **kwargs):
        with SessionLocal() as db:
            source = db.get(ProductPriceSource, source_id)
            persist_product_catalog(
                db,
                source,
                [sample_product("jp-5g", "5G 日本", "48")],
            )
            source.status = "completed"
            source.imported_products = 1
            source.imported_offers = 1
            source.completed_at = utcnow()
            run = db.get(ProductPriceSyncRun, kwargs.get("sync_run_id"))
            if run is not None:
                run.status = "completed"
                run.completed_at = utcnow()
            db.commit()

    monkeypatch.setattr(
        "backend.app.api.product_prices.normalize_public_root_url",
        fake_normalize,
    )
    monkeypatch.setattr(
        "backend.app.api.product_prices.run_product_price_sync",
        fake_sync,
    )
    response = authenticated_client.post(
        "/api/product-prices/sources",
        json={"root_url": "https://catalog.example.com", "name": "香港产品站"},
    )
    assert response.status_code == 202
    listed_sources = authenticated_client.get("/api/product-prices/sources").json()
    assert listed_sources[0]["root_url"] == "https://catalog.example.com/"
    listed_products = authenticated_client.get("/api/product-prices/products").json()
    assert listed_products[0]["source_name"] == "香港产品站"
    assert listed_products[0]["source_url"] == "https://catalog.example.com/"


def test_segmented_price_reply_sends_and_stores_each_whatsapp_part(
    authenticated_client: TestClient,
    monkeypatch,
):
    del authenticated_client
    sent: list[str] = []

    install_demo_send_spy(monkeypatch, sent, "provider-part")
    with SessionLocal() as db:
        contact = Contact(
            tenant_id=1,
            wa_id="85260000001",
            phone="+85260000001",
            display_name="价格测试客户",
        )
        db.add(contact)
        db.flush()
        conversation = Conversation(tenant_id=1, contact_id=contact.id, subject="完整价目表")
        db.add(conversation)
        db.flush()
        inbound = Message(
            tenant_id=1,
            conversation_id=conversation.id,
            external_id="segmented-price-inbound-1",
            direction="inbound",
            sender_type="customer",
            sender_name=contact.display_name,
            body="完整價目表",
        )
        db.add(inbound)
        db.commit()
        db.refresh(conversation)
        result = AgentResult(
            route="pricing",
            answer="这个价格\n\n请稍后",
            handoff=False,
            sources=[{"source_id": 1, "title": "测试价格站", "source": "https://example.com"}],
            reply_parts=["这个价格", "请稍后"],
        )
        _store_agent_result(
            db,
            conversation,
            contact,
            result,
            source_message_id=inbound.id,
        )
        messages = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.direction == "outbound",
            )
            .order_by(Message.id)
        ).all()
    assert sent == [
        format_ai_customer_message("這個價格", language="zh-TW"),
        format_ai_customer_message(
            "請稍後\n\n參考來源\n[1] 測試價格站\nhttps://example.com",
            language="zh-TW",
        ),
    ]
    assert [message.body for message in messages] == sent
    assert [message.metadata_json["part_index"] for message in messages] == [1, 2]
    assert all(message.metadata_json["part_count"] == 2 for message in messages)
    assert all(message.metadata_json["language"] == "zh-TW" for message in messages)
    assert all(message.metadata_json["sources"][0]["title"] == "測試價格站" for message in messages)
