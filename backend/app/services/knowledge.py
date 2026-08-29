from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache

from langchain_core.documents import Document
from opencc import OpenCC
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import configure_hnsw_session
from ..models import KnowledgeChunk, KnowledgeDocument, KnowledgeWebPage, Product
from .embeddings import (
    LOCAL_EMBEDDING_MODEL,
    LocalHashEmbeddings,
    configured_embedding_model,
    embed_query,
    get_embeddings,
)
from .knowledge_ingestion import rebuild_document_chunks
from .product_price_query import (
    is_product_recommendation_query,
    matching_product_catalog_ids,
)


_t2s = OpenCC("t2s.json")
_s2hk = OpenCC("s2hk.json")

_SEMANTIC_FUSION_WEIGHT = 0.45
_LEXICAL_FUSION_WEIGHT = 0.55
_DESTINATION_TITLE_BOOST = 0.18
_DESTINATION_CONTENT_BOOST = 0.04
_DIRECT_PRODUCT_CATALOG_BOOST = 0.55
_NAMED_PRODUCT_CATALOG_BOOST = 0.25
_MIN_LEXICAL_RELEVANCE = 0.08
_INFORMATIONAL_PRODUCT_TERMS = (
    "还是",
    "還是",
    "比较",
    "比較",
    "区别",
    "區別",
    "会不会",
    "會不會",
    "是否",
    "为什么",
    "為什麼",
    "在哪里",
    "在哪裡",
    "哪里领取",
    "哪裡領取",
    "取机",
    "取機",
    "领取",
    "領取",
    "归还",
    "歸還",
    "支持哪些",
    "支援哪些",
    "购买前",
    "購買前",
    "检查什么",
    "檢查什麼",
    "怎么安排",
    "怎麼安排",
    "如何安排",
    "怎么使用",
    "怎麼使用",
    "如何使用",
    "出发前",
    "出發前",
    "即日",
    "临时",
    "臨時",
    "到达后",
    "到達後",
    "到埗",
    "攻略",
    "指南",
    "fup",
    " vs ",
    " or ",
    "should i",
    "where ",
    "pick up",
    "pickup",
    " return",
    "before buying",
    "what should",
    "how to",
    "how should",
    "subject to",
    "cheaper than",
    "same day",
    "last minute",
    "walk-in",
    "after arrival",
)
_DIRECT_PRODUCT_TERMS = (
    "有吗",
    "有嗎",
    "有没有",
    "有沒有",
    "有冇",
    "提供",
    "出售",
    "购买",
    "購買",
    "想买",
    "想買",
    "我要买",
    "我要買",
    "想租",
    "我要租",
    "租借",
    "租用",
    "出租",
    "价格",
    "價格",
    "价钱",
    "價錢",
    "多少钱",
    "幾錢",
    "库存",
    "庫存",
    "缺货",
    "缺貨",
    "how much",
    "price",
    "pricing",
    "cost",
    "rate",
    "do you have",
    "do you rent",
    "rent ",
    "rental",
    "buy ",
    "purchase",
    "available",
    "availability",
    "stock",
    "sell",
)
_DESTINATION_QUERY_PATTERNS = (
    re.compile(
        r"(?:去|到(?!达|達|埗)|前往)\s*([\u3400-\u9fff]{2,20}?)(?="
        r"(?:哪(?:个)?好|哪(?:个)?|怎么选|如何选|上网|旅游|旅行|自由行|出差|$))"
    ),
    re.compile(
        r"(?:^|\s)([\u3400-\u9fff]{2,20}?)(?="
        r"(?:哪(?:个)?好|怎么选|如何选))"
    ),
)
_DESTINATION_EQUIVALENTS = {
    "南韩": ("南韩", "韩国"),
    "韩国": ("韩国", "南韩"),
    "澳大利亚": ("澳大利亚", "澳洲"),
    "澳洲": ("澳洲", "澳大利亚"),
    "中国大陆": ("中国大陆", "中国内地", "中国"),
    "中国内地": ("中国内地", "中国大陆", "中国"),
}


