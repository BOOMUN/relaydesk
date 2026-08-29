"""Run the 40-case end-to-end Agent quality suite locally."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.database import SessionLocal, create_tables
from backend.app.services.quality_evaluation import (
    run_realistic_evaluation,
)


if __name__ == "__main__":
    create_tables()
    with SessionLocal() as db:
        report = run_realistic_evaluation(db, 1, live_model=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))
