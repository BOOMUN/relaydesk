from __future__ import annotations

import pytest
from langchain_core.embeddings import Embeddings
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from backend.app.models import KnowledgeChunk
from backend.app.services import embeddings as embedding_service
from backend.app.services.embeddings import EmbeddingProviderError, embed_documents


def test_postgresql_embedding_column_is_fixed_vector_dimension() -> None:
    ddl = str(
        CreateTable(KnowledgeChunk.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "embedding VECTOR(384)" in ddl


def test_sqlite_regression_column_remains_json() -> None:
    ddl = str(
        CreateTable(KnowledgeChunk.__table__).compile(dialect=sqlite.dialect())
    )
    assert "embedding JSON" in ddl


def test_provider_errors_never_fallback_to_legacy_hash(monkeypatch) -> None:
    class BrokenEmbeddings(Embeddings):
        def embed_documents(self, texts):
            raise OSError("provider unavailable")

        def embed_query(self, text):
            raise OSError("provider unavailable")

    monkeypatch.setattr(
        embedding_service,
        "get_embeddings",
        lambda **_: (BrokenEmbeddings(), "multilingual-test-model"),
    )
    with pytest.raises(EmbeddingProviderError, match="failed"):
        embed_documents(["multilingual query"])
