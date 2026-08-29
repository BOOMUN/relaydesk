from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..database import get_db
from ..dependencies import get_current_user, require_roles
from ..models import User, UserRole
from ..services.quality_evaluation import (
    load_latest_quality_report,
    run_realistic_evaluation,
)
from sqlalchemy.orm import Session


router = APIRouter(prefix="/api/quality-evaluation", tags=["quality-evaluation"])


@router.get("/latest")
def latest_quality_evaluation(user: User = Depends(get_current_user)):
    try:
        report = load_latest_quality_report(user.tenant_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="评测报告无法读取") from exc
    if report is None:
        raise HTTPException(status_code=404, detail="尚未生成 Agent 质量评测报告")
    return report


@router.post("/realistic/run")
def run_realistic_quality_evaluation(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER, UserRole.ANALYST)),
):
    report = run_realistic_evaluation(db, user.tenant_id, live_model=False)
    return report
