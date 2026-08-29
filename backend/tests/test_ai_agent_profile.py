from __future__ import annotations

from sqlalchemy import select

from backend.app.database import SessionLocal
from backend.app.models import AgentProfileVersion, KnowledgeChunk, KnowledgeDocument
from backend.app.services.knowledge_ingestion import (
    estimate_token_count,
    rebuild_document_chunks,
    split_content_sections,
)
from backend.app.services.quality_evaluation import load_realistic_suite


def test_realistic_evaluation_suite_has_required_coverage():
    suite = load_realistic_suite()
    assert 30 <= len(suite["cases"]) <= 50
    categories = {case["category"] for case in suite["cases"]}
    assert {"price", "refund", "pickup_return", "fault", "no_answer", "multilingual"} <= categories


def test_agent_profile_requires_publish_and_supports_rollback(authenticated_client):
    profile = authenticated_client.get("/api/ai-agent")
    assert profile.status_code == 200
    initial = profile.json()
    assert initial["active_version"] is None
    assert initial["draft_version"]["status"] == "draft"

    saved = authenticated_client.patch(
        "/api/ai-agent/draft",
        json={
            "identity": "测试客服 AI",
            "service_scope": ["WiFi 咨询"],
            "tone": "简洁",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["status"] == "draft"

    published = authenticated_client.post("/api/ai-agent/publish")
    assert published.status_code == 200
    first_published = published.json()["active_version"]
    assert first_published["status"] == "published"

    second = authenticated_client.patch(
        "/api/ai-agent/draft",
        json={"tone": "更友好"},
    )
    assert second.status_code == 200
    assert authenticated_client.get("/api/ai-agent").json()["active_version"]["id"] == first_published["id"]
    assert authenticated_client.post("/api/ai-agent/publish").status_code == 200

    rollback = authenticated_client.post(
        f"/api/ai-agent/versions/{first_published['id']}/rollback"
    )
    assert rollback.status_code == 200
    restored = rollback.json()["active_version"]
    assert restored["status"] == "published"
    assert restored["rollback_from_version_id"] == first_published["id"]
    assert restored["id"] != first_published["id"]


def test_heading_aware_chunks_keep_source_metadata(client):
    del client
    content = "\n\n".join(
        [
            "# Japan WiFi",
            "## Pickup\n" + ("Pickup instructions and airport counter details. " * 80),
            "## Return\n" + ("Return instructions and packaging details. " * 80),
        ]
    )
    drafts = split_content_sections(content, title="Japan WiFi")
    assert drafts
    assert all(item.section_path for item in drafts)
    assert all(item.token_count == estimate_token_count(item.content) for item in drafts)
    assert all(item.token_count <= 800 for item in drafts)

    with SessionLocal() as db:
        document = KnowledgeDocument(
            tenant_id=1,
            title="Japan WiFi",
            content=content,
            source="https://example.com/japan",
            category="service",
            is_active=True,
        )
        db.add(document)
        db.flush()
        rebuild_document_chunks(db, document, prefer_local=True)
        db.commit()
        chunks = db.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
        ).all()
        assert chunks
        assert all(chunk.source_url == document.source for chunk in chunks)
        assert all(chunk.page_title == document.title for chunk in chunks)
        assert all(chunk.section_path for chunk in chunks)
        assert all(chunk.token_count > 0 for chunk in chunks)
