from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..channels import (
    ensure_default_channel_account,
    find_webhook_account,
    provider_for_account,
)
from ..channels.types import ChannelEvent
from ..config import settings
from ..database import SessionLocal
from ..models import ChannelAccount, ChannelWebhookEvent, Tenant, utcnow
from ..services.conversations import receive_inbound
from ..services.delivery import record_delivery_receipt


router = APIRouter(tags=["webhooks"])


@router.get("/api/webhooks/whatsapp", response_class=PlainTextResponse)
def verify_webhook(
    mode: str = Query(alias="hub.mode"),
    token: str = Query(alias="hub.verify_token"),
    challenge: str = Query(alias="hub.challenge"),
):
    if mode != "subscribe" or token != settings.meta_verify_token:
        raise HTTPException(status_code=403, detail="Webhook verification failed")
    return challenge


def _meta_account_keys(payload: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []):
            value = change.get("value") if isinstance(change, dict) else None
            metadata = value.get("metadata") if isinstance(value, dict) else None
            key = str((metadata or {}).get("phone_number_id") or "").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def _single_nonproduction_account(db, provider: str) -> ChannelAccount | None:
    if settings.environment == "production":
        return None
    matches = db.scalars(
        select(ChannelAccount)
        .where(ChannelAccount.is_active.is_(True))
        .order_by(ChannelAccount.id)
        .limit(2)
    ).all()
    if len(matches) != 1:
        return None
    account = matches[0]
    if settings.whatsapp_provider == provider and account.provider != provider:
        account = ensure_default_channel_account(db, account.tenant_id)
        db.commit()
    return account


def _resolve_account(db, provider: str, account_key: str) -> ChannelAccount | None:
    account = find_webhook_account(db, provider, account_key) if account_key else None
    return account or _single_nonproduction_account(db, provider)


def _parse_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    return payload


def _event_payload(event: ChannelEvent) -> dict[str, Any]:
    return {
        "sender_id": event.sender_id,
        "sender_address": event.sender_address,
        "display_name": event.display_name,
        "body": event.body,
        "content_type": event.content_type,
        "delivery_status": event.delivery_status,
        "occurred_at": event.occurred_at,
        "provider_payload": event.payload,
    }


