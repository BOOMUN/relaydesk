from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_roles
from ..models import AuditLog, User, UserRole
from ..schemas import (
    AgentProfileDraftUpdate,
    AgentProfileGenerateRequest,
    AgentProfilePublishResponse,
    AgentProfileResponse,
    AgentProfileVersionResponse,
)
from ..services.agent_profiles import (
    generate_agent_draft,
    list_versions,
    profile_state,
    publish_agent_draft,
    rollback_agent_version,
    update_agent_draft,
)


router = APIRouter(prefix="/api/ai-agent", tags=["ai-agent"])
manage_agent = require_roles(UserRole.ADMIN, UserRole.MANAGER)
publish_agent = require_roles(UserRole.ADMIN)


def _version_payload(version) -> AgentProfileVersionResponse:
    return AgentProfileVersionResponse.model_validate(version)


def _audit(
    db: Session,
    user: User,
    action: str,
    version_id: int,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            tenant_id=user.tenant_id,
            user_id=user.id,
            action=action,
            entity_type="agent_profile_version",
            entity_id=str(version_id),
            details=details or {},
        )
    )
    db.commit()


@router.get("", response_model=AgentProfileResponse)
def get_agent_profile(
    db: Session = Depends(get_db),
    user: User = Depends(manage_agent),
) -> AgentProfileResponse:
    profile, active, draft = profile_state(db, user.tenant_id, user.id)
    return AgentProfileResponse(
        profile_id=profile.id,
        active_version=_version_payload(active) if active is not None else None,
        draft_version=_version_payload(draft) if draft is not None else None,
    )


@router.patch("/draft", response_model=AgentProfileVersionResponse)
def save_agent_draft(
    payload: AgentProfileDraftUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(manage_agent),
) -> AgentProfileVersionResponse:
    version = update_agent_draft(db, user, payload.model_dump(exclude_none=True))
    _audit(db, user, "agent_profile.draft_saved", version.id)
    return _version_payload(version)


@router.post("/generate", response_model=AgentProfileVersionResponse)
def generate_agent_profile(
    payload: AgentProfileGenerateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(manage_agent),
) -> AgentProfileVersionResponse:
    try:
        version = generate_agent_draft(db, user, payload.source_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"网站分析失败：{exc}") from exc
    _audit(
        db,
        user,
        "agent_profile.draft_generated",
        version.id,
        {"source_url": version.source_url},
    )
    return _version_payload(version)


@router.post("/publish", response_model=AgentProfilePublishResponse)
def publish_agent_profile(
    db: Session = Depends(get_db),
    user: User = Depends(publish_agent),
) -> AgentProfilePublishResponse:
    try:
        version = publish_agent_draft(db, user)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        db,
        user,
        "agent_profile.published",
        version.id,
        {"version_number": version.version_number},
    )
    return AgentProfilePublishResponse(active_version=_version_payload(version))


@router.get("/versions", response_model=list[AgentProfileVersionResponse])
def get_agent_versions(
    db: Session = Depends(get_db),
    user: User = Depends(manage_agent),
) -> list[AgentProfileVersionResponse]:
    return [_version_payload(version) for version in list_versions(db, user.tenant_id)]


@router.post(
    "/versions/{version_id}/rollback",
    response_model=AgentProfilePublishResponse,
)
def rollback_agent_profile(
    version_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(publish_agent),
) -> AgentProfilePublishResponse:
    try:
        version = rollback_agent_version(db, user, version_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(
        db,
        user,
        "agent_profile.rolled_back",
        version.id,
        {
            "version_number": version.version_number,
            "rollback_from_version_id": version.rollback_from_version_id,
        },
    )
    return AgentProfilePublishResponse(active_version=_version_payload(version))


__all__ = ["router"]
