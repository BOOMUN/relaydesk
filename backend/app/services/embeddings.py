from __future__ import annotations

import hashlib
import math
import re
import time
from functools import lru_cache
from typing import Any

from langchain_core.embeddings import Embeddings

from ..config import settings
from ..vector_types import validate_vector


LOCAL_EMBEDDING_MODEL = "local-hash-v1"
DEFAULT_MULTILINGUAL_EMBEDDING_MODEL = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)


class EmbeddingProviderError(RuntimeError):
    """Raised when the configured embedding provider cannot produce vectors."""


class LocalHashEmbeddings(Embeddings):
    """Small deterministic embedding kept for isolated SQLite regression tests.

    This provider is deliberately never used as an implicit fallback for a
    configured multilingual/OpenAI provider.  A failed production embedding
    call must fail loudly rather than create a mixed-model index.
    """

    dimensions = 384

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        normalized = text.lower().strip()
        latin_tokens = re.findall(r"[a-z0-9_-]+", normalized)
        cjk_chars = re.findall(r"[\u3400-\u9fff]", normalized)
        cjk_bigrams = ["".join(cjk_chars[index : index + 2]) for index in range(len(cjk_chars) - 1)]
        for token in [*latin_tokens, *cjk_chars, *cjk_bigrams]:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


@lru_cache(maxsize=4)
def _fastembed_model(model_name: str, threads: int | None = None) -> Any:
    """Load one FastEmbed model per process and reuse it across requests."""

    try:
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise EmbeddingProviderError(
            "FastEmbed is not installed; run pip install -r requirements.txt"
        ) from exc
    try:
        model = TextEmbedding(
            model_name=model_name,
            threads=threads if threads is not None else settings.embedding_threads,
        )
    except Exception as exc:  # pragma: no cover - depends on model/cache/network
        raise EmbeddingProviderError(
            f"Unable to load multilingual embedding model {model_name!r}: {exc}"
        ) from exc
    expected = settings.embedding_dimensions
    actual = int(getattr(model, "embedding_size", expected))
    if actual != expected:
        raise EmbeddingProviderError(
            f"Embedding model {model_name!r} has dimension {actual}, "
            f"but AGENTDESK_EMBEDDING_DIMENSIONS={expected}"
        )
    return model


