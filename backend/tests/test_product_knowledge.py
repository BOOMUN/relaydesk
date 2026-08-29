from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from backend.app.database import SessionLocal
from backend.app.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgePageRevision,
    KnowledgeSource,
    KnowledgeWebPage,
    Product,
    ProductPriceSource,
)
from backend.app.services.knowledge import retrieve_knowledge
from backend.app.services.knowledge_ingestion import (
    persist_catalog_product_page,
    publish_source_changes,
    rebuild_document_chunks,
    run_crawl_job,
)
from backend.app.services.product_knowledge import catalog_product_knowledge_pages
from backend.app.services.product_price_ingestion import (
    ScrapedOffer,
    ScrapedProduct,
    persist_product_catalog,
)
from backend.app.services.web_crawler import CrawledPage


def _product(
    key: str,
    name: str,
    category: str,
    product_type: str,
    url: str,
    *,
    destination: str | None = None,
    network: str | None = None,
) -> ScrapedProduct:
    return ScrapedProduct(
        external_key=key,
        canonical_url=url,
        name=name,
        name_translations={"zh-CN": name, "zh-TW": name},
        aliases=[name],
        category=category,
        product_type=product_type,
        destination=destination,
        network=network,
        description=f"{name} 的官方产品介绍与使用说明。",
        metadata={},
        offers=[
            ScrapedOffer(
                external_key="default",
                label="每日租用" if "rental" in product_type or category == "travel_gadget" else "藍色",
                currency="HKD",
                price_amount=Decimal("138.00"),
                unit="day" if "rental" in product_type or category == "travel_gadget" else "item",
            )
        ],
    )


def _create_sources_and_products() -> tuple[int, int]:
    with SessionLocal() as db:
        knowledge_source = KnowledgeSource(
            tenant_id=1,
            created_by_user_id=1,
            root_url="https://songwifi.example/",
            domain="songwifi.example",
            status="queued",
            max_pages=100,
            max_depth=5,
        )
        price_source = ProductPriceSource(
            tenant_id=1,
            created_by_user_id=1,
            name="SongWiFi test catalogue",
            root_url="https://songwifi.example/",
            domain="songwifi.example",
            adapter="songwifi",
            status="completed",
        )
        db.add_all((knowledge_source, price_source))
        db.flush()
        persist_product_catalog(
            db,
            price_source,
            [
                _product(
                    "wifi-jp-5g",
                    "5G 日本",
                    "wifi_5g",
                    "wifi_rental",
                    "https://songwifi.example/order/song-wifi?destination_id=138",
                    destination="日本",
                    network="5G",
                ),
                _product(
                    "gopro",
                    "GoPro HERO 13",
                    "travel_gadget",
                    "travel_gadget",
                    "https://songwifi.example/travel-gadgets/gopro",
                ),
                _product(
                    "child-camera",
                    "兒童攝影相機",
                    "eshop",
                    "eshop_product",
                    "https://songwifi.example/eshop/product/133",
                ),
                _product(
                    "jp-esim",
                    "日本 eSIM",
                    "esim",
                    "esim",
                    "https://songwifi.example/esim/jp",
                    destination="日本",
                    network="4G/5G",
                ),
            ],
        )
        db.commit()
        return knowledge_source.id, price_source.id


def test_catalog_product_pages_cover_wifi_gadgets_and_eshop_without_live_prices(
    authenticated_client,
):
    del authenticated_client
    knowledge_source_id, _ = _create_sources_and_products()
    with SessionLocal() as db:
        source = db.get(KnowledgeSource, knowledge_source_id)
        pages = catalog_product_knowledge_pages(db, source)

    assert {page.metadata["product_category"] for page in pages} == {
        "wifi_5g",
        "travel_gadget",
        "eshop",
    }
    assert {page.title for page in pages} == {
        "5G 日本",
        "GoPro HERO 13",
        "兒童攝影相機",
    }
    assert all(page.metadata["extraction_mode"] == "structured_product_catalog" for page in pages)
    assert all("HK$" not in page.content for page in pages)
    assert all("結構化產品目錄" in page.content for page in pages)
    assert all("/esim/" not in page.url for page in pages)

    pages_by_key = {
        page.metadata["product_external_key"]: page
        for page in pages
    }
    child_camera = pages_by_key["child-camera"]
    assert "儿童相机" in child_camera.content
    assert "兒童相機" in child_camera.content
    assert "儿童数码相机" in child_camera.metadata["search_aliases"]
    assert "兒童數碼相機" in child_camera.metadata["search_aliases"]

    gopro = pages_by_key["gopro"]
    assert "运动相机" in gopro.content
    assert "運動相機" in gopro.content
    assert "运动摄影机" in gopro.metadata["search_aliases"]
    assert "運動攝影機" in gopro.metadata["search_aliases"]


