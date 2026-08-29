from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from time import perf_counter
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import PROJECT_ROOT, settings
from ..models import KnowledgeDocument, Product
from .agent import (
    SupportAgentWorkflow,
    _detect_language,
    _fallback_english_retrieval_query,
    active_db,
)
from .knowledge import normalize_retrieval_text, retrieve_knowledge
from .product_price_query import matching_product_catalog_ids


DEFAULT_QUALITY_SUITE = PROJECT_ROOT / "backend" / "evals" / "agent_quality_suite.json"
DEFAULT_REALISTIC_SUITE = PROJECT_ROOT / "backend" / "evals" / "agent_realistic_suite.json"
QUALITY_REPORT_DIR = PROJECT_ROOT / "data" / "evaluations"


def load_quality_suite(path: str | Path | None = None) -> dict[str, Any]:
    suite_path = Path(path) if path is not None else DEFAULT_QUALITY_SUITE
    with suite_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    required = {
        "version",
        "intent_cases",
        "country_recall_cases",
        "product_recall_cases",
        "retrieval_cases",
    }
    missing = sorted(required.difference(payload))
    if missing:
        raise ValueError(f"Quality suite is missing fields: {', '.join(missing)}")
    return payload


def load_realistic_suite(path: str | Path | None = None) -> dict[str, Any]:
    suite_path = Path(path) if path is not None else DEFAULT_REALISTIC_SUITE
    with suite_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    cases = payload.get("cases")
    if not isinstance(cases, list) or not 30 <= len(cases) <= 50:
        raise ValueError("真实问答集必须包含 30–50 条案例")
    required = {"id", "category", "query", "expected_route", "expected_language", "expected_handoff"}
    for case in cases:
        if not required.issubset(case):
            raise ValueError(f"真实问答案例缺少字段: {sorted(required.difference(case))}")
    return payload


def latest_quality_report_path(tenant_id: int) -> Path:
    if tenant_id <= 0:
        raise ValueError("tenant_id must be positive")
    return QUALITY_REPORT_DIR / f"tenant-{tenant_id}-latest.json"