@dataclass
class _RetrievalCandidate:
    chunk: KnowledgeChunk
    document: KnowledgeDocument
    semantic_score: float
    lexical_score: float
    title_score: float
    destination_strength: int
    product_catalog_match: bool
    catalog_boost: float = 0.0
    semantic_normalized: float = 0.0
    retrieval_score: float = 0.0
    reranker_score: float = 0.0


def normalize_retrieval_text(value: str) -> str:
    """Return one comparison form for simplified/traditional customer text."""

    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = _t2s.convert(normalized).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _compact_retrieval_text(value: str) -> str:
    return re.sub(
        r"[^0-9a-z\u3400-\u9fff]+",
        "",
        normalize_retrieval_text(value),
    )


def _retrieval_tokens(value: str) -> list[str]:
    """Tokenize English and CJK text without requiring a separate dictionary."""

    normalized = normalize_retrieval_text(value)
    tokens: list[str] = []
    for matched in re.finditer(r"[a-z0-9]+|[\u3400-\u9fff]+", normalized):
        term = matched.group(0)
        if term.isascii():
            tokens.append(term)
            continue
        if len(term) <= 2:
            tokens.append(term)
            continue
        # Character n-grams work for simplified/traditional-normalized Chinese
        # titles while preserving exact English model names such as GoPro.
        tokens.extend(term[index : index + 2] for index in range(len(term) - 1))
        tokens.extend(term[index : index + 3] for index in range(len(term) - 2))
    return tokens


def _postgres_lexical_terms(value: str) -> list[str]:
    """Choose a small, high-signal ILIKE term set for the SQL supplement."""

    tokens = list(dict.fromkeys(_retrieval_tokens(value)))
    limit = max(2, int(settings.rag_lexical_term_limit))
    latin = [token for token in tokens if token.isascii()]
    cjk = [token for token in tokens if not token.isascii()]
    # Longer CJK n-grams are more selective; retain short country/product
    # aliases only when there is room after the high-signal terms.
    cjk.sort(key=lambda token: (-len(token), token))
    selected = latin[:limit]
    selected.extend(cjk[: max(0, limit - len(selected))])
    if len(selected) < limit:
        selected.extend(token for token in tokens if token not in selected)
    return list(dict.fromkeys(selected))[:limit]


def _normalized_bm25_scores(
    query_tokens: list[str],
    corpus: dict[int, list[str]],
) -> dict[int, float]:
    """Return self-contained BM25 scores normalized to the current recall set."""

    if not query_tokens or not corpus:
        return {item_id: 0.0 for item_id in corpus}
    document_count = len(corpus)
    average_length = sum(len(tokens) for tokens in corpus.values()) / document_count
    if average_length <= 0:
        return {item_id: 0.0 for item_id in corpus}

    document_frequency: Counter[str] = Counter()
    for tokens in corpus.values():
        document_frequency.update(set(tokens))

    query_terms = set(query_tokens)
    query_term_weights = {
        term: math.log(
            1
            + (document_count - document_frequency.get(term, 0) + 0.5)
            / (document_frequency.get(term, 0) + 0.5)
        )
        for term in query_terms
    }
    total_query_weight = sum(query_term_weights.values()) or 1.0
    k1 = 1.5
    b = 0.75
    raw_scores: dict[int, float] = {}
    query_coverages: dict[int, float] = {}
    for item_id, tokens in corpus.items():
        frequencies = Counter(tokens)
        length_normalizer = k1 * (1 - b + b * len(tokens) / average_length)
        score = 0.0
        matched_query_weight = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            inverse_document_frequency = query_term_weights[term]
            matched_query_weight += inverse_document_frequency
            score += inverse_document_frequency * (
                frequency * (k1 + 1) / (frequency + length_normalizer)
            )
        raw_scores[item_id] = score
        query_coverages[item_id] = matched_query_weight / total_query_weight

    maximum = max(raw_scores.values(), default=0.0)
    if maximum <= 0:
        return {item_id: 0.0 for item_id in corpus}
    return {
        item_id: (score / maximum) * math.sqrt(query_coverages[item_id])
        for item_id, score in raw_scores.items()
    }


