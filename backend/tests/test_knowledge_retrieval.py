from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.database import SessionLocal
from backend.app.models import KnowledgeChunk, KnowledgeDocument
from backend.app.services.knowledge import (
    normalize_retrieval_text,
    retrieve_knowledge,
)
from backend.app.services.knowledge_ingestion import rebuild_document_chunks


DESTINATION_CASES = (
    ("去日本哪个好", "日本", "日本上網方案比較"),
    ("去韩国哪个好", "韩国", "韓國上網方案比較"),
    ("去台湾哪个好", "台湾", "台灣上網方案比較"),
    ("去泰国哪个好", "泰国", "泰國上網方案比較"),
    ("去中国哪个好", "中国", "中國上網方案比較"),
    ("去新加坡哪个好", "新加坡", "新加坡上網方案比較"),
    ("去马来西亚哪个好", "马来西亚", "馬來西亞上網方案比較"),
    ("去欧洲哪个好", "欧洲", "歐洲上網方案比較"),
    ("去美国哪个好", "美国", "美國上網方案比較"),
    ("去澳洲哪个好", "澳洲", "澳洲上網方案比較"),
)


def _add_document(db: Session, *, title: str, content: str) -> None:
    document = KnowledgeDocument(
        tenant_id=1,
        title=title,
        content=content,
        source=f"test://{len(db.new) + 1}",
        category="product",
        is_active=True,
    )
    db.add(document)
    db.flush()
    rebuild_document_chunks(db, document, prefer_local=True)


def test_retrieval_normalization_unifies_simplified_and_traditional() -> None:
    assert normalize_retrieval_text("去韩国哪个好") == normalize_retrieval_text(
        "去韓國哪個好"
    )
    assert normalize_retrieval_text("台湾 eSIM") == normalize_retrieval_text(
        "台灣 eSIM"
    )


def test_country_filter_and_vector_rerank_cover_destination_queries(client) -> None:
    del client
    with SessionLocal() as db:
        for _query, _country, title in DESTINATION_CASES:
            _add_document(
                db,
                title=title,
                content=(
                    f"{title}。單人旅行可考慮 eSIM；多人同行或需要多部裝置時，"
                    "WiFi 蛋較方便，應按手機支援及同行人數選擇。"
                ),
            )
        db.commit()

        for query, expected_country, _title in DESTINATION_CASES:
            matches = retrieve_knowledge(db, 1, query)
            assert matches, query
            top = matches[0]
            assert expected_country in normalize_retrieval_text(top.metadata["title"])
            assert top.metadata["retrieval_mode"] == "destination_hybrid"
            assert top.metadata["destination_match"] == "title"
            assert top.metadata["retrieval_score"] >= 0.15


def test_explicit_destination_never_falls_back_to_another_country(client) -> None:
    del client
    with SessionLocal() as db:
        _add_document(
            db,
            title="日本上網方案比較",
            content="日本旅行可選擇 WiFi 蛋、SIM 卡或 eSIM。",
        )
        db.commit()

        assert retrieve_knowledge(db, 1, "去韩国哪个好") == []


def test_hybrid_reranker_deduplicates_chunks_from_the_same_source(client) -> None:
    del client
    with SessionLocal() as db:
        repeated = "香港机场 WiFi 蛋取机与归还柜台说明。" * 140
        primary = KnowledgeDocument(
            tenant_id=1,
            title="香港机场 WiFi 蛋取还指南",
            content=repeated,
            source="https://songwifi.example/guides/airport-pickup",
            category="service",
            is_active=True,
        )
        secondary = KnowledgeDocument(
            tenant_id=1,
            title="香港机场客服与营业时间",
            content="机场柜台提供 WiFi 蛋领取咨询及营业时间说明。",
            source="https://songwifi.example/guides/airport-hours",
            category="service",
            is_active=True,
        )
        db.add_all((primary, secondary))
        db.flush()
        rebuild_document_chunks(db, primary, prefer_local=True)
        rebuild_document_chunks(db, secondary, prefer_local=True)
        db.commit()

        matches = retrieve_knowledge(db, 1, "香港机场 WiFi 蛋在哪里取机和归还？", limit=3)
        document_ids = [item.metadata["document_id"] for item in matches]
        assert document_ids[0] == primary.id
        assert len(document_ids) == len(set(document_ids))
        assert all(
            item.metadata["reranker"] == "bm25_vector_metadata_v1"
            for item in matches
        )


def test_semantic_only_candidate_below_fused_threshold_is_rejected(
    client,
    monkeypatch,
) -> None:
    del client
    with SessionLocal() as db:
        document = KnowledgeDocument(
            tenant_id=1,
            title="退货政策",
            content="客户签收后七天内可以申请退货。",
            source="test://returns",
            category="policy",
            is_active=True,
        )
        db.add(document)
        db.flush()
        rebuild_document_chunks(db, document, prefer_local=True)
        db.flush()
        chunk = db.query(KnowledgeChunk).filter_by(document_id=document.id).one()
        chunk.embedding_model = "forced-semantic-model"
        chunk.embedding = [1.0, 0.0]
        db.commit()

        monkeypatch.setattr(
            "backend.app.services.knowledge._query_vectors",
            lambda *_args, **_kwargs: [[1.0, 0.0]],
        )
        assert retrieve_knowledge(db, 1, "completely unrelated semantic collision") == []
