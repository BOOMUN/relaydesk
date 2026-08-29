from __future__ import annotations

import json
from decimal import Decimal

from fastapi.testclient import TestClient

from backend.app.database import SessionLocal
from backend.app.models import KnowledgeDocument, ProductPriceSource
from backend.app.services.knowledge_ingestion import rebuild_document_chunks
from backend.app.services.product_price_ingestion import (
    ScrapedOffer,
    ScrapedProduct,
    persist_product_catalog,
)
from backend.app.services.quality_evaluation import (
    load_latest_quality_report,
    run_quality_evaluation,
    save_quality_report,
)


def _suite_file(tmp_path):
    payload = {
        "version": "test-suite",
        "intent_cases": [
            {"id": "greeting", "query": "Hi", "expected_route": "greeting"},
            {
                "id": "cancel",
                "query": "Cancel order ORD-1001",
                "expected_route": "handoff",
            },
        ],
        "country_recall_cases": [
            {
                "id": "japan",
                "query": "Which internet options are available for Japan?",
                "expected_destination_terms": ["日本"],
            }
        ],
        "product_recall_cases": [
            {
                "id": "type-c",
                "query": "Do you sell a Type-C cable?",
                "expected_name_terms": ["rc-134a"],
            }
        ],
        "retrieval_cases": [
            {
                "id": "fup",
                "query": "What is FUP?",
                "expected_source_terms": ["fup-guide"],
            }
        ],
    }
    path = tmp_path / "quality-suite.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _seed_quality_records() -> None:
    with SessionLocal() as db:
        source = ProductPriceSource(
            tenant_id=1,
            created_by_user_id=1,
            name="Quality catalogue",
            root_url="https://quality.example.com/",
            domain="quality.example.com",
            adapter="schema_org",
            status="completed",
        )
        db.add(source)
        db.flush()
        products = [
            ScrapedProduct(
                external_key="jp-5g",
                canonical_url="https://quality.example.com/japan",
                name="5G 日本",
                name_translations={"zh-TW": "5G 日本", "en": "5G Japan"},
                aliases=["日本", "Japan"],
                category="wifi_5g",
                product_type="wifi_rental",
                destination="日本",
                network="5G",
                description="日本 WiFi",
                metadata={},
                offers=[
                    ScrapedOffer(
                        external_key="jp-day",
                        label="每日租用",
                        currency="HKD",
                        price_amount=Decimal("48"),
                        unit="day",
                    )
                ],
            ),
            ScrapedProduct(
                external_key="type-c",
                canonical_url="https://quality.example.com/type-c",
                name="REMAX 速捷數據線 RC-134a (Type-C)",
                name_translations={"zh-TW": "REMAX 速捷數據線 RC-134a (Type-C)"},
                aliases=["RC-134a"],
                category="eshop",
                product_type="eshop_product",
                destination=None,
                network=None,
                description="Type-C 數據線",
                metadata={},
                offers=[
                    ScrapedOffer(
                        external_key="type-c-black",
                        label="黑",
                        currency="HKD",
                        price_amount=Decimal("20"),
                    )
                ],
            ),
        ]
        persist_product_catalog(db, source, products)
        document = KnowledgeDocument(
            tenant_id=1,
            title="公平使用政策 FUP",
            content="FUP 是公平使用政策，用于说明无限流量方案的合理使用限制。",
            source="https://quality.example.com/fup-guide",
            category="policy",
            is_active=True,
        )
        db.add(document)
        db.flush()
        rebuild_document_chunks(db, document, prefer_local=True)
        db.commit()


def test_quality_evaluation_calculates_labelled_metrics(
    authenticated_client: TestClient,
    tmp_path,
):
    del authenticated_client
    _seed_quality_records()
    suite = _suite_file(tmp_path)
    with SessionLocal() as db:
        report = run_quality_evaluation(
            db,
            1,
            live_model=False,
            suite_path=suite,
        )
    assert report["suite_version"] == "test-suite"
    assert report["metrics"] == {
        "intent_accuracy_pct": 100.0,
        "country_recall_pct": 100.0,
        "product_recall_pct": 100.0,
        "retrieval_top1_accuracy_pct": 100.0,
        "retrieval_top3_accuracy_pct": 100.0,
    }


def test_latest_quality_report_api_is_tenant_scoped(
    authenticated_client: TestClient,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "backend.app.services.quality_evaluation.QUALITY_REPORT_DIR",
        tmp_path,
    )
    report = {
        "suite_version": "api-test",
        "generated_at": "2026-08-25T00:00:00+00:00",
        "tenant_id": 1,
        "live_model": False,
        "embedding_provider": "local_hash_embeddings",
        "metrics": {
            "intent_accuracy_pct": 90.0,
            "country_recall_pct": 80.0,
            "product_recall_pct": 70.0,
            "retrieval_top1_accuracy_pct": 60.0,
            "retrieval_top3_accuracy_pct": 85.0,
        },
        "intent_accuracy": {"correct_count": 9, "case_count": 10},
        "country_recall": {"hit_count": 8, "expected_count": 10, "evaluated_cases": 2},
        "product_recall": {"hit_count": 7, "expected_count": 10, "evaluated_cases": 2},
        "retrieval_accuracy": {
            "top1_correct_count": 6,
            "top3_correct_count": 8,
            "evaluated_cases": 10,
        },
    }
    save_quality_report(report)
    assert load_latest_quality_report(1) == report
    response = authenticated_client.get("/api/quality-evaluation/latest")
    assert response.status_code == 200
    assert response.json()["metrics"]["intent_accuracy_pct"] == 90.0