def _lexical_relevance_scores(
    query: str,
    rows: list[tuple[KnowledgeChunk, KnowledgeDocument]],
) -> dict[int, tuple[float, float]]:
    """Fuse title BM25 and chunk BM25 before the semantic reranking stage."""

    query_tokens = _retrieval_tokens(query)
    documents = {document.id: document for _, document in rows}
    title_scores = _normalized_bm25_scores(
        query_tokens,
        {
            document_id: _retrieval_tokens(document.title)
            for document_id, document in documents.items()
        },
    )
    body_scores = _normalized_bm25_scores(
        query_tokens,
        {chunk.id: _retrieval_tokens(chunk.content) for chunk, _ in rows},
    )
    return {
        chunk.id: (
            0.72 * title_scores.get(document.id, 0.0)
            + 0.28 * body_scores.get(chunk.id, 0.0),
            title_scores.get(document.id, 0.0),
        )
        for chunk, document in rows
    }


def _pairwise_reranker_score(query: str, candidate: _RetrievalCandidate) -> float:
    """Score a query/document pair after recall using deterministic features.

    This lightweight cross-encoder substitute is deliberately explainable and
    works offline for the local demo database. It rewards exact phrases,
    title coverage, and high-signal query terms while retaining semantic
    similarity as a tie-breaker. A hosted cross-encoder can replace this
    function later without changing the retrieval contract.
    """

    query_normalized = normalize_retrieval_text(query)
    title = normalize_retrieval_text(candidate.document.title)
    body = normalize_retrieval_text(candidate.chunk.content)
    compact_query = _compact_retrieval_text(query_normalized)
    compact_title = _compact_retrieval_text(title)
    compact_body = _compact_retrieval_text(body)
    exact_phrase = 1.0 if compact_query and compact_query in compact_title else 0.0
    body_phrase = 0.7 if compact_query and compact_query in compact_body else 0.0
    query_tokens = set(_retrieval_tokens(query_normalized))
    title_tokens = set(_retrieval_tokens(title))
    body_tokens = set(_retrieval_tokens(body))
    if query_tokens:
        title_coverage = len(query_tokens & title_tokens) / len(query_tokens)
        body_coverage = len(query_tokens & body_tokens) / len(query_tokens)
    else:
        title_coverage = body_coverage = 0.0
    return (
        0.42 * exact_phrase
        + 0.12 * body_phrase
        + 0.28 * title_coverage
        + 0.10 * body_coverage
        + 0.08 * max(candidate.semantic_score, 0.0)
    )


def _rerank_candidates(
    query: str,
    candidates: list[_RetrievalCandidate],
) -> list[_RetrievalCandidate]:
    if not candidates or not settings.rag_reranker_enabled:
        return candidates
    bounded = candidates[: max(3, int(settings.rag_reranker_candidate_limit))]
    for candidate in bounded:
        pairwise_score = _pairwise_reranker_score(query, candidate)
        # The pairwise feature score separates obviously relevant pages, but
        # near ties must retain the normalized BM25/vector/metadata evidence
        # from recall.  Without this term, a broad index page that repeats
        # WiFi/eSIM/SIM can outrank a destination-specific comparison page by
        # a few thousandths even when the latter has the stronger fused score.
        fused_score = min(max(candidate.retrieval_score, 0.0), 1.0)
        candidate.reranker_score = 0.82 * pairwise_score + 0.18 * fused_score
    bounded.sort(
        key=lambda item: (
            item.reranker_score,
            item.retrieval_score,
            item.title_score,
            item.semantic_score,
        ),
        reverse=True,
    )
    return bounded


