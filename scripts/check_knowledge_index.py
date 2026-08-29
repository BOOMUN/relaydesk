"""Read-only health check for the knowledge/vector index."""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import settings  # noqa: E402
from backend.app.database import create_database_engine  # noqa: E402
from backend.app.models import (  # noqa: E402
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSyncRun,
    Product,
    ProductPriceOffer,
)


def _engine(url: str):
    return create_database_engine(url)


def _safe_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return url


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only health check for the knowledge/vector index."
    )
    parser.add_argument(
        "database_url_positional",
        nargs="?",
        help="SQLAlchemy database URL (legacy positional form).",
    )
    parser.add_argument(
        "--database-url",
        dest="database_url",
        help="SQLAlchemy database URL.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Keep JSON output (accepted for scripting compatibility).",
    )
    args = parser.parse_args()
    url = args.database_url or args.database_url_positional or settings.database_url
    engine = _engine(url)
    result: dict[str, Any] = {
        "database": _safe_url(url),
        "dialect": engine.dialect.name,
        "expected_model": settings.configured_embedding_model,
        "expected_dimensions": settings.embedding_dimensions,
    }
    with Session(engine) as db:
        result["documents"] = int(
            db.scalar(select(func.count()).select_from(KnowledgeDocument)) or 0
        )
        result["chunks"] = int(
            db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0
        )
        models = sorted(
            str(item)
            for item in db.scalars(select(KnowledgeChunk.embedding_model).distinct()).all()
            if item
        )
        result["models"] = models
        dimension_counts: dict[str, int] = {}
        for vector in db.scalars(select(KnowledgeChunk.embedding)).yield_per(512):
            key = str(len(vector)) if vector is not None else "null"
            dimension_counts[key] = dimension_counts.get(key, 0) + 1
        result["dimension_counts"] = dimension_counts
        result["products"] = int(db.scalar(select(func.count()).select_from(Product)) or 0)
        result["offers"] = int(
            db.scalar(select(func.count()).select_from(ProductPriceOffer)) or 0
        )
        latest = db.scalar(
            select(KnowledgeSyncRun.completed_at)
            .where(KnowledgeSyncRun.status == "completed")
            .order_by(KnowledgeSyncRun.completed_at.desc())
            .limit(1)
        )
        result["latest_successful_sync"] = latest.isoformat() if latest else None
        result["failed_sync_runs"] = int(
            db.scalar(
                select(func.count())
                .select_from(KnowledgeSyncRun)
                .where(KnowledgeSyncRun.status == "failed")
            )
            or 0
        )
    schema_type = None
    if engine.dialect.name == "postgresql":
        for column in inspect(engine).get_columns("knowledge_chunks"):
            if column["name"] == "embedding":
                schema_type = str(column["type"])
                break
        with engine.connect() as connection:
            result["pgvector_version"] = connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
    result["embedding_column"] = schema_type
    expected_type = (
        f"VECTOR({settings.embedding_dimensions})"
        if engine.dialect.name == "postgresql"
        else "JSON"
    )
    result["schema_ok"] = (
        schema_type.casefold() == expected_type.casefold()
        if schema_type is not None
        else engine.dialect.name != "postgresql"
    )
    result["model_set_ok"] = not result["models"] or result["models"] == [
        settings.configured_embedding_model
    ]
    result["dimensions_ok"] = not result["dimension_counts"] or set(
        result["dimension_counts"]
    ) == {str(settings.embedding_dimensions)}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    engine.dispose()
    return 0 if result["schema_ok"] and result["model_set_ok"] and result["dimensions_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
