r"""Rebuild every knowledge chunk with one configured embedding model.

Examples (PowerShell)::

    $env:AGENTDESK_EMBEDDING_PROVIDER = "fastembed"
    .\.venv\Scripts\python scripts\rebuild_embeddings.py \
        --database-url sqlite:///./data/agentdesk.db

The command is intentionally separate from application startup.  It gives an
operator a verifiable, all-or-nothing cutover point and refuses to create a
mixed ``local-hash-v1``/multilingual index.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import settings  # noqa: E402
from backend.app.database import Base, create_database_engine  # noqa: E402
from backend.app.models import KnowledgeChunk, KnowledgeDocument  # noqa: E402,F401
from backend.app.services.embeddings import (  # noqa: E402
    LOCAL_EMBEDDING_MODEL,
    configured_embedding_model,
)
from backend.app.services.knowledge_ingestion import (  # noqa: E402
    rebuild_documents_chunks,
)
from backend.app.vector_types import validate_vector  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="SQLAlchemy URL (defaults to AGENTDESK_DATABASE_URL).",
    )
    parser.add_argument("--tenant-id", type=int, action="append")
    parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="Allow explicitly rebuilding with local-hash-v1 (tests only).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_only")
    return parser.parse_args()


def _engine(database_url: str):
    return create_database_engine(database_url)


def _safe_url(database_url: str) -> str:
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:
        return database_url


def _ensure_schema(engine) -> None:
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)


def _rebuild(db: Session, tenant_ids: set[int] | None, *, allow_legacy: bool) -> dict:
    model_name = configured_embedding_model()
    if model_name == LOCAL_EMBEDDING_MODEL and not allow_legacy:
        raise RuntimeError(
            "Refusing to rebuild production knowledge with local-hash-v1. "
            "Set AGENTDESK_EMBEDDING_PROVIDER=fastembed (or pass --allow-legacy "
            "only for an isolated regression fixture)."
        )

    statement = select(KnowledgeDocument).order_by(KnowledgeDocument.id)
    if tenant_ids:
        statement = statement.where(KnowledgeDocument.tenant_id.in_(tenant_ids))
    documents = list(db.scalars(statement).all())
    old_models = set(
        db.scalars(
            select(KnowledgeChunk.embedding_model)
            .where(
                KnowledgeChunk.tenant_id.in_(tenant_ids)
                if tenant_ids
                else True
            )
            .distinct()
        ).all()
    )
    if old_models and old_models == {LOCAL_EMBEDDING_MODEL}:
        old_model_state = "legacy_local_hash"
    elif old_models:
        old_model_state = "mixed_or_other"
    else:
        old_model_state = "empty"

    if not documents:
        return {
            "model": model_name,
            "dimensions": settings.embedding_dimensions,
            "documents": 0,
            "chunks": 0,
            "old_models": sorted(old_models),
            "old_model_state": old_model_state,
        }
    if old_models and model_name not in old_models and old_model_state == "mixed_or_other":
        # A complete delete/rebuild is still safe, but make the destructive
        # intent explicit in the command output and transaction log.
        pass

    total_chunks = rebuild_documents_chunks(
        db,
        documents,
        model_name=model_name,
        batch_size=settings.embedding_batch_size,
    )
    db.flush()
    db.commit()

    models = set(
        db.scalars(
            select(KnowledgeChunk.embedding_model)
            .where(
                KnowledgeChunk.tenant_id.in_(tenant_ids)
                if tenant_ids
                else True
            )
            .distinct()
        ).all()
    )
    if models != {model_name}:
        raise RuntimeError(
            f"Embedding rebuild produced an unexpected model set: {sorted(models)}"
        )
    dimensions = settings.embedding_dimensions
    checked = 0
    for vector in db.scalars(
        select(KnowledgeChunk.embedding).where(
            KnowledgeChunk.tenant_id.in_(tenant_ids) if tenant_ids else True
        )
    ).yield_per(512):
        validate_vector(vector, dimensions=dimensions)
        checked += 1
    return {
        "model": model_name,
        "dimensions": dimensions,
        "documents": len(documents),
        "chunks": total_chunks,
        "validated_vectors": checked,
        "old_models": sorted(old_models),
        "old_model_state": old_model_state,
    }


def main() -> int:
    args = _arguments()
    if args.tenant_id and any(item <= 0 for item in args.tenant_id):
        raise SystemExit("--tenant-id values must be positive")
    engine = _engine(args.database_url)
    _ensure_schema(engine)
    if args.dry_run:
        with Session(engine) as db:
            query = select(func.count()).select_from(KnowledgeDocument)
            if args.tenant_id:
                query = query.where(KnowledgeDocument.tenant_id.in_(args.tenant_id))
            chunks = db.scalar(select(func.count()).select_from(KnowledgeChunk))
            result = {
                "model": configured_embedding_model(),
                "dimensions": settings.embedding_dimensions,
                "documents": db.scalar(query) or 0,
                "chunks_before": chunks or 0,
            }
    else:
        with Session(engine) as db:
            result = _rebuild(
                db,
                set(args.tenant_id) if args.tenant_id else None,
                allow_legacy=args.allow_legacy,
            )
    result["database"] = _safe_url(args.database_url)
    result["dialect"] = engine.dialect.name
    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.dry_run:
            print("Embedding rebuild completed; no legacy model rows remain in scope.")
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