def save_quality_report(report: dict[str, Any], path: str | Path | None = None) -> Path:
    tenant_id = int(report["tenant_id"])
    target = Path(path) if path is not None else latest_quality_report_path(tenant_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def load_latest_quality_report(tenant_id: int) -> dict[str, Any] | None:
    path = latest_quality_report_path(tenant_id)
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    if int(report.get("tenant_id", 0)) != tenant_id:
        raise ValueError("Quality report tenant does not match requested tenant")
    return report


def _percentage(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


def _recall_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [item for item in results if not item.get("skipped")]
    hits = sum(int(item["hit_count"]) for item in evaluated)
    expected = sum(int(item["expected_count"]) for item in evaluated)
    recalls = [float(item["recall_pct"]) for item in evaluated]
    return {
        "recall_pct": _percentage(hits, expected),
        "macro_recall_pct": round(fmean(recalls), 2) if recalls else 0.0,
        "hit_count": hits,
        "expected_count": expected,
        "evaluated_cases": len(evaluated),
        "skipped_cases": len(results) - len(evaluated),
        "cases": results,
    }


def _intent_metrics(
    db: Session,
    tenant_id: int,
    cases: list[dict[str, Any]],
    workflow: SupportAgentWorkflow,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    token = active_db.set(db)
    try:
        for case in cases:
            query = str(case["query"])
            expected = str(case["expected_route"])
            try:
                decision = workflow._classify(
                    {
                        "tenant_id": tenant_id,
                        "message": query,
                        "effective_message": query,
                        "history": [],
                        "forced_intent": None,
                        "preferred_language": None,
                    }
                )
                predicted = str(decision.get("intent", "handoff"))
                error = None
            except Exception as exc:  # pragma: no cover - defensive live-model guard
                predicted = "error"
                error = type(exc).__name__
            results.append(
                {
                    "id": case["id"],
                    "query": query,
                    "expected": expected,
                    "predicted": predicted,
                    "correct": predicted == expected,
                    "error": error,
                }
            )
    finally:
        active_db.reset(token)
    correct = sum(1 for item in results if item["correct"])
    return {
        "accuracy_pct": _percentage(correct, len(results)),
        "correct_count": correct,
        "case_count": len(results),
        "cases": results,
    }


def _catalog_products(db: Session, tenant_id: int) -> list[Product]:
    return list(
        db.scalars(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.is_active.is_(True),
            )
        ).all()
    )


def _country_recall_metrics(
    db: Session,
    tenant_id: int,
    cases: list[dict[str, Any]],
    products: list[Product],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    names = {product.id: product.name for product in products}
    for case in cases:
        expected_terms = [
            normalize_retrieval_text(str(term))
            for term in case["expected_destination_terms"]
        ]
        expected_ids = {
            product.id
            for product in products
            if product.destination
            and any(
                term in normalize_retrieval_text(product.destination)
                for term in expected_terms
            )
        }
        retrieved_ids = set(
            matching_product_catalog_ids(db, tenant_id, str(case["query"]))
        )
        hits = expected_ids.intersection(retrieved_ids)
        missed = expected_ids.difference(retrieved_ids)
        results.append(
            {
                "id": case["id"],
                "query": case["query"],
                "expected_count": len(expected_ids),
                "retrieved_count": len(retrieved_ids),
                "hit_count": len(hits),
                "recall_pct": _percentage(len(hits), len(expected_ids)),
                "missed_products": [names[item] for item in sorted(missed)],
                "unexpected_products": [
                    names[item]
                    for item in sorted(retrieved_ids.difference(expected_ids))
                    if item in names
                ],
                "skipped": not expected_ids,
            }
        )
    return _recall_summary(results)


def _product_recall_metrics(
    db: Session,
    tenant_id: int,
    cases: list[dict[str, Any]],
    products: list[Product],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    names = {product.id: product.name for product in products}
    normalized_names = {
        product.id: normalize_retrieval_text(product.name) for product in products
    }
    for case in cases:
        expected_terms = [
            normalize_retrieval_text(str(term)) for term in case["expected_name_terms"]
        ]
        expected_ids = {
            product_id
            for product_id, name in normalized_names.items()
            if any(term in name for term in expected_terms)
        }
        retrieved_ids = set(
            matching_product_catalog_ids(db, tenant_id, str(case["query"]))
        )
        hits = expected_ids.intersection(retrieved_ids)
        missed = expected_ids.difference(retrieved_ids)
        results.append(
            {
                "id": case["id"],
                "query": case["query"],
                "expected_count": len(expected_ids),
                "retrieved_count": len(retrieved_ids),
                "hit_count": len(hits),
                "recall_pct": _percentage(len(hits), len(expected_ids)),
                "missed_products": [names[item] for item in sorted(missed)],
                "unexpected_products": [
                    names[item]
                    for item in sorted(retrieved_ids.difference(expected_ids))
                    if item in names
                ],
                "skipped": not expected_ids,
            }
        )
    return _recall_summary(results)


def _reference_exists(
    documents: list[KnowledgeDocument],
    terms: list[str],
) -> bool:
    return any(
        any(term in f"{document.source} {document.title}".casefold() for term in terms)
        for document in documents
    )


def _retrieval_metrics(
    db: Session,
    tenant_id: int,
    cases: list[dict[str, Any]],
    workflow: SupportAgentWorkflow,
    *,
    live_model: bool,
) -> dict[str, Any]:
    knowledge_documents = list(
        db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.is_active.is_(True),
            )
        ).all()
    )
    results: list[dict[str, Any]] = []
    for case in cases:
        query = str(case["query"])
        expected_terms = [
            str(term).casefold() for term in case["expected_source_terms"]
        ]
        reference_exists = _reference_exists(knowledge_documents, expected_terms)
        if _detect_language(query) == "en":
            retrieval_query = (
                workflow._translate_english_retrieval_query(query)
                if live_model
                else _fallback_english_retrieval_query(query)
            )
        else:
            retrieval_query = query
        documents = retrieve_knowledge(db, tenant_id, retrieval_query, limit=3)

        def matches(document: Any) -> bool:
            value = (
                f"{document.metadata.get('source', '')} "
                f"{document.metadata.get('title', '')}"
            ).casefold()
            return any(term in value for term in expected_terms)

        top1 = bool(documents and matches(documents[0]))
        top3 = any(matches(document) for document in documents[:3])
        results.append(
            {
                "id": case["id"],
                "query": query,
                "retrieval_query": retrieval_query,
                "top1_correct": top1,
                "top3_correct": top3,
                "expected_source_terms": expected_terms,
                "retrieved": [
                    {
                        "title": document.metadata.get("title", ""),
                        "source": document.metadata.get("source", ""),
                        "score": document.metadata.get("retrieval_score"),
                    }
                    for document in documents[:3]
                ],
                "skipped": not reference_exists,
            }
        )
    evaluated = [item for item in results if not item["skipped"]]
    top1_count = sum(1 for item in evaluated if item["top1_correct"])
    top3_count = sum(1 for item in evaluated if item["top3_correct"])
    return {
        "top1_accuracy_pct": _percentage(top1_count, len(evaluated)),
        "top3_accuracy_pct": _percentage(top3_count, len(evaluated)),
        "top1_correct_count": top1_count,
        "top3_correct_count": top3_count,
        "evaluated_cases": len(evaluated),
        "skipped_cases": len(results) - len(evaluated),
        "cases": results,
    }


def run_quality_evaluation(
    db: Session,
    tenant_id: int,
    *,
    live_model: bool = False,
    suite_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the labelled quality suite without creating customer conversations."""

    suite = load_quality_suite(suite_path)
    workflow = SupportAgentWorkflow()
    if not live_model:
        workflow.model = None
    products = _catalog_products(db, tenant_id)
    intent = _intent_metrics(
        db,
        tenant_id,
        list(suite["intent_cases"]),
        workflow,
    )
    country = _country_recall_metrics(
        db,
        tenant_id,
        list(suite["country_recall_cases"]),
        products,
    )
    product = _product_recall_metrics(
        db,
        tenant_id,
        list(suite["product_recall_cases"]),
        products,
    )
    retrieval = _retrieval_metrics(
        db,
        tenant_id,
        list(suite["retrieval_cases"]),
        workflow,
        live_model=live_model,
    )
    return {
        "suite_version": suite["version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "live_model": live_model,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.configured_embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "metrics": {
            "intent_accuracy_pct": intent["accuracy_pct"],
            "country_recall_pct": country["recall_pct"],
            "product_recall_pct": product["recall_pct"],
            "retrieval_top1_accuracy_pct": retrieval["top1_accuracy_pct"],
            "retrieval_top3_accuracy_pct": retrieval["top3_accuracy_pct"],
        },
        "intent_accuracy": intent,
        "country_recall": country,
        "product_recall": product,
        "retrieval_accuracy": retrieval,
    }


def run_realistic_evaluation(
    db: Session,
    tenant_id: int,
    *,
    live_model: bool = False,
    suite_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the end-to-end labelled question set without sending messages."""

    suite = load_realistic_suite(suite_path)
    workflow = SupportAgentWorkflow()
    if not live_model:
        workflow.model = None
    results: list[dict[str, Any]] = []
    token = active_db.set(db)
    try:
        for case in suite["cases"]:
            started = perf_counter()
            query = str(case["query"])
            try:
                answer = workflow.run(
                    db,
                    tenant_id=tenant_id,
                    conversation_id=-(len(results) + 1),
                    customer_name="Evaluation customer",
                    customer_phone="",
                    message=query,
                    history=[],
                )
                predicted_route = str(answer.route)
                predicted_language = str(answer.language or _detect_language(query))
                predicted_handoff = bool(answer.handoff)
                source_count = len(answer.sources)
                error = None
            except Exception as exc:  # pragma: no cover - defensive report path
                predicted_route = "error"
                predicted_language = "unknown"
                predicted_handoff = False
                source_count = 0
                error = type(exc).__name__
            elapsed_ms = round((perf_counter() - started) * 1000, 2)
            results.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "query": query,
                    "expected_route": case["expected_route"],
                    "predicted_route": predicted_route,
                    "route_correct": predicted_route == str(case["expected_route"]),
                    "expected_language": case["expected_language"],
                    "predicted_language": predicted_language,
                    "language_correct": predicted_language == str(case["expected_language"]),
                    "expected_handoff": bool(case["expected_handoff"]),
                    "predicted_handoff": predicted_handoff,
                    "handoff_correct": predicted_handoff == bool(case["expected_handoff"]),
                    "source_count": source_count,
                    "latency_ms": elapsed_ms,
                    "error": error,
                }
            )
    finally:
        active_db.reset(token)

    def pct(field: str) -> float:
        return _percentage(sum(1 for item in results if item[field]), len(results))

    category_metrics: dict[str, dict[str, Any]] = {}
    for category in sorted({str(item["category"]) for item in results}):
        subset = [item for item in results if item["category"] == category]
        category_metrics[category] = {
            "case_count": len(subset),
            "route_accuracy_pct": _percentage(
                sum(1 for item in subset if item["route_correct"]), len(subset)
            ),
            "language_accuracy_pct": _percentage(
                sum(1 for item in subset if item["language_correct"]), len(subset)
            ),
            "handoff_accuracy_pct": _percentage(
                sum(1 for item in subset if item["handoff_correct"]), len(subset)
            ),
            "p95_latency_ms": sorted(item["latency_ms"] for item in subset)[
                max(0, int(len(subset) * 0.95) - 1)
            ],
        }
    latencies = sorted(item["latency_ms"] for item in results)
    return {
        "suite_version": suite["version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "live_model": live_model,
        "case_count": len(results),
        "metrics": {
            "route_accuracy_pct": pct("route_correct"),
            "language_accuracy_pct": pct("language_correct"),
            "handoff_accuracy_pct": pct("handoff_correct"),
            "citation_rate_pct": _percentage(
                sum(1 for item in results if item["source_count"] > 0), len(results)
            ),
            "p50_latency_ms": latencies[max(0, int(len(latencies) * 0.50) - 1)] if latencies else 0,
            "p95_latency_ms": latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else 0,
        },
        "category_metrics": category_metrics,
        "cases": results,
    }


__all__ = [
    "DEFAULT_QUALITY_SUITE",
    "DEFAULT_REALISTIC_SUITE",
    "latest_quality_report_path",
    "load_latest_quality_report",
    "load_quality_suite",
    "load_realistic_suite",
    "run_realistic_evaluation",
    "run_quality_evaluation",
    "save_quality_report",
]
