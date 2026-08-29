"""Compare retrieval quality and latency on the existing labelled test suite.

The benchmark does not change production traffic.  It runs the same intent,
country, product and Top-1/Top-3 cases against two database URLs, then measures
warm retrieval and deterministic end-to-end response latency (p50/p95/p99) for
the suite's retrieval queries.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import settings  # noqa: E402
from backend.app.database import create_database_engine  # noqa: E402
from backend.app.models import KnowledgeChunk, KnowledgeDocument  # noqa: E402
from backend.app.services.agent import (  # noqa: E402
    SupportAgentWorkflow,
    _detect_language,
    _fallback_english_retrieval_query,
)
from backend.app.services.embeddings import warmup_embeddings  # noqa: E402
from backend.app.services.knowledge import retrieve_knowledge  # noqa: E402
from backend.app.services.quality_evaluation import (  # noqa: E402
    load_quality_suite,
    run_quality_evaluation,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-url",
        default="sqlite:///./data/agentdesk.db",
        help="Existing index before migration (default: local SQLite DB).",
    )
    parser.add_argument(
        "--candidate-url",
        default=settings.database_url,
        help="Migrated candidate index (default: AGENTDESK_DATABASE_URL).",
    )
    parser.add_argument(
        "--baseline-embedding-provider",
        choices=("fastembed", "openai", "local_hash"),
        help="Provider used for baseline rows (use local_hash for an isolated legacy baseline).",
    )
    parser.add_argument(
        "--candidate-embedding-provider",
        choices=("fastembed", "openai", "local_hash"),
        help="Provider used for candidate rows (defaults to current configuration).",
    )
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--pool-concurrency",
        type=int,
        default=0,
        help=(
            "Optional concurrent retrieval workers for connection-pool pressure "
            "(0 disables; use a value above pool_size to test overflow/waiting)."
        ),
    )
    parser.add_argument(
        "--pool-requests",
        type=int,
        default=0,
        help="Requests for the optional pool-pressure run (default: 4x concurrency).",
    )
    parser.add_argument(
        "--max-latency-regression-pct",
        type=float,
        default=10.0,
        help="Hold cutover when retrieval/response p95 regresses beyond this percent.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_only")
    return parser.parse_args()


def _engine(url: str):
    return create_database_engine(url)


def _safe_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return url


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, math.ceil((percentile / 100) * len(ordered)) - 1),
    )
    return round(ordered[index], 3)


def _embedding_state(db: Session) -> dict[str, Any]:
    models = sorted(
        str(item)
        for item in db.scalars(select(KnowledgeChunk.embedding_model).distinct()).all()
        if item
    )
    dimensions: set[int] = set()
    sample_count = 0
    for vector in db.scalars(select(KnowledgeChunk.embedding)).yield_per(512):
        try:
            dimensions.add(len(vector))
        except TypeError:
            dimensions.add(-1)
        sample_count += 1
        if sample_count >= 5000:
            break
    expected_model = settings.configured_embedding_model
    expected_dimensions = int(settings.embedding_dimensions)
    return {
        "models": models,
        "dimensions": sorted(dimensions),
        "sampled_chunks": sample_count,
        "expected_model": expected_model,
        "expected_dimensions": expected_dimensions,
        "model_set_ok": models == [expected_model],
        "dimensions_ok": dimensions == {expected_dimensions},
    }


def _latency_metrics(
    db: Session,
    tenant_id: int,
    suite_path: Path | None,
    repeats: int,
) -> dict[str, Any]:
    suite = load_quality_suite(suite_path)
    workflow = SupportAgentWorkflow()
    workflow.model = None
    values: list[float] = []
    response_values: list[float] = []
    cases: list[dict[str, Any]] = []
    for case_index, case in enumerate(suite["retrieval_cases"]):
        query = str(case["query"])
        retrieval_query = (
            _fallback_english_retrieval_query(query)
            if _detect_language(query) == "en"
            else query
        )
        # Warm model/cache and database connection before recording samples.
        retrieve_knowledge(db, tenant_id, retrieval_query, limit=3)
        samples: list[float] = []
        for _ in range(max(1, repeats)):
            start = time.perf_counter_ns()
            retrieve_knowledge(db, tenant_id, retrieval_query, limit=3)
            samples.append((time.perf_counter_ns() - start) / 1_000_000)
        values.extend(samples)

        # Measure the deterministic end-to-end response path as well as the
        # retrieval primitive.  The model is disabled in this benchmark, so
        # this isolates routing + structured lookup + RAG + guard overhead and
        # does not call an external LLM or send a message.
        response_samples: list[float] = []
        response_conversation_id = 10_000_000 + case_index
        workflow.run(
            db,
            tenant_id=tenant_id,
            conversation_id=response_conversation_id,
            customer_name="benchmark",
            customer_phone="0000000000",
            message=query,
            history=[],
        )
        for repeat_index in range(max(1, repeats)):
            start = time.perf_counter_ns()
            workflow.run(
                db,
                tenant_id=tenant_id,
                conversation_id=response_conversation_id + repeat_index + 1,
                customer_name="benchmark",
                customer_phone="0000000000",
                message=query,
                history=[],
            )
            response_samples.append((time.perf_counter_ns() - start) / 1_000_000)
        response_values.extend(response_samples)
        cases.append(
            {
                "id": case["id"],
                "query": query,
                "retrieval_query": retrieval_query,
                "p50_ms": _percentile(samples, 50),
                "p95_ms": _percentile(samples, 95),
                "response_p50_ms": _percentile(response_samples, 50),
                "response_p95_ms": _percentile(response_samples, 95),
                "samples": len(samples),
            }
        )
    return {
        "sample_count": len(values),
        "mean_ms": round(mean(values), 3) if values else 0.0,
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
        "p99_ms": _percentile(values, 99),
        "response_sample_count": len(response_values),
        "response_mean_ms": round(mean(response_values), 3)
        if response_values
        else 0.0,
        "response_p50_ms": _percentile(response_values, 50),
        "response_p95_ms": _percentile(response_values, 95),
        "response_p99_ms": _percentile(response_values, 99),
        "cases": cases,
    }


def _pool_pressure_metrics(
    engine,
    tenant_id: int,
    suite_path: Path | None,
    concurrency: int,
    request_count: int,
) -> dict[str, Any]:
    """Measure concurrent retrieval using one tuned engine and independent sessions.

    The regular benchmark is intentionally serial so quality and latency are
    stable.  This optional probe checks that the configured QueuePool handles a
    production-like burst without sharing SQLAlchemy sessions across threads.
    Query vectors are warmed before timing; each worker owns its Session and the
    engine owns all pooling/recycle/pre-ping behavior.
    """

    if concurrency <= 0 or request_count <= 0:
        return {"enabled": False}
    suite = load_quality_suite(suite_path)
    queries = [str(case["query"]) for case in suite["retrieval_cases"]]
    if not queries:
        return {"enabled": True, "concurrency": concurrency, "request_count": 0}
    with Session(engine) as warm_db:
        for query in queries:
            retrieval_query = (
                _fallback_english_retrieval_query(query)
                if _detect_language(query) == "en"
                else query
            )
            retrieve_knowledge(warm_db, tenant_id, retrieval_query, limit=3)

    def _one(index: int) -> float:
        query = queries[index % len(queries)]
        retrieval_query = (
            _fallback_english_retrieval_query(query)
            if _detect_language(query) == "en"
            else query
        )
        started = time.perf_counter_ns()
        with Session(engine) as db:
            retrieve_knowledge(db, tenant_id, retrieval_query, limit=3)
        return (time.perf_counter_ns() - started) / 1_000_000

    started = time.perf_counter()
    values: list[float] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_one, index) for index in range(request_count)]
        for future in as_completed(futures):
            try:
                values.append(float(future.result()))
            except Exception as exc:  # pragma: no cover - depends on DB pressure
                errors.append(f"{type(exc).__name__}: {exc}")
    elapsed_ms = (time.perf_counter() - started) * 1000
    pool = getattr(engine, "pool", None)
    pool_info: dict[str, Any] = {"class": type(pool).__name__ if pool else None}
    if pool is not None:
        for name in ("size", "_max_overflow", "timeout", "_recycle", "_use_lifo"):
            value = getattr(pool, name, None)
            if value is None and name == "_use_lifo":
                # SQLAlchemy stores this flag on the underlying queue in some
                # releases rather than exposing it directly on QueuePool.
                value = getattr(getattr(pool, "_pool", None), "use_lifo", None)
            if callable(value):
                try:
                    value = value()
                except TypeError:
                    pass
            if value is not None:
                pool_info[name.lstrip("_")] = value
    return {
        "enabled": True,
        "concurrency": concurrency,
        "request_count": request_count,
        "completed": len(values),
        "errors": errors[:10],
        "elapsed_ms": round(elapsed_ms, 3),
        "throughput_rps": round(len(values) / (elapsed_ms / 1000), 3)
        if elapsed_ms > 0
        else 0.0,
        "mean_ms": round(mean(values), 3) if values else 0.0,
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
        "p99_ms": _percentile(values, 99),
        "pool": pool_info,
    }


def _run_one(
    url: str,
    tenant_id: int,
    suite: Path | None,
    repeats: int,
    provider: str | None = None,
    pool_concurrency: int = 0,
    pool_requests: int = 0,
) -> dict[str, Any]:
    previous_provider = settings.embedding_provider
    if provider:
        settings.embedding_provider = provider
    try:
        # A provider can change between baseline and candidate in a fair
        # legacy-vs-multilingual comparison. Query vectors are cached by model,
        # but clearing avoids retaining unnecessary entries in long runs.
        from backend.app.services.knowledge import _cached_embed_query

        _cached_embed_query.cache_clear()
        warmup = warmup_embeddings()
        engine = _engine(url)
        try:
            with Session(engine) as db:
                quality = run_quality_evaluation(
                    db,
                    tenant_id,
                    live_model=False,
                    suite_path=suite,
                )
                latency = _latency_metrics(db, tenant_id, suite, repeats)
                state = _embedding_state(db)
            # Release the serial benchmark session before opening concurrent
            # worker sessions; otherwise it would consume one pool slot during
            # the pressure probe and make the configured capacity misleading.
            pool_pressure = _pool_pressure_metrics(
                engine,
                tenant_id,
                suite,
                pool_concurrency,
                pool_requests or max(1, pool_concurrency * 4),
            )
            schema = {
                "dialect": engine.dialect.name,
                "knowledge_chunks_embedding": None,
            }
            if engine.dialect.name == "postgresql":
                inspector = inspect(engine)
                for column in inspector.get_columns("knowledge_chunks"):
                    if column["name"] == "embedding":
                        schema["knowledge_chunks_embedding"] = str(column["type"])
                        break
            expected_type = f"VECTOR({int(settings.embedding_dimensions)})"
            schema["schema_ok"] = (
                engine.dialect.name != "postgresql"
                or str(schema["knowledge_chunks_embedding"]).casefold()
                == expected_type.casefold()
            )
        finally:
            engine.dispose()
        return {
            "database_url": _safe_url(url),
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.configured_embedding_model,
            "quality": quality,
            "latency": latency,
            "pool_pressure": pool_pressure,
            "embedding_state": state,
            "schema": schema,
            "warmup": warmup,
        }
    finally:
        settings.embedding_provider = previous_provider


def _comparison(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_latency_regression_pct: float,
) -> dict[str, Any]:
    keys = (
        "intent_accuracy_pct",
        "country_recall_pct",
        "product_recall_pct",
        "retrieval_top1_accuracy_pct",
        "retrieval_top3_accuracy_pct",
    )
    base_metrics = baseline["quality"]["metrics"]
    cand_metrics = candidate["quality"]["metrics"]
    deltas = {key: round(float(cand_metrics[key]) - float(base_metrics[key]), 2) for key in keys}
    base_latency = baseline["latency"]
    cand_latency = candidate["latency"]
    base_p95 = float(base_latency["p95_ms"])
    cand_p95 = float(cand_latency["p95_ms"])
    base_response_p95 = float(base_latency["response_p95_ms"])
    cand_response_p95 = float(cand_latency["response_p95_ms"])
    retrieval_change_pct = (
        (cand_p95 - base_p95) * 100 / base_p95 if base_p95 else None
    )
    response_change_pct = (
        (cand_response_p95 - base_response_p95) * 100 / base_response_p95
        if base_response_p95
        else None
    )
    base_pool = baseline.get("pool_pressure", {})
    cand_pool = candidate.get("pool_pressure", {})
    base_pool_p95 = float(base_pool.get("p95_ms", 0.0) or 0.0)
    cand_pool_p95 = float(cand_pool.get("p95_ms", 0.0) or 0.0)
    pool_change_pct = (
        (cand_pool_p95 - base_pool_p95) * 100 / base_pool_p95
        if base_pool.get("enabled") and cand_pool.get("enabled") and base_pool_p95
        else None
    )
    quality_ok = all(float(value) >= 0 for value in deltas.values())
    candidate_state = candidate.get("embedding_state", {})
    candidate_schema = candidate.get("schema", {})
    index_state_ok = bool(candidate_state.get("model_set_ok", True)) and bool(
        candidate_state.get("dimensions_ok", True)
    ) and bool(candidate_schema.get("schema_ok", True))
    quality_ok = quality_ok and index_state_ok
    latency_ok = all(
        change is None or change <= max_latency_regression_pct
        for change in (retrieval_change_pct, response_change_pct, pool_change_pct)
    )
    # A pressure run is optional, but when enabled any candidate worker error
    # is a hard fail even if the serial p95 numbers look healthy.
    if cand_pool.get("enabled") and cand_pool.get("errors"):
        latency_ok = False
    if not quality_ok:
        recommendation = "keep_production_on_baseline_and_investigate_quality_regression"
    elif not latency_ok:
        recommendation = "hold_cutover_and_tune_latency_before_production"
    else:
        recommendation = "candidate_ready_for_manual_review"
    return {
        "metric_delta_candidate_minus_baseline": deltas,
        "latency_ms": {
            "baseline_p50": base_latency["p50_ms"],
            "candidate_p50": cand_latency["p50_ms"],
            "baseline_p95": base_p95,
            "candidate_p95": cand_p95,
            "p95_change_pct": round(retrieval_change_pct, 2)
            if retrieval_change_pct is not None
            else None,
            "baseline_response_p95": base_response_p95,
            "candidate_response_p95": cand_response_p95,
            "response_p95_change_pct": round(response_change_pct, 2)
            if response_change_pct is not None
            else None,
            "pool_p95_baseline": base_pool_p95 if base_pool.get("enabled") else None,
            "pool_p95_candidate": cand_pool_p95 if cand_pool.get("enabled") else None,
            "pool_p95_change_pct": round(pool_change_pct, 2)
            if pool_change_pct is not None
            else None,
            "pool_candidate_errors": len(cand_pool.get("errors", []))
            if cand_pool.get("enabled")
            else 0,
            "max_allowed_regression_pct": max_latency_regression_pct,
        },
        "production_cutover_recommendation": recommendation,
        "index_state_ok": index_state_ok,
    }


def main() -> int:
    args = _arguments()
    if (
        args.tenant_id <= 0
        or args.repeats <= 0
        or args.max_latency_regression_pct < 0
        or args.pool_concurrency < 0
        or args.pool_requests < 0
    ):
        raise SystemExit(
            "tenant-id/repeats must be positive and latency threshold non-negative"
        )
    baseline = _run_one(
        args.baseline_url,
        args.tenant_id,
        args.suite,
        args.repeats,
        args.baseline_embedding_provider,
        args.pool_concurrency,
        args.pool_requests,
    )
    candidate = _run_one(
        args.candidate_url,
        args.tenant_id,
        args.suite,
        args.repeats,
        args.candidate_embedding_provider,
        args.pool_concurrency,
        args.pool_requests,
    )
    report = {
        "suite": str(args.suite or "backend/evals/agent_quality_suite.json"),
        "tenant_id": args.tenant_id,
        "embedding_configuration": {
            "provider": settings.embedding_provider,
            "model": settings.configured_embedding_model,
            "dimensions": settings.embedding_dimensions,
        },
        "baseline": baseline,
        "candidate": candidate,
        "comparison": _comparison(
            baseline,
            candidate,
            max_latency_regression_pct=args.max_latency_regression_pct,
        ),
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