def _product_catalog_boost(query: str) -> float:
    """Boost catalogue pages only for direct stock, rental, or price requests."""

    normalized = f" {normalize_retrieval_text(query)} "
    # A destination recommendation such as “去日本租哪个比较好” is a
    # catalogue choice request even though it contains the informational word
    # “比较”.  Put authoritative product records first, then let the guide
    # pages supply the WiFi/eSIM suitability comparison.
    if is_product_recommendation_query(query):
        return _DIRECT_PRODUCT_CATALOG_BOOST
    if any(term in normalized for term in _INFORMATIONAL_PRODUCT_TERMS):
        return 0.0
    if any(term in normalized for term in _DIRECT_PRODUCT_TERMS):
        return _DIRECT_PRODUCT_CATALOG_BOOST
    # A named model can still match the structured catalogue even when the
    # customer omits conversational words such as “rent” or “price”.
    return _NAMED_PRODUCT_CATALOG_BOOST


def should_prioritize_product_catalog(query: str) -> bool:
    """Return whether structured product records should precede guide content."""

    return _product_catalog_boost(query) > 0


def _source_deduplication_key(document: KnowledgeDocument) -> tuple[str, str | int]:
    source = (document.source or "").strip().casefold().rstrip("/")
    if source and source != "manual":
        return "source", source
    return "document", document.id


def _destination_aliases(value: str) -> set[str]:
    normalized = normalize_retrieval_text(value)
    aliases: set[str] = set()
    for item in (normalized, *re.split(r"[-+&＆、,/]+", normalized)):
        compact = _compact_retrieval_text(item)
        compact = re.sub(
            r"(?:升级)?自动翻墙$|城市适用$|商业用$|(?:\d+国|多国)$",
            "",
            compact,
        )
        if len(compact) >= 2:
            aliases.add(compact)
    return aliases


def _expand_destination_terms(terms: set[str]) -> tuple[str, ...]:
    expanded = set(terms)
    for term in tuple(terms):
        expanded.update(_DESTINATION_EQUIVALENTS.get(term, ()))
    return tuple(sorted(expanded, key=lambda value: (-len(value), value)))


def _is_purchase_origin_context(normalized_query: str, alias: str) -> bool:
    """Do not treat Hong Kong as the travel destination in SIM buying FAQs."""

    if alias != "香港":
        return False
    compact = _compact_retrieval_text(normalized_query)
    asks_where_to_buy = any(
        term in compact
        for term in (
            "旅行电话卡",
            "旅游电话卡",
            "travelsimcard",
        )
    )
    compares_arrival = any(
        term in compact
        for term in (
            "到达后",
            "到埗",
            "抵达后",
            "afterarrival",
            "预先买",
        )
    )
    return asks_where_to_buy and compares_arrival


def _query_destination_terms(
    db: Session,
    tenant_id: int,
    query: str,
) -> tuple[str, ...]:
    normalized_query = normalize_retrieval_text(query)
    compact_query = _compact_retrieval_text(query)
    destination_cache = db.info.setdefault("agentdesk_catalog_destinations", {})
    if tenant_id in destination_cache:
        catalog_values = destination_cache[tenant_id]
    else:
        catalog_values = db.scalars(
            select(Product.destination).where(
                Product.tenant_id == tenant_id,
                Product.is_active.is_(True),
                Product.destination.is_not(None),
            )
        ).all()
        destination_cache[tenant_id] = tuple(catalog_values)
    catalog_aliases = {
        alias
        for value in catalog_values
        if value
        for alias in _destination_aliases(str(value))
    }
    matches = {
        alias
        for alias in catalog_aliases
        if alias in compact_query
        and not _is_purchase_origin_context(normalized_query, alias)
    }
    if matches:
        # Keep the most specific catalogue names while retaining separate
        # destinations in a multi-country query.
        matches = {
            match
            for match in matches
            if not any(match != other and match in other for other in matches)
        }
        return _expand_destination_terms(matches)

    for pattern in _DESTINATION_QUERY_PATTERNS:
        matched = pattern.search(normalized_query)
        if matched is None:
            continue
        destination = _compact_retrieval_text(matched.group(1))
        if 2 <= len(destination) <= 20:
            return _expand_destination_terms({destination})
    return ()


def _document_destination_strength(
    document: KnowledgeDocument,
    destination_terms: tuple[str, ...],
) -> int:
    title = _compact_retrieval_text(document.title)
    if any(term in title for term in destination_terms):
        return 2
    content = _compact_retrieval_text(document.content)
    if any(term in content for term in destination_terms):
        return 1
    return 0