def test_crawl_job_persists_catalog_product_pages_as_reviewable_rag_documents(
    authenticated_client,
    monkeypatch,
):
    del authenticated_client
    knowledge_source_id, _ = _create_sources_and_products()

    class FakeCrawler:
        def __init__(self, *_args, **_kwargs):
            self.discovered_count = 1
            self.failed_count = 0
            self.limit_reached = False
            self.errors: list[str] = []

        def crawl(self):
            yield CrawledPage(
                url="https://songwifi.example/",
                title="SongWiFi",
                content="SongWiFi 官方网站首页及客户服务说明。",
                content_type="html",
                language="zh-TW",
                metadata={"http_content_type": "text/html"},
            )

    monkeypatch.setattr(
        "backend.app.services.knowledge_ingestion.WebsiteCrawler",
        FakeCrawler,
    )
    result = run_crawl_job(knowledge_source_id, trigger="test")
    assert result.status == "completed"

    with SessionLocal() as db:
        source = db.get(KnowledgeSource, knowledge_source_id)
        pages = db.scalars(
            select(KnowledgeWebPage).where(KnowledgeWebPage.source_id == source.id)
        ).all()
        product_pages = [
            page
            for page in pages
            if page.metadata_json.get("extraction_mode") == "structured_product_catalog"
        ]
        assert source.imported_pages == 4
        assert source.discovered_pages == 4
        assert len(product_pages) == 3
        assert all(page.review_status == "published" for page in product_pages)
        assert all(
            db.scalar(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.document_id == page.document_id
                )
            )
            is not None
            for page in product_pages
        )

        matches = retrieve_knowledge(db, 1, "请问有儿童相机吗")
        assert matches
        assert matches[0].metadata["title"] == "兒童攝影相機"
        assert matches[0].metadata["retrieval_mode"] == "product_catalog_hybrid"
        assert matches[0].metadata["product_catalog_match"] is True

        gopro_matches = retrieve_knowledge(db, 1, "我想租GoPro")
        assert gopro_matches
        assert gopro_matches[0].metadata["title"] == "GoPro HERO 13"

        wifi_matches = retrieve_knowledge(db, 1, "日本5G WiFi有吗")
        assert wifi_matches
        assert wifi_matches[0].metadata["title"] == "5G 日本"
        assert wifi_matches[0].metadata["catalog_boost"] > 0

        guide = KnowledgeDocument(
            tenant_id=1,
            title="日本旅行上网比较｜5G WiFi 蛋还是电话卡",
            content=(
                "日本自由行选择上网方案时，可以比较 5G WiFi 蛋与旅行电话卡。"
                "多人共享、网络覆盖、取还方式及每日报价都应在出发前确认。"
            ),
            source="https://songwifi.example/guides/japan-wifi-vs-sim",
            category="product",
            is_active=True,
        )
        db.add(guide)
        db.flush()
        rebuild_document_chunks(db, guide, prefer_local=True)
        db.commit()

        comparison_matches = retrieve_knowledge(
            db,
            1,
            "日本旅行应该租5G WiFi还是买电话卡？",
        )
        assert comparison_matches
        assert comparison_matches[0].metadata["document_id"] == guide.id
        assert comparison_matches[0].metadata["reranker"] == "bm25_vector_metadata_v1"
        assert all(item.metadata["catalog_boost"] == 0 for item in comparison_matches)

        child_camera = db.scalar(
            select(Product).where(Product.external_key == "child-camera")
        )
        child_camera.description = "兒童相機新增防震保護功能及掛繩說明。"
        db.commit()
        updated_page = next(
            page
            for page in catalog_product_knowledge_pages(db, source)
            if page.metadata["product_external_key"] == "child-camera"
        )
        updated = persist_catalog_product_page(db, source, updated_page)
        assert updated.change == "changed"
        updated_web_page = db.get(KnowledgeWebPage, updated.web_page_id)
        updated_document = db.get(KnowledgeDocument, updated_web_page.document_id)
        assert updated_web_page.review_status == "published"
        assert updated_document.is_active is True
        assert "新增防震保護功能" in updated_document.content
        assert db.scalar(
            select(KnowledgePageRevision.id).where(
                KnowledgePageRevision.web_page_id == updated_web_page.id,
                KnowledgePageRevision.status == "draft",
            )
        ) is None

        updated_page.metadata["alias_sync_marker"] = "metadata-only-change"
        metadata_only = persist_catalog_product_page(db, source, updated_page)
        assert metadata_only.change == "unchanged"
        db.refresh(updated_web_page)
        assert (
            updated_web_page.metadata_json["alias_sync_marker"]
            == "metadata-only-change"
        )

        published_new, published_updates = publish_source_changes(db, source)
        assert (published_new, published_updates) == (1, 0)
        active_product_documents = db.scalars(
            select(KnowledgeDocument)
            .join(KnowledgeWebPage, KnowledgeWebPage.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeWebPage.source_id == source.id,
                KnowledgeWebPage.metadata_json.is_not(None),
                KnowledgeDocument.is_active.is_(True),
            )
        ).all()
        assert len(active_product_documents) == 4
