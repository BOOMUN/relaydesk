from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ChannelAccount
from .base import ChannelProvider, ChannelProviderError
from .demo import DemoChannelProvider
from .evolution import EvolutionChannelProvider
from .meta import MetaCloudChannelProvider


DEFAULT_CAPABILITIES = {
    "demo": ["text", "template", "buttons", "list", "webhook"],
    "meta": ["text", "template", "template_sync", "buttons", "list", "webhook"],
    "evolution": ["text", "buttons", "list", "webhook", "delivery_reconcile"],
}


def ensure_default_channel_account(db: Session, tenant_id: int) -> ChannelAccount:
    account = db.scalar(
        select(ChannelAccount)
        .where(
            ChannelAccount.tenant_id == tenant_id,
            ChannelAccount.is_default.is_(True),
        )
        .order_by(ChannelAccount.id)
    )
    provider = settings.whatsapp_provider
    if account is None:
        external_id = (
            settings.meta_phone_number_id
            if provider == "meta"
            else settings.evolution_instance_name
            if provider == "evolution"
            else "demo"
        )
        account = ChannelAccount(
            tenant_id=tenant_id,
            provider=provider,
            name=f"WhatsApp {provider.title()}",
            external_account_id=external_id or provider,
            phone_number_id=settings.meta_phone_number_id if provider == "meta" else None,
            business_account_id=(
                settings.meta_business_account_id if provider == "meta" else None
            ),
            instance_name=(
                settings.evolution_instance_name if provider == "evolution" else None
            ),
            capabilities=DEFAULT_CAPABILITIES[provider],
            is_default=True,
            is_active=True,
        )
        db.add(account)
        db.flush()
        return account
    if account.provider != provider and account.credentials_reference == "environment":
        account.is_default = False
        replacement = db.scalar(
            select(ChannelAccount).where(
                ChannelAccount.tenant_id == tenant_id,
                ChannelAccount.provider == provider,
                ChannelAccount.credentials_reference == "environment",
            )
        )
        if replacement is None:
            replacement = ChannelAccount(
                tenant_id=tenant_id,
                provider=provider,
                name=f"WhatsApp {provider.title()}",
                external_account_id=(
                    settings.meta_phone_number_id
                    if provider == "meta"
                    else settings.evolution_instance_name
                    if provider == "evolution"
                    else "demo"
                )
                or provider,
                phone_number_id=settings.meta_phone_number_id if provider == "meta" else None,
                business_account_id=(
                    settings.meta_business_account_id if provider == "meta" else None
                ),
                instance_name=(
                    settings.evolution_instance_name if provider == "evolution" else None
                ),
                capabilities=DEFAULT_CAPABILITIES[provider],
                is_active=True,
            )
            db.add(replacement)
        replacement.is_default = True
        db.flush()
        account = replacement
    return account


def get_channel_account(
    db: Session,
    tenant_id: int,
    channel_account_id: int | None = None,
) -> ChannelAccount:
    if channel_account_id is None:
        account = ensure_default_channel_account(db, tenant_id)
    else:
        account = db.get(ChannelAccount, channel_account_id)
        if account is None or account.tenant_id != tenant_id:
            raise ChannelProviderError(
                "Channel account does not exist", code="channel_account_not_found"
            )
    if not account.is_active:
        raise ChannelProviderError(
            "Channel account is disabled", code="channel_account_disabled"
        )
    return account


def provider_for_account(account: ChannelAccount) -> ChannelProvider:
    if account.provider == "meta":
        return MetaCloudChannelProvider(account)
    if account.provider == "evolution":
        return EvolutionChannelProvider(account)
    if account.provider == "demo":
        return DemoChannelProvider(account)
    raise ChannelProviderError(
        f"Unsupported channel provider: {account.provider}",
        code="unsupported_provider",
    )


def get_channel_provider(
    db: Session,
    tenant_id: int,
    channel_account_id: int | None = None,
) -> ChannelProvider:
    return provider_for_account(get_channel_account(db, tenant_id, channel_account_id))


def find_webhook_account(
    db: Session,
    provider: str,
    account_key: str,
) -> ChannelAccount | None:
    conditions = [
        ChannelAccount.provider == provider,
        ChannelAccount.is_active.is_(True),
    ]
    if provider == "meta":
        conditions.append(ChannelAccount.phone_number_id == account_key)
    elif provider == "evolution":
        conditions.append(ChannelAccount.instance_name == account_key)
    else:
        conditions.append(ChannelAccount.external_account_id == account_key)
    matches = db.scalars(
        select(ChannelAccount).where(*conditions).order_by(ChannelAccount.id).limit(2)
    ).all()
    # A provider account key must identify exactly one tenant. Silently using
    # the first match would route one business' customer data into another.
    return matches[0] if len(matches) == 1 else None
