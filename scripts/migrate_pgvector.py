r"""Create/migrate an AgentDesk PostgreSQL database with pgvector.

The migration deliberately treats ``knowledge_chunks`` as derived data:
structured products, offers, prices, stock and destinations are copied first,
then every chunk is regenerated with the one configured multilingual model.
This avoids carrying ``local-hash-v1`` rows into a PostgreSQL vector index.

Typical local cutover:

    $env:AGENTDESK_EMBEDDING_PROVIDER = "fastembed"
    .\.venv\Scripts\python scripts\migrate_pgvector.py `
      --database-url postgresql+psycopg://agentdesk:change-me@127.0.0.1:55432/agentdesk `
      --source-database-url sqlite:///./data/agentdesk.db
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import settings  # noqa: E402
from backend.app.database import Base, create_database_engine  # noqa: E402
from backend.app.models import KnowledgeChunk, KnowledgeDocument, Product  # noqa: E402,F401
from backend.app.services.embeddings import configured_embedding_model  # noqa: E402
from backend.app.services.knowledge_ingestion import rebuild_documents_chunks  # noqa: E402
from backend.app.vector_types import validate_vector  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--source-database-url",
        help="Optional existing SQLite/PostgreSQL URL whose structured data is copied.",
    )
    parser.add_argument("--tenant-id", type=int, action="append")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Truncate existing target tables before copying source data.",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Install/verify schema without copying or rebuilding chunks.",
    )
    parser.add_argument("--json", action="store_true", dest="json_only")
    return parser.parse_args()


def _engine(url: str) -> Engine:
    return create_database_engine(url)


def _require_postgres(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        raise SystemExit("--database-url must use PostgreSQL (postgresql+psycopg://...)")


def _ensure_extension(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


def _vector_column_signature(engine: Engine) -> str | None:
    with engine.connect() as connection:
        exists = connection.scalar(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'knowledge_chunks'"
            )
        )
        if not exists:
            return None
        return connection.scalar(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'knowledge_chunks' "
                "AND a.attname = 'embedding' AND a.attnum > 0 AND NOT a.attisdropped"
            )
        )


def _drop_chunk_indexes(engine: Engine) -> None:
    # Index names are stable in the ORM model.  Dropping them before renaming a
    # legacy table prevents schema-global name collisions when the new HNSW
    # index is created.
    names = {
        index.name
        for index in KnowledgeChunk.__table__.indexes
        if index.name
    }
    with engine.begin() as connection:
        for name in sorted(names):
            connection.execute(text(f'DROP INDEX IF EXISTS "{name}"'))


def _ensure_chunk_filter_index(engine: Engine) -> None:
    """Add the composite tenant/model filter index to existing schemas."""

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_knowledge_chunk_tenant_model_document "
                "ON knowledge_chunks (tenant_id, embedding_model, document_id)"
            )
        )


def _prepare_schema(engine: Engine) -> str | None:
    """Install the extension and ensure ``embedding vector(n)`` exists.

    If an old JSON or differently-sized vector column is present, it is kept
    under a timestamped legacy table until the new index has been rebuilt.
    """

    _ensure_extension(engine)
    signature = _vector_column_signature(engine)
    expected = f"vector({settings.embedding_dimensions})"
    legacy_table: str | None = None
    if signature is not None and signature.casefold() != expected.casefold():
        _drop_chunk_indexes(engine)
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        legacy_table = f"knowledge_chunks_legacy_{suffix}_{secrets.token_hex(2)}"
        legacy_table = re.sub(r"[^a-zA-Z0-9_]", "", legacy_table)[:63]
        with engine.begin() as connection:
            connection.execute(
                text(f'ALTER TABLE "knowledge_chunks" RENAME TO "{legacy_table}"')
            )
    Base.metadata.create_all(engine)
    _ensure_chunk_filter_index(engine)
    actual = _vector_column_signature(engine)
    if actual is None or actual.casefold() != expected.casefold():
        raise RuntimeError(
            f"knowledge_chunks.embedding is {actual!r}; expected {expected!r}"
        )
    return legacy_table
def _target_has_rows(engine: Engine) -> bool:
    with Session(engine) as db:
        for table in Base.metadata.sorted_tables:
            # knowledge_chunks may be an intentionally empty derived table;
            # every other populated table means a source copy needs --replace.
            if table.name == KnowledgeChunk.__tablename__:
                continue
            if db.scalar(select(func.count()).select_from(table)):
                return True
        return False


def _truncate_target(engine: Engine) -> None:
    table_names = [table.name for table in reversed(Base.metadata.sorted_tables)]
    with engine.begin() as connection:
        for name in table_names:
            connection.execute(
                text(f'TRUNCATE TABLE "{name}" RESTART IDENTITY CASCADE')
            )


def _copy_structured_data(source: Engine, target: Engine) -> dict[str, int]:
    """Copy all ORM tables except derived knowledge chunks.

    The source is reflected so the command can migrate the existing SQLite
    database without converting price/stock/country fields into text blobs.
    Inserts follow SQLAlchemy's foreign-key order and preserve primary keys.
    """

    source_metadata = MetaData()
    source_metadata.reflect(bind=source)
    source_rows: dict[str, list[dict[str, Any]]] = {}
    with source.connect() as source_connection:
        for target_table in Base.metadata.sorted_tables:
            source_table = source_metadata.tables.get(target_table.name)
            if source_table is None or target_table.name == KnowledgeChunk.__tablename__:
                continue
            source_rows[target_table.name] = [
                dict(row)
                for row in source_connection.execute(select(source_table)).mappings().all()
            ]

    # Build source primary-key sets up front.  The live SQLite database may
    # contain historical delivery-attempt rows whose message was purged; such
    # orphan rows are not valid in PostgreSQL and are reported/skipped instead
    # of aborting the whole structured-data migration.
    primary_keys: dict[tuple[str, str], set[Any]] = {}
    for target_table in Base.metadata.sorted_tables:
        for column in target_table.primary_key.columns:
            values = {
                row.get(column.name)
                for row in source_rows.get(target_table.name, [])
                if row.get(column.name) is not None
            }
            primary_keys[(target_table.name, column.name)] = values
    copied: dict[str, int] = {}
    skipped = 0
    with target.begin() as target_connection:
        for target_table in Base.metadata.sorted_tables:
            name = target_table.name
            if name == KnowledgeChunk.__tablename__:
                continue
            rows = source_rows.get(name)
            if rows is None:
                continue
            if not rows:
                copied[name] = 0
                continue
            target_columns = {column.name for column in target_table.columns}
            payload = []
            for row in rows:
                invalid_reference = False
                for foreign_key in target_table.foreign_keys:
                    value = row.get(foreign_key.parent.name)
                    if value is None:
                        continue
                    referred_key = (
                        foreign_key.column.table.name,
                        foreign_key.column.name,
                    )
                    if value not in primary_keys.get(referred_key, set()):
                        invalid_reference = True
                        break
                if invalid_reference:
                    skipped += 1
                    continue
                payload.append(
                    {
                        key: value
                        for key, value in row.items()
                        if key in target_columns
                    }
                )
            if not payload:
                copied[name] = 0
                continue
            for start in range(0, len(payload), 500):
                target_connection.execute(target_table.insert(), payload[start : start + 500])
            copied[name] = len(payload)

        # Explicitly inserted integer primary keys do not advance PostgreSQL
        # sequences; reset them so subsequent writes cannot collide.
        for target_table in Base.metadata.sorted_tables:
            id_column = target_table.c.get("id")
            if id_column is None:
                continue
            sequence = target_connection.scalar(
                text("SELECT pg_get_serial_sequence(:qualified, 'id')"),
                {"qualified": f"public.{target_table.name}"},
            )
            if not sequence:
                continue
            maximum = target_connection.scalar(
                text(f'SELECT MAX("id") FROM "{target_table.name}"')
            )
            if maximum is None:
                continue
            target_connection.execute(
                text("SELECT setval(:sequence, :value, true)"),
                {"sequence": sequence, "value": int(maximum)},
            )
    if skipped:
        copied["_skipped_orphan_rows"] = skipped
    return copied


def _rebuild_target(
    engine: Engine,
    tenant_ids: set[int] | None,
) -> dict[str, Any]:
    model_name = configured_embedding_model()
    if model_name == "local-hash-v1":
        raise RuntimeError(
            "PostgreSQL migration requires a multilingual provider; "
            "set AGENTDESK_EMBEDDING_PROVIDER=fastembed"
        )
    with Session(engine) as db:
        statement = select(KnowledgeDocument).order_by(KnowledgeDocument.id)
        if tenant_ids:
            statement = statement.where(KnowledgeDocument.tenant_id.in_(tenant_ids))
        documents = list(db.scalars(statement).all())
        chunks = rebuild_documents_chunks(
            db,
            documents,
            model_name=model_name,
            batch_size=settings.embedding_batch_size,
        )
        db.flush()
        db.commit()

        model_statement = select(KnowledgeChunk.embedding_model).distinct()
        if tenant_ids:
            model_statement = model_statement.where(
                KnowledgeChunk.tenant_id.in_(tenant_ids)
            )
        models = set(db.scalars(model_statement).all())
        if models != {model_name}:
            raise RuntimeError(f"Mixed embedding models remain: {sorted(models)}")
        checked = 0
        vector_statement = select(KnowledgeChunk.embedding)
        if tenant_ids:
            vector_statement = vector_statement.where(
                KnowledgeChunk.tenant_id.in_(tenant_ids)
            )
        for vector in db.scalars(vector_statement).yield_per(512):
            validate_vector(vector, dimensions=settings.embedding_dimensions)
            checked += 1
        product_count = db.scalar(select(func.count()).select_from(Product)) or 0
        offer_count = db.scalar(
            text("SELECT count(*) FROM product_price_offers")
        ) or 0
    # Refresh planner statistics before the candidate benchmark/traffic
    # review; the HNSW index itself is created by Base.metadata.create_all.
    with engine.begin() as connection:
        connection.execute(text("ANALYZE knowledge_chunks"))
    return {
        "model": model_name,
        "dimensions": settings.embedding_dimensions,
        "documents": len(documents),
        "chunks": chunks,
        "validated_vectors": checked,
        "products": int(product_count),
        "offers": int(offer_count),
    }


def main() -> int:
    args = _arguments()
    if args.tenant_id and any(item <= 0 for item in args.tenant_id):
        raise SystemExit("--tenant-id values must be positive")
    if args.source_database_url and args.source_database_url == args.database_url:
        raise SystemExit("source and target database URLs must differ")

    target = _engine(args.database_url)
    _require_postgres(target)
    legacy_table = _prepare_schema(target)
    copied: dict[str, int] = {}
    try:
        if args.source_database_url:
            if _target_has_rows(target) and not args.replace:
                raise SystemExit(
                    "Target already contains data; pass --replace to copy the source safely."
                )
            if args.replace:
                _truncate_target(target)
            source = _engine(args.source_database_url)
            try:
                copied = _copy_structured_data(source, target)
            finally:
                source.dispose()
        if args.schema_only:
            result: dict[str, Any] = {
                "schema": "ok",
                "embedding_type": _vector_column_signature(target),
                "legacy_table": legacy_table,
                "copied": copied,
            }
        else:
            result = _rebuild_target(
                target,
                set(args.tenant_id) if args.tenant_id else None,
            )
            result.update(
                {
                    "schema": "ok",
                    "embedding_type": _vector_column_signature(target),
                    "legacy_table": legacy_table,
                    "copied": copied,
                }
            )
            if legacy_table:
                with target.begin() as connection:
                    connection.execute(text(f'DROP TABLE "{legacy_table}"'))
                result["legacy_table"] = None
    except Exception:
        # Keep the legacy table if rebuilding failed; this gives an operator a
        # recoverable source for investigation instead of silently discarding
        # the old index.
        raise
    finally:
        target.dispose()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