def _persist_event(db, account: ChannelAccount, event: ChannelEvent) -> tuple[int, bool]:
    existing = db.scalar(
        select(ChannelWebhookEvent).where(
            ChannelWebhookEvent.provider == event.provider,
            ChannelWebhookEvent.account_key == event.account_key,
            ChannelWebhookEvent.event_key == event.event_key,
        )
    )
    if existing is not None:
        should_retry = existing.status in {"received", "failed"}
        if existing.status == "failed":
            existing.status = "received"
            existing.failure_reason = None
            db.commit()
        return existing.id, should_retry

    payload_json = _event_payload(event)
    payload_hash = hashlib.sha256(
        json.dumps(
            payload_json,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    record = ChannelWebhookEvent(
        tenant_id=account.tenant_id,
        channel_account_id=account.id,
        provider=event.provider,
        account_key=event.account_key,
        event_key=event.event_key,
        event_type=event.event_type,
        external_message_id=event.external_message_id,
        payload_hash=payload_hash,
        payload_json=payload_json,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(ChannelWebhookEvent).where(
                ChannelWebhookEvent.provider == event.provider,
                ChannelWebhookEvent.account_key == event.account_key,
                ChannelWebhookEvent.event_key == event.event_key,
            )
        )
        if existing is None:
            raise
        return existing.id, False
    db.refresh(record)
    return record.id, True


def _process_event(event_id: int) -> None:
    with SessionLocal() as db:
        claimed = db.execute(
            update(ChannelWebhookEvent)
            .where(
                ChannelWebhookEvent.id == event_id,
                ChannelWebhookEvent.status == "received",
            )
            .values(
                status="processing",
                attempt_count=ChannelWebhookEvent.attempt_count + 1,
                failure_reason=None,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if claimed.rowcount != 1:
            return
        record = db.get(ChannelWebhookEvent, event_id)
        if record is None or record.tenant_id is None or record.channel_account_id is None:
            return
        payload = dict(record.payload_json or {})
        try:
            if record.event_type == "status":
                if record.external_message_id and payload.get("delivery_status") is not None:
                    record_delivery_receipt(
                        db,
                        record.external_message_id,
                        payload["delivery_status"],
                    )
            else:
                sender_id = str(payload.get("sender_id") or "").strip()
                if not sender_id:
                    raise ValueError("Inbound webhook message has no sender ID")
                provider_metadata = dict(
                    (payload.get("provider_payload") or {}).get(
                        "_agentdesk_provider_metadata"
                    )
                    or {}
                )
                receive_inbound(
                    db,
                    tenant_id=record.tenant_id,
                    wa_id=sender_id,
                    phone=f"+{sender_id}" if sender_id.isdigit() else sender_id,
                    display_name=str(payload.get("display_name") or "WhatsApp customer"),
                    body=str(payload.get("body") or ""),
                    external_id=record.external_message_id,
                    content_type=str(payload.get("content_type") or "text"),
                    evolution_recipient_jid=provider_metadata.get("recipient_jid"),
                    channel_account_id=record.channel_account_id,
                    provider=record.provider,
                    sender_address=str(payload.get("sender_address") or sender_id),
                    provider_metadata=provider_metadata,
                )
            record = db.get(ChannelWebhookEvent, event_id)
            if record is not None:
                record.status = "processed"
                record.processed_at = utcnow()
                db.commit()
        except Exception as exc:
            db.rollback()
            record = db.get(ChannelWebhookEvent, event_id)
            if record is not None:
                record.status = "failed"
                record.failure_reason = str(exc)[:2000]
                db.commit()


def _accept_events(
    db,
    provider: str,
    raw_body: bytes,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> list[int]:
    initial_key = (
        (_meta_account_keys(payload) or [""])[0]
        if provider == "meta"
        else str(payload.get("instance") or "").strip()
    )
    initial_account = _resolve_account(db, provider, initial_key)
    if initial_account is None:
        raise HTTPException(status_code=404, detail="Unknown or ambiguous channel account")
    verifier = provider_for_account(initial_account)
    # Non-production compatibility accounts can be demo-backed, but webhook
    # parsing and verification must still use the requested provider.
    if verifier.provider_name != provider:
        account_provider = initial_account.provider
        initial_account.provider = provider
        try:
            verifier = provider_for_account(initial_account)
        finally:
            initial_account.provider = account_provider
    if not verifier.verify_webhook(raw_body, headers):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    resolved_events: list[tuple[ChannelEvent, ChannelAccount]] = []
    for event in verifier.parse_webhook(payload):
        account = _resolve_account(db, provider, event.account_key)
        if account is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown or ambiguous channel account: {event.account_key}",
            )
        resolved_events.append((event, account))

    accepted: list[int] = []
    for event, account in resolved_events:
        event_id, should_process = _persist_event(db, account, event)
        if should_process:
            accepted.append(event_id)
    return accepted


@router.post("/api/webhooks/whatsapp")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    payload = _parse_payload(raw_body)
    with SessionLocal() as db:
        event_ids = _accept_events(
            db,
            "meta",
            raw_body,
            {key.casefold(): value for key, value in request.headers.items()},
            payload,
        )
    for event_id in event_ids:
        background_tasks.add_task(_process_event, event_id)
    return {"received": True, "accepted_events": len(event_ids)}


@router.post("/api/webhooks/evolution")
async def receive_evolution_webhook(
    request: Request, background_tasks: BackgroundTasks
):
    raw_body = await request.body()
    payload = _parse_payload(raw_body)
    with SessionLocal() as db:
        event_ids = _accept_events(
            db,
            "evolution",
            raw_body,
            {key.casefold(): value for key, value in request.headers.items()},
            payload,
        )
    for event_id in event_ids:
        background_tasks.add_task(_process_event, event_id)
    return {"received": True, "accepted_events": len(event_ids)}