class FastEmbedEmbeddings(Embeddings):
    """Multilingual sentence-transformer embeddings backed by FastEmbed."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.embedding_model
        self.dimensions = int(settings.embedding_dimensions)

    @property
    def model(self) -> Any:
        return _fastembed_model(self.model_name, settings.embedding_threads)

    @staticmethod
    def _as_vectors(values: Any) -> list[list[float]]:
        vectors: list[list[float]] = []
        for value in values:
            # numpy arrays expose ``tolist``; plain iterables are accepted too.
            raw = value.tolist() if hasattr(value, "tolist") else list(value)
            vectors.append(validate_vector(raw))
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return self._as_vectors(
                self.model.embed(texts, batch_size=settings.embedding_batch_size)
            )
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            raise EmbeddingProviderError(
                f"FastEmbed document embedding failed for {self.model_name!r}: {exc}"
            ) from exc

    def embed_query(self, text: str) -> list[float]:
        try:
            vectors = self._as_vectors(self.model.query_embed([text]))
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            raise EmbeddingProviderError(
                f"FastEmbed query embedding failed for {self.model_name!r}: {exc}"
            ) from exc
        if not vectors:
            raise EmbeddingProviderError("FastEmbed returned no query vector")
        return vectors[0]


def configured_embedding_model() -> str:
    """Return the one model allowed for newly indexed/query vectors."""

    return settings.configured_embedding_model


def get_embeddings(*, model_name: str | None = None) -> tuple[Embeddings, str]:
    """Resolve an embedding implementation without silent provider fallback."""

    if model_name == LOCAL_EMBEDDING_MODEL:
        return LocalHashEmbeddings(), LOCAL_EMBEDDING_MODEL

    if model_name is None:
        provider = settings.embedding_provider
        if provider == "local_hash":
            return LocalHashEmbeddings(), LOCAL_EMBEDDING_MODEL
        if provider == "openai":
            model_name = settings.configured_embedding_model
        else:
            model_name = settings.embedding_model

    if model_name == settings.embedding_model and settings.embedding_provider == "fastembed":
        return FastEmbedEmbeddings(model_name), model_name

    # Explicit OpenAI model names remain supported for a deliberate OpenAI
    # configuration, but are never selected when the provider is fastembed.
    if (
        settings.embedding_provider == "openai"
        and settings.openai_embeddings_enabled
        and model_name == settings.openai_embedding_model
    ):
        from langchain_openai import OpenAIEmbeddings

        return (
            OpenAIEmbeddings(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.openai_embedding_model,
            ),
            settings.openai_embedding_model,
        )

    if settings.embedding_provider == "openai" and model_name == settings.configured_embedding_model:
        if not settings.openai_embeddings_enabled:
            raise EmbeddingProviderError(
                "OpenAI embeddings are selected but API key/model are not configured"
            )
        from langchain_openai import OpenAIEmbeddings

        return (
            OpenAIEmbeddings(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.openai_embedding_model,
            ),
            settings.openai_embedding_model,
        )

    raise EmbeddingProviderError(
        f"Unknown or disabled embedding model {model_name!r}; "
        "configure AGENTDESK_EMBEDDING_PROVIDER and model explicitly"
    )


@lru_cache(maxsize=8)
def _warmup_cached(
    provider: str,
    model_name: str,
    dimensions: int,
    batch_size: int,
    threads: int,
) -> dict[str, Any]:
    """Load the embedding runtime and exercise document/query code paths once."""

    started = time.perf_counter()
    embeddings, resolved_name = get_embeddings(model_name=model_name)
    # Two short samples force tokenizer/runtime initialization while keeping
    # startup cost bounded.  The configured batch size is used so the warmup
    # follows the same execution path as a real ingestion batch.
    samples = [
        "AgentDesk multilingual retrieval warmup",
        "多語言知識庫向量預熱",
    ]
    vectors = embeddings.embed_documents(samples)
    query_vector = embeddings.embed_query("knowledge base warmup")
    for vector in [*vectors, query_vector]:
        validate_vector(vector, dimensions=dimensions)
    return {
        "provider": provider,
        "model": resolved_name,
        "dimensions": dimensions,
        "batch_size": batch_size,
        "threads": threads,
        "samples": len(samples),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def warmup_embeddings() -> dict[str, Any]:
    """Preload the configured embedding model before serving customers.

    FastEmbed and the explicit local test provider are safe to warm locally.
    OpenAI embeddings are remote calls, so application startup leaves those
    to the first controlled ingestion/query instead of making startup depend
    on external API availability.
    """

    if not settings.embedding_warmup_enabled:
        return {"status": "disabled"}
    if settings.embedding_provider == "openai":
        return {
            "status": "skipped_remote_provider",
            "provider": settings.embedding_provider,
            "model": settings.configured_embedding_model,
        }
    result = _warmup_cached(
        settings.embedding_provider,
        settings.configured_embedding_model,
        int(settings.embedding_dimensions),
        int(settings.embedding_batch_size),
        int(settings.embedding_threads),
    )
    return {"status": "ready", **result}


def embed_documents(
    texts: list[str],
    *,
    prefer_local: bool = False,
    model_name: str | None = None,
    batch_size: int | None = None,
) -> tuple[list[list[float]], str]:
    """Embed documents and validate every vector's fixed dimension.

    ``prefer_local`` is retained as an explicit compatibility switch for the
    SQLite regression suite.  It is never used by production ingestion paths.
    """

    selected_model = LOCAL_EMBEDDING_MODEL if prefer_local else model_name
    embeddings, resolved_name = get_embeddings(model_name=selected_model)
    size = int(batch_size or settings.embedding_batch_size)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), size):
        try:
            batch = embeddings.embed_documents(texts[start : start + size])
        except Exception as exc:
            if isinstance(exc, EmbeddingProviderError):
                raise
            raise EmbeddingProviderError(
                f"Embedding provider {resolved_name!r} failed: {exc}"
            ) from exc
        vectors.extend(validate_vector(vector) for vector in batch)
    if len(vectors) != len(texts):
        raise EmbeddingProviderError(
            f"Embedding provider {resolved_name!r} returned {len(vectors)} vectors "
            f"for {len(texts)} texts"
        )
    return vectors, resolved_name


def embed_query(query: str, model_name: str, *, strict: bool = False) -> list[float] | None:
    """Embed a query using the same model as its candidate chunks.

    Unknown legacy/custom model labels return ``None`` in normal retrieval so
    callers can fail closed.  Migration/health checks can pass ``strict=True``
    to receive the actionable provider error.
    """

    if model_name == LOCAL_EMBEDDING_MODEL:
        embeddings: Embeddings = LocalHashEmbeddings()
    else:
        try:
            embeddings, resolved_name = get_embeddings(model_name=model_name)
        except Exception:
            if strict:
                raise
            return None
        if resolved_name != model_name:
            if strict:
                raise EmbeddingProviderError(
                    f"Resolved query model {resolved_name!r} does not match {model_name!r}"
                )
            return None
    try:
        return validate_vector(embeddings.embed_query(query))
    except Exception:
        if strict:
            raise
        return None


__all__ = [
    "DEFAULT_MULTILINGUAL_EMBEDDING_MODEL",
    "EmbeddingProviderError",
    "FastEmbedEmbeddings",
    "LOCAL_EMBEDDING_MODEL",
    "LocalHashEmbeddings",
    "configured_embedding_model",
    "embed_documents",
    "embed_query",
    "get_embeddings",
    "warmup_embeddings",
]