def _postgres_destination_documents(
    db: Session,
    tenant_id: int,
    destination_terms: tuple[str, ...],
) -> list[KnowledgeDocument]:
    """Fetch only destination-matching documents for the PostgreSQL path.

    The previous implementation hydrated every active document before each
    query.  SQL-side candidate selection keeps the hard destination filter but
    avoids an O(total-document-count) Python scan on large indexes.  Both
    simplified and traditional variants are included in the SQL clauses.
    """

    variants: set[str] = set()
    for term in destination_terms:
        variants.update((term, _t2s.convert(term), _s2hk.convert(term)))
    clauses = [
        or_(
            KnowledgeDocument.title.ilike(f"%{variant}%"),
            KnowledgeDocument.content.ilike(f"%{variant}%"),
        )
        for variant in variants
        if variant
    ]
    if not clauses:
        return []
    return list(
        db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.is_active.is_(True),
                or_(*clauses),
            )
        ).all()
    )


def _query_vectors(query: str, model_name: str) -> list[list[float]]:
    variants = [query]
    if model_name == LOCAL_EMBEDDING_MODEL:
        # Existing local indexes may contain either simplified or traditional
        # source text. Query expansion keeps those indexes compatible without
        # forcing a destructive rebuild.
        variants.extend((_t2s.convert(query), _s2hk.convert(query)))
    unique_variants = list(dict.fromkeys(item.strip() for item in variants if item.strip()))
    return [
        vector
        for item in unique_variants
        if (vector := _cached_embed_query(item, model_name)) is not None
    ]


@lru_cache(maxsize=4096)
def _cached_embed_query(query: str, model_name: str) -> tuple[float, ...] | None:
    """Reuse deterministic query vectors across repeated customer questions."""

    vector = embed_query(query, model_name)
    return tuple(vector) if vector is not None else None


def _active_documents(db: Session, tenant_id: int) -> list[KnowledgeDocument]:
    """Reuse the active-document snapshot for a request/session."""

    cache = db.info.setdefault("agentdesk_active_documents", {})
    if tenant_id in cache:
        return cache[tenant_id]
    documents = list(
        db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.is_active.is_(True),
            )
        ).all()
    )
    cache[tenant_id] = documents
    return documents


def _ensure_document_chunks(db: Session, documents: list[KnowledgeDocument]) -> None:
    if not documents:
        return
    verified_tenants = db.info.setdefault("agentdesk_chunks_verified_tenants", set())
    tenant_ids = {document.tenant_id for document in documents}
    if tenant_ids and tenant_ids.issubset(verified_tenants):
        return
    document_ids = {document.id for document in documents}
    indexed_ids = set(
        db.scalars(
            select(KnowledgeChunk.document_id)
            .where(KnowledgeChunk.document_id.in_(document_ids))
            .distinct()
        ).all()
    )
    changed = False
    for document in documents:
        if document.id in indexed_ids:
            continue
        rebuild_document_chunks(db, document)
        changed = True
    if changed:
        db.commit()
    verified_tenants.update(tenant_ids)


