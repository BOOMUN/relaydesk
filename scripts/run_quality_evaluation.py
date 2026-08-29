from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import SessionLocal  # noqa: E402
from backend.app.services.quality_evaluation import (  # noqa: E402
    run_quality_evaluation,
    save_quality_report,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure labelled AgentDesk intent and retrieval quality.",
    )
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="Use the configured chat model for intent routing and English query translation.",
    )
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--save-latest",
        action="store_true",
        help="Publish the report for the authenticated analytics API.",
    )
    parser.add_argument("--json", action="store_true", dest="json_only")
    parser.add_argument("--show-failures", action="store_true")
    return parser.parse_args()


def _failures(report: dict) -> list[str]:
    lines: list[str] = []
    for case in report["intent_accuracy"]["cases"]:
        if not case["correct"]:
            lines.append(
                f"intent | {case['id']} | expected={case['expected']} "
                f"predicted={case['predicted']} | {case['query']}"
            )
    for section in ("country_recall", "product_recall"):
        for case in report[section]["cases"]:
            if not case["skipped"] and case["recall_pct"] < 100:
                lines.append(
                    f"{section} | {case['id']} | recall={case['recall_pct']}% | "
                    f"missed={case['missed_products']}"
                )
    for case in report["retrieval_accuracy"]["cases"]:
        if not case["skipped"] and not case["top1_correct"]:
            retrieved = [item["title"] for item in case["retrieved"]]
            rank = "top3=false" if not case["top3_correct"] else "top1=false, top3=true"
            lines.append(
                f"retrieval | {case['id']} | {rank} | retrieved={retrieved}"
            )
    return lines


def main() -> int:
    args = _arguments()
    with SessionLocal() as db:
        report = run_quality_evaluation(
            db,
            args.tenant_id,
            live_model=args.live_model,
            suite_path=args.suite,
        )
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    if args.save_latest:
        save_quality_report(report)
    if args.json_only:
        print(serialized)
        return 0

    metrics = report["metrics"]
    print(f"Suite: {report['suite_version']} | live_model={report['live_model']}")
    print(f"Intent accuracy: {metrics['intent_accuracy_pct']:.2f}%")
    print(f"Country recall: {metrics['country_recall_pct']:.2f}%")
    print(f"Product recall: {metrics['product_recall_pct']:.2f}%")
    print(f"Retrieval Top-1 accuracy: {metrics['retrieval_top1_accuracy_pct']:.2f}%")
    print(f"Retrieval Top-3 accuracy: {metrics['retrieval_top3_accuracy_pct']:.2f}%")
    if args.show_failures:
        failures = _failures(report)
        print(f"Failures: {len(failures)}")
        for failure in failures:
            print(f"- {failure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
