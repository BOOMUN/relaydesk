"""Dialect-aware vector type used by the knowledge index.

The application still supports SQLite for the deterministic test/demo mode, but
production knowledge indexes are stored in PostgreSQL's fixed-width ``vector``
type.  Keeping the type adapter in one place lets the ORM model describe the
same column on both databases without silently serialising PostgreSQL vectors as
JSON.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, TypeDecorator

from .config import settings

try:  # pgvector is optional for SQLite-only installations/tests.
    from pgvector.sqlalchemy import Vector as _PgVector
except ImportError:  # pragma: no cover - exercised only without the extra dep
    _PgVector = None  # type: ignore[assignment,misc]


class EmbeddingVector(TypeDecorator[list[float]]):
    """A fixed-dimension pgvector column with a JSON SQLite fallback.

    ``TypeDecorator`` delegates bind/result processing to pgvector on
    PostgreSQL, while SQLite receives the existing JSON representation.  The
    comparator is intentionally exposed so ``column.cosine_distance(vector)``
    compiles to PostgreSQL's ``<=>`` operator for ANN retrieval.
    """

    impl = JSON
    cache_ok = True

    # Vector.Comparator provides l2/cosine/inner-product operators.  It is safe
    # to attach it to the fallback type because those operators are only used
    # on PostgreSQL connections.
    if _PgVector is not None:
        comparator_factory = _PgVector.Comparator

    def __init__(self, dimensions: int | None = None) -> None:
        super().__init__()
        self.dimensions = int(dimensions or settings.embedding_dimensions)
        if self.dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            if _PgVector is None:
                raise RuntimeError(
                    "pgvector is required for PostgreSQL; install the pgvector package"
                )
            return dialect.type_descriptor(_PgVector(self.dimensions))
        return dialect.type_descriptor(JSON())

    def copy(self, **_: Any) -> "EmbeddingVector":
        return type(self)(self.dimensions)

    def __repr__(self) -> str:
        return f"EmbeddingVector({self.dimensions})"


def embedding_dimension() -> int:
    """Return the configured fixed dimension used by storage and validation."""

    return int(settings.embedding_dimensions)


def validate_vector(vector: Any, *, dimensions: int | None = None) -> list[float]:
    """Validate and normalise an embedding before it is persisted."""

    expected = int(dimensions or settings.embedding_dimensions)
    if not isinstance(vector, (list, tuple)):
        # pgvector may return a small array-like object; converting it here
        # keeps model instances JSON/test friendly.
        try:
            vector = list(vector)
        except TypeError as exc:
            raise ValueError("embedding must be a finite numeric sequence") from exc
    if len(vector) != expected:
        raise ValueError(
            f"embedding dimension mismatch: expected {expected}, got {len(vector)}"
        )
    values: list[float] = []
    for value in vector:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("embedding contains a non-finite value")
        values.append(number)
    return values


__all__ = ["EmbeddingVector", "embedding_dimension", "validate_vector"]
