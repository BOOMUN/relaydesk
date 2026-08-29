from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..channels import get_channel_provider
from ..config import settings
from ..database import get_db
from ..dependencies import get_current_user
from ..models import User, UserRole, utcnow
from ..schemas import WhatsAppConnectionResponse
from ..services.whatsapp import (
    WhatsAppError,
    connect_evolution,
)


router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def _require_integration_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in {UserRole.ADMIN.value, UserRole.MANAGER.value}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有管理员可以配置消息通道",
        )
    return user


@router.get("/whatsapp", response_model=WhatsAppConnectionResponse)
def whatsapp_status(
    user: User = Depends(_require_integration_admin),
    db: Session = Depends(get_db),
):
    provider = get_channel_provider(db, user.tenant_id)
    value = provider.connection_status()
    provider.account.connection_state = str(value.get("state") or "unknown")
    provider.account.last_checked_at = utcnow()
    db.commit()
    return {
        **value,
        "instance_name": provider.account.instance_name,
        "webhook_url": (
            settings.evolution_webhook_url if provider.provider_name == "evolution" else None
        ),
        "qr_code": None,
        "message": value.get("message"),
    }


@router.post("/whatsapp/connect", response_model=WhatsAppConnectionResponse)
def whatsapp_connect(
    user: User = Depends(_require_integration_admin),
    db: Session = Depends(get_db),
):
    try:
        result = connect_evolution()
        provider = get_channel_provider(db, user.tenant_id)
        provider.account.connection_state = result.state
        provider.account.last_checked_at = utcnow()
        db.commit()
        return result.to_dict()
    except WhatsAppError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
