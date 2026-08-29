r"""Create an isolated, marked knowledge-index scale fixture.

The recommended ``noise`` mode preserves the original labelled documents and
adds unrelated, deterministic chunks.  This exercises the HNSW candidate pool
and connection/predicate overhead without copying the same answer dozens of
times (which would distort Top-1/Top-3 quality).  ``duplicate`` is retained for
backwards-compatible index stress tests.  Neither mode touches the structured
product tables.  Every generated row carries an explicit marker so it can be
removed safely with ``--cleanup-marker``.

Examples (PowerShell)::

    .\.venv\Scripts\python scripts/scale_knowledge_index.py `
      --database-url postgresql+psycopg://.../agentdesk_scale `
      --factor 50

    .\.venv\Scripts\python scripts/scale_knowledge_index.py `
      --database-url postgresql+psycopg://.../agentdesk_scale `
      --cleanup-marker scale-20260826-ab12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import create_database_engine  # noqa: E402
from backend.app.models import KnowledgeChunk, KnowledgeDocument  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--mode",
        choices=("noise", "duplicate"),
        default="noise",
        help=(
            "Scale strategy: unrelated deterministic rows (recommended) or "
            "verbatim document replicas (default: noise)."
        ),
    )
    parser.add_argument(
        "--factor",
        type=int,
        default=50,
        help="Total multiplier including the original rows (default: 50).",
    )
    parser.add_argument(
        "--marker",
        help="Marker for generated rows (default: scale-<UTC timestamp>-<random>).",
    )
    parser.add_argument(
        "--cleanup-marker",
        help="Delete only generated documents whose source contains this exact marker.",
    )
    parser.add_argument("--json", action="store_true", dest="json_only")
    return parser.parse_args()


def _new_marker() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"scale-{stamp}-{secrets.token_hex(2)}"


def _safe_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<redacted>"


def _noise_vector(seed: int, dimensions: int) -> list[float]:
    """Generate a deterministic unit vector without invoking an embedder."""

    # Mix the ordinal through a digest so nearby rows do not receive nearby
    # pseudo-random streams.  This keeps both benchmark databases reproducible.
    digest = hashlib.blake2b(str(seed).encode("ascii"), digest_size=16).digest()
    generator = random.Random(int.from_bytes(digest, "big"))
    values = [generator.gauss(0.0, 1.0) for _ in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _cleanup(db: Session, marker: str) -> dict[str, int | str]:
    if not marker or len(marker) > 120 or any(char in marker for char in "%_\n\r"):
        raise ValueError("cleanup marker must be a plain, bounded string")
    pattern = f"%{marker}%"
    document_ids = list(
        db.scalars(
            select(KnowledgeDocument.id).where(KnowledgeDocument.source.like(pattern))
        ).all()
    )
    if document_ids:
        db.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(document_ids))
        )
        db.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids))
        )
    db.commit()
    return {"action": "cleanup", "marker": marker, "documents_removed": len(document_ids)}


def _scale(db: Session, factor: int, marker: str) -> dict[str, int | str]:
    if factor < 2 or factor > 1000:
        raise ValueError("factor must be between 2 and 1000")
    originals = list(
        db.scalars(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.is_active.is_(True))
            .order_by(KnowledgeDocument.id)
        ).all()
    )
    original_chunks: dict[int, list[KnowledgeChunk]] = {}
    for document in originals:
        original_chunks[document.id] = list(
            db.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document.id)
                .order_by(KnowledgeChunk.chunk_index)
            ).all()
        )
    if not originals:
        return {"action": "scale", "marker": marker, "documents_added": 0, "chunks_added": 0}

    added_documents = 0
    added_chunks = 0
    for replica in range(1, factor):
        clones: list[tuple[KnowledgeDocument, KnowledgeDocument]] = []
        for document in originals:
            clone = KnowledgeDocument(
                tenant_id=document.tenant_id,
                title=f"{document.title} [{marker}-{replica}]",
                content=document.content,
                source=f"{document.source}#{marker}-{replica}-{document.id}",
                category=document.category,
                is_active=True,
                created_at=document.created_at,
                updated_at=document.updated_at,
            )
            db.add(clone)
            clones.append((document, clone))
        db.flush()
        for original, clone in clones:
            for chunk in original_chunks[original.id]:
                db.add(
                    KnowledgeChunk(
                        tenant_id=clone.tenant_id,
                        document_id=clone.id,
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        content_hash=chunk.content_hash,
                        embedding=list(chunk.embedding),
                        embedding_model=chunk.embedding_model,
                        created_at=chunk.created_at,
                    )
                )
                added_chunks += 1
        db.flush()
        added_documents += len(clones)
    db.commit()
    # Refresh planner statistics so the pressure run observes the same
    # cardinality estimates a production bulk load would provide.
    db.execute(text("ANALYZE knowledge_chunks"))
    db.commit()
    return {
        "action": "scale",
        "marker": marker,
        "factor": factor,
        "source_documents": len(originals),
        "source_chunks": sum(len(items) for items in original_chunks.values()),
        "documents_added": added_documents,
        "chunks_added": added_chunks,
        "documents_total": int(
            db.scalar(select(func.count()).select_from(KnowledgeDocument)) or 0
        ),
        "chunks_total": int(db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0),
    }


def _scale_noise(db: Session, factor: int, marker: str) -> dict[str, int | str]:
    """Add unrelated chunks until the active corpus reaches ``factor`` size.

    One synthetic document per chunk keeps the target count exact.  A rotating
    destination label in each title means country hard-filter queries still
    exercise a realistic subset of the enlarged index; the body contains no
    product or answer terms.  The source model and vector dimension are copied
    from an existing chunk, so no mixed-model rows are introduced.
    """

    if factor < 2 or factor > 1000:
        raise ValueError("factor must be between 2 and 1000")
    # Treat previously generated benchmark-noise rows as fixtures, not as the
    # new corpus baseline.  Re-running the command therefore cannot scale
    # 50x noise into another 50x expansion.
    originals = list(
        db.scalars(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.is_active.is_(True))
            .where(KnowledgeDocument.category != "benchmark_noise")
            .order_by(KnowledgeDocument.id)
        ).all()
    )
    source_chunk = db.scalar(
        select(KnowledgeChunk)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeDocument.is_active.is_(True))
        .where(KnowledgeDocument.category != "benchmark_noise")
        .order_by(KnowledgeChunk.id)
    )
    if not originals or source_chunk is None:
        return {
            "action": "scale_noise",
            "marker": marker,
            "factor": factor,
            "documents_added": 0,
            "chunks_added": 0,
        }

    base_chunks = int(
        db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(KnowledgeDocument.is_active.is_(True))
            .where(KnowledgeDocument.category != "benchmark_noise")
        )
        or 0
    )
    existing_noise = int(
        db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(
                KnowledgeDocument.is_active.is_(True),
                KnowledgeDocument.category == "benchmark_noise",
            )
        )
        or 0
    )
    target_chunks = base_chunks * factor
    needed = max(0, target_chunks - base_chunks - existing_noise)
    if needed == 0:
        return {
            "action": "scale_noise",
            "marker": marker,
            "factor": factor,
            "source_chunks": base_chunks,
            "existing_noise_chunks": existing_noise,
            "documents_added": 0,
            "chunks_added": 0,
            "documents_total": int(
                db.scalar(select(func.count()).select_from(KnowledgeDocument)) or 0
            ),
            "chunks_total": base_chunks + existing_noise,
        }

    # Canonical Traditional labels used by the destination extractor.  They
    # are only in the title, so the rows remain semantically unrelated noise.
    destinations = (
        "日本",
        "韓國",
        "中國",
        "泰國",
        "台灣",
        "新加坡",
        "馬來西亞",
        "印尼",
        "菲律賓",
        "越南",
        "澳洲",
        "紐西蘭",
        "美國",
        "加拿大",
        "歐洲",
        "阿聯酋",
        "馬爾代夫",
        "關島",
        "香港",
        "澳門",
        "全球",
        "東南亞",
    )
    dimensions = len(source_chunk.embedding)
    model_name = source_chunk.embedding_model
    tenant_id = source_chunk.tenant_id
    added_documents = 0
    added_chunks = 0
    batch_size = 500
    for start in range(0, needed, batch_size):
        count = min(batch_size, needed - start)
        batch_documents: list[tuple[int, KnowledgeDocument, str]] = []
        for offset in range(count):
            ordinal = start + offset
            destination = destinations[ordinal % len(destinations)]
            content = (
                "Synthetic benchmark context unrelated to customer answers; "
                f"row {ordinal} marker {marker}."
            )
            document = KnowledgeDocument(
                tenant_id=tenant_id,
                title=f"{destination} benchmark noise {ordinal}",
                content=content,
                source=f"benchmark-noise://{marker}/{ordinal}",
                category="benchmark_noise",
                is_active=True,
            )
            db.add(document)
            batch_documents.append((ordinal, document, content))
        # Resolve all document ids in one flush, then insert the matching
        # chunks as one ORM batch instead of round-tripping once per row.
        db.flush()
        for ordinal, document, content in batch_documents:
            db.add(
                KnowledgeChunk(
                    tenant_id=tenant_id,
                    document_id=document.id,
                    chunk_index=0,
                    content=content,
                    content_hash=hashlib.sha256(
                        f"{marker}:{ordinal}".encode("utf-8")
                    ).hexdigest(),
                    embedding=_noise_vector(ordinal, dimensions),
                    embedding_model=model_name,
                )
            )
        added_documents += len(batch_documents)
        added_chunks += len(batch_documents)
        db.flush()
    db.commit()
    # Refresh planner statistics so the pressure run observes the same
    # cardinality estimates a production bulk load would provide.
    db.execute(text("ANALYZE knowledge_chunks"))
    db.commit()
    return {
        "action": "scale_noise",
        "marker": marker,
        "factor": factor,
        "source_chunks": base_chunks,
        "existing_noise_chunks": existing_noise,
        "target_chunks": target_chunks,
        "documents_added": added_documents,
        "chunks_added": added_chunks,
        "documents_total": int(
            db.scalar(select(func.count()).select_from(KnowledgeDocument)) or 0
        ),
        "chunks_total": int(db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0),
    }


def main() -> int:
    args = _arguments()
    if args.cleanup_marker and args.marker:
        raise SystemExit("use either --marker or --cleanup-marker, not both")
    marker = args.marker or _new_marker()
    engine = create_database_engine(args.database_url)
    try:
        with Session(engine) as db:
            result = (
                _cleanup(db, args.cleanup_marker)
                if args.cleanup_marker
                else (
                    _scale_noise(db, args.factor, marker)
                    if args.mode == "noise"
                    else _scale(db, args.factor, marker)
                )
            )
        result["database"] = _safe_url(args.database_url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