def _postgres_vector_rows(
    db: Session,
    tenant_id: int,
    query: str,
    document_ids: set[int] | None,
    catalog_document_ids: set[int],
    destination_terms: tuple[str, ...] = (),
) -> list[tuple[KnowledgeChunk, KnowledgeDocument]]:
    """Fetch an ANN candidate pool from pgvector before Python BM25/reranking.

    The query is restricted to the configured model identifier.  This is an
    intentional safety boundary: if a migration left old ``local-hash-v1``
    rows beside the multilingual index, those rows are not silently compared
    with a different model.  The rebuild command must finish before traffic is
    switched to the PostgreSQL database.
    """

    if document_ids is not None and not document_ids:
        return []
    try:
        model_name = configured_embedding_model()
    except Exception:
        return []
    query_vectors = _query_vectors(query, model_name)
    rows_by_id: dict[int, tuple[KnowledgeChunk, KnowledgeDocument]] = {}
    candidate_limit = max(int(settings.rag_vector_candidate_limit), 10)

    # The tuned engine applies these once per pooled connection.  The session
    # fallback only runs for callers that constructed a raw SQLAlchemy engine.
    configure_hnsw_session(db)

    base_filters = [
        KnowledgeChunk.tenant_id == tenant_id,
        KnowledgeChunk.embedding_model == model_name,
        KnowledgeDocument.tenant_id == tenant_id,
        KnowledgeDocument.is_active.is_(True),
    ]
    if document_ids is not None:
        base_filters.append(KnowledgeChunk.document_id.in_(document_ids))
    if destination_terms:
        destination_variants: set[str] = set()
        for term in destination_terms:
            destination_variants.update(
                (term, _t2s.convert(term), _s2hk.convert(term))
            )
        destination_clauses = [
            or_(
                KnowledgeDocument.title.ilike(f"%{variant}%"),
                KnowledgeDocument.content.ilike(f"%{variant}%"),
            )
            for variant in destination_variants
            if variant
        ]
        if destination_clauses:
            base_filters.append(or_(*destination_clauses))

    for vector in query_vectors:
        # pgvector's bind processor accepts lists/ndarrays (not tuples from
        # the LRU cache), so materialise the cached immutable vector here.
        vector_value = list(vector)
        distance = KnowledgeChunk.embedding.cosine_distance(vector_value)
        statement = (
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(*base_filters)
            .order_by(distance)
            .limit(candidate_limit)
        )
        for chunk, document in db.execute(statement).all():
            rows_by_id[chunk.id] = (chunk, document)

    # Keep BM25 useful when a lexical exact hit falls outside the ANN pool.
    # This is a bounded supplementary scan; the final BM25 score is still
    # calculated by the existing reranker, not replaced by SQL text matching.
    lexical_terms = _postgres_lexical_terms(query)
    # The ANN pool is already the semantic recall set.  Only run the broader
    # ILIKE supplement when that pool is sparse; this keeps BM25 in the fusion
    # stage while avoiding a costly full-table text scan on every hot request.
    if lexical_terms and len(rows_by_id) < candidate_limit:
        lexical_clauses = [
            or_(
                KnowledgeChunk.content.ilike(f"%{term}%"),
                KnowledgeDocument.title.ilike(f"%{term}%"),
            )
            for term in lexical_terms
        ]
        statement = (
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(*base_filters, or_(*lexical_clauses))
            .order_by(KnowledgeChunk.id)
            .limit(candidate_limit)
        )
        for chunk, document in db.execute(statement).all():
            rows_by_id[chunk.id] = (chunk, document)

    # Product catalogue records are authoritative for direct product queries.
    # Include all matching catalogue chunks in the candidate pool even if a
    # terse query has a weak semantic distance.
    if catalog_document_ids:
        statement = (
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(
                *base_filters,
                KnowledgeChunk.document_id.in_(catalog_document_ids),
            )
            .order_by(KnowledgeChunk.id)
            # Structured catalogue rows are authoritative.  Do not truncate a
            # complete price-list request merely because the ANN pool is 64.
            .limit(max(candidate_limit, len(catalog_document_ids)))
        )
        for chunk, document in db.execute(statement).all():
            rows_by_id[chunk.id] = (chunk, document)

    return list(rows_by_id.values())


