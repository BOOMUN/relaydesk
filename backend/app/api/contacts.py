from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..actions import ActionContext, ActionExecutionError, propose_action
from ..contact_attributes import public_contact_attributes
from ..database import get_db
from ..dependencies import get_current_user
from ..models import ActionStatus, Contact, User
from ..schemas import ContactSummary, ContactUpdate


router = APIRouter(prefix="/api/contacts", tags=["contacts"])


def _run_contact_action(db: Session, user: User, name: str, arguments: dict) -> None:
    try:
        execution = propose_action(
            db,
            ActionContext.for_user(user),
            name,
            arguments,
        )
    except ActionExecutionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if execution.status != ActionStatus.SUCCEEDED.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": execution.error_code or execution.status,
                "message": execution.failure_reason or "Action failed",
                "action_execution_id": execution.id,
            },
        )


@router.get("", response_model=list[ContactSummary])
def list_contacts(
    search: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(Contact).where(Contact.tenant_id == user.tenant_id).order_by(Contact.updated_at.desc())
    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(
            or_(Contact.display_name.ilike(pattern), Contact.phone.ilike(pattern), Contact.wa_id.ilike(pattern))
        )
    return db.scalars(statement).all()


@router.patch("/{contact_id}", response_model=ContactSummary)
def update_contact(
    contact_id: int,
    payload: ContactUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    contact = db.get(Contact, contact_id)
    if contact is None or contact.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="联系人不存在")
    changes = payload.model_dump(exclude_unset=True)
    profile = {
        key: changes[key]
        for key in ("display_name", "language")
        if key in changes
    }
    if profile:
        _run_contact_action(
            db,
            user,
            "contact.update_profile",
            {"contact_id": contact.id, **profile},
        )
    if "tags" in changes:
        current_tags = {value.casefold(): value for value in contact.tags or []}
        requested_tags = {
            str(value).strip().casefold(): str(value).strip()
            for value in changes["tags"] or []
            if str(value).strip()
        }
        additions = [value for key, value in requested_tags.items() if key not in current_tags]
        removals = [value for key, value in current_tags.items() if key not in requested_tags]
        if additions:
            _run_contact_action(
                db,
                user,
                "contact.tags.add",
                {"contact_id": contact.id, "tags": additions},
            )
        if removals:
            _run_contact_action(
                db,
                user,
                "contact.tags.remove",
                {"contact_id": contact.id, "tags": removals},
            )
    if "custom_attributes" in changes:
        current_fields = public_contact_attributes(contact.custom_attributes)
        requested_fields = dict(changes["custom_attributes"] or {})
        fields_to_set = {
            key: value
            for key, value in requested_fields.items()
            if current_fields.get(key) != value
        }
        keys_to_remove = [key for key in current_fields if key not in requested_fields]
        if fields_to_set:
            _run_contact_action(
                db,
                user,
                "contact.custom_fields.set",
                {"contact_id": contact.id, "fields": fields_to_set},
            )
        if keys_to_remove:
            _run_contact_action(
                db,
                user,
                "contact.custom_fields.remove",
                {"contact_id": contact.id, "keys": keys_to_remove},
            )
    db.refresh(contact)
    return contact