def retrieve_knowledge(
    db: Session,
    tenant_id: int,
    query: str,
    limit: int = 3,
) -> list[Document]:
    session_dialect = db.get_bind().dialect.name
    # PostgreSQL ingestion writes chunks atomically, so the hot retrieval path
    # does not hydrate every active document just to verify an already-built
    # index. SQLite keeps the defensive check for local/demo databases.
    if session_dialect == "postgresql":
        documents: list[KnowledgeDocument] = []
    else:
        documents = _active_documents(db, tenant_id)
        _ensure_document_chunks(db, documents)
        if not documents:
            return []

    purchase_origin_query = _is_purchase_origin_context(
        normalize_retrieval_text(query),
        "香港",
    )
    catalog_product_ids = (
        set()
        if purchase_origin_query
        else set(matching_product_catalog_ids(db, tenant_id, query))
    )
    catalog_document_ids: set[int] = set()
    if catalog_product_ids:
        page_rows = db.execute(
            select(KnowledgeWebPage.document_id, KnowledgeWebPage.metadata_json).where(
                KnowledgeWebPage.tenant_id == tenant_id,
                KnowledgeWebPage.review_status == "published",
            )
        ).all()
        catalog_document_ids = {
            document_id
            for document_id, metadata in page_rows
            if document_id is not None
            and (metadata or {}).get("extraction_mode")
            == "structured_product_catalog"
            and (metadata or {}).get("product_id") in catalog_product_ids
        }
    destination_terms = _query_destination_terms(db, tenant_id, query)
    destination_strengths: dict[int, int] = {}
    if destination_terms and session_dialect != "postgresql":
        destination_strengths = {
            document.id: strength
            for document in documents
            if (strength := _document_destination_strength(document, destination_terms))
        }
        # An explicit destination must never fall through to an unrelated
        # country merely because its vector happens to clear the threshold.
        if not destination_strengths:
            return []

    eligible_document_ids: set[int] | None
    if session_dialect == "postgresql":
        # Destination predicates are pushed into each ANN/lexical SQL query;
        # this avoids hydrating every matching guide document before retrieval.
        eligible_document_ids = None
    else:
        eligible_document_ids = {
            document.id
            for document in documents
            if not destination_terms or document.id in destination_strengths
        }
    retrieval_backend = "pgvector_hnsw" if session_dialect == "postgresql" else "python_cosine"
    if session_dialect == "postgresql":
        eligible_rows = _postgres_vector_rows(
            db,
            tenant_id,
            query,
            eligible_document_ids,
            catalog_document_ids,
            destination_terms,
        )
    else:
        rows = db.execute(
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeDocument.is_active.is_(True),
                KnowledgeChunk.document_id.in_(eligible_document_ids),
            )
        ).all()
        eligible_rows = list(rows)
    if destination_terms and session_dialect == "postgresql":
        destination_strengths = {
            document.id: strength
            for _, document in eligible_rows
            if (strength := _document_destination_strength(document, destination_terms))
        }
        # An explicit destination must never fall through to an unrelated
        # country merely because its vector happened to clear the threshold.
        if not destination_strengths:
            return []
    lexical_scores = _lexical_relevance_scores(query, eligible_rows)
    grouped: dict[str, list[tuple[KnowledgeChunk, KnowledgeDocument]]] = defaultdict(list)
    for chunk, document in eligible_rows:
        grouped[chunk.embedding_model].append((chunk, document))

    catalog_weight = _product_catalog_boost(query)
    matches: list[_RetrievalCandidate] = []
    for model_name, items in grouped.items():
        query_vectors = _query_vectors(query, model_name)
        for chunk, document in items:
            semantic_score = (
                max(
                    _cosine_similarity(query_vector, chunk.embedding)
                    for query_vector in query_vectors
                )
                if query_vectors
                else -1.0
            )
            lexical_score, title_score = lexical_scores.get(chunk.id, (0.0, 0.0))
            # A direct catalogue match is authoritative even when a terse
            # conversational query has weak standalone vector/BM25 scores
            # (for example, “去日本租哪个比较好”).  The catalogue boost is
            # applied below and must be allowed to rescue that candidate;
            # unrelated semantic collisions still fail closed here.
            if (
                semantic_score < settings.rag_min_similarity
                and lexical_score < _MIN_LEXICAL_RELEVANCE
                and not (
                    document.id in catalog_document_ids
                    and catalog_weight > 0
                )
            ):
                continue
            destination_strength = destination_strengths.get(document.id, 0)
            product_catalog_match = document.id in catalog_document_ids
            matches.append(
                _RetrievalCandidate(
                    chunk=chunk,
                    document=document,
                    semantic_score=semantic_score,
                    lexical_score=lexical_score,
                    title_score=title_score,
                    destination_strength=destination_strength,
                    product_catalog_match=product_catalog_match,
                    catalog_boost=catalog_weight if product_catalog_match else 0.0,
                )
            )

    semantic_maximum = max(
        (max(candidate.semantic_score, 0.0) for candidate in matches),
        default=0.0,
    )
    for candidate in matches:
        candidate.semantic_normalized = (
            max(candidate.semantic_score, 0.0) / semantic_maximum
            if semantic_maximum > 0
            else 0.0
        )
        destination_boost = {
            2: _DESTINATION_TITLE_BOOST,
            1: _DESTINATION_CONTENT_BOOST,
        }.get(candidate.destination_strength, 0.0)
        candidate.retrieval_score = (
            _SEMANTIC_FUSION_WEIGHT * candidate.semantic_normalized
            + _LEXICAL_FUSION_WEIGHT * candidate.lexical_score
            + destination_boost
            + candidate.catalog_boost
        )
    matches.sort(
        key=lambda item: (
            item.retrieval_score,
            item.title_score,
            item.lexical_score,
            item.semantic_score,
        ),
        reverse=True,
    )
    matches = _rerank_candidates(query, matches)

    results: list[Document] = []
    seen_sources: set[tuple[str, str | int]] = set()
    for candidate in matches:
        if candidate.retrieval_score < settings.rag_min_retrieval_score:
            continue
        if (
            candidate.catalog_boost <= 0
            and candidate.lexical_score < settings.rag_min_lexical_score
            and candidate.semantic_score < settings.rag_semantic_override_score
        ):
            continue
        deduplication_key = _source_deduplication_key(candidate.document)
        if deduplication_key in seen_sources:
            continue
        seen_sources.add(deduplication_key)
        results.append(
            Document(
                page_content=candidate.chunk.content,
                metadata={
                    "document_id": candidate.document.id,
                    "chunk_id": candidate.chunk.id,
                    "title": candidate.document.title,
                    "source": candidate.document.source,
                    "source_url": candidate.chunk.source_url or candidate.document.source,
                    "page_title": candidate.chunk.page_title or candidate.document.title,
                    "section_path": candidate.chunk.section_path,
                    "source_updated_at": (
                        candidate.chunk.source_updated_at.isoformat()
                        if candidate.chunk.source_updated_at is not None
                        else candidate.document.updated_at.isoformat()
                    ),
                    "token_count": candidate.chunk.token_count,
                    "category": candidate.document.category,
                    "similarity": round(candidate.semantic_score, 4),
                    "semantic_normalized": round(candidate.semantic_normalized, 4),
                    "bm25_score": round(candidate.lexical_score, 4),
                    "title_relevance": round(candidate.title_score, 4),
                    "catalog_boost": round(candidate.catalog_boost, 4),
                    "retrieval_score": round(candidate.retrieval_score, 4),
                    # Keep the public legacy label for API compatibility;
                    # ``reranker_model`` identifies the new final stage.
                    "reranker": "bm25_vector_metadata_v1",
                    "reranker_model": "pairwise_hybrid_v2",
                    "reranker_score": round(candidate.reranker_score, 4),
                    "vector_backend": retrieval_backend,
                    "retrieval_mode": (
                        "product_catalog_hybrid"
                        if candidate.product_catalog_match
                        else "destination_hybrid"
                        if destination_terms
                        else "hybrid"
                    ),
                    "product_catalog_match": candidate.product_catalog_match,
                    "destination_match": (
                        "title"
                        if candidate.destination_strength == 2
                        else "content"
                        if candidate.destination_strength == 1
                        else None
                    ),
                    "destination_terms": list(destination_terms),
                },
            )
        )
        if len(results) >= limit:
            break
    return results


def _cosine_similarity(first: list[float], second: list[float]) -> float:
    if not first or not second or len(first) != len(second):
        return -1.0
    numerator = sum(left * right for left, right in zip(first, second, strict=True))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if not first_norm or not second_norm:
        return -1.0
    return numerator / (first_norm * second_norm)


__all__ = [
    "LocalHashEmbeddings",
    "get_embeddings",
    "normalize_retrieval_text",
    "retrieve_knowledge",
    "should_prioritize_product_catalog",
]
