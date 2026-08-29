from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class InboxSignal:
    activity: str
    conversation_id: int | None = None
    sender_type: str | None = None


@dataclass(frozen=True)
class InboxSubscription:
    tenant_id: int
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[InboxSignal]


_subscriptions: dict[int, list[InboxSubscription]] = {}
_subscriptions_lock = Lock()


def subscribe_inbox(tenant_id: int) -> InboxSubscription:
    subscription = InboxSubscription(
        tenant_id=tenant_id,
        loop=asyncio.get_running_loop(),
        queue=asyncio.Queue(maxsize=32),
    )
    with _subscriptions_lock:
        _subscriptions.setdefault(tenant_id, []).append(subscription)
    return subscription


def unsubscribe_inbox(subscription: InboxSubscription) -> None:
    with _subscriptions_lock:
        tenant_subscriptions = _subscriptions.get(subscription.tenant_id)
        if not tenant_subscriptions:
            return
        try:
            tenant_subscriptions.remove(subscription)
        except ValueError:
            return
        if not tenant_subscriptions:
            _subscriptions.pop(subscription.tenant_id, None)


def _enqueue_signal(
    queue: asyncio.Queue[InboxSignal],
    signal: InboxSignal,
) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(signal)


def publish_inbox_updated(
    tenant_id: int,
    *,
    activity: str,
    conversation_id: int | None = None,
    sender_type: str | None = None,
) -> None:
    signal = InboxSignal(
        activity=activity,
        conversation_id=conversation_id,
        sender_type=sender_type,
    )
    with _subscriptions_lock:
        subscriptions = tuple(_subscriptions.get(tenant_id, ()))
    for subscription in subscriptions:
        try:
            subscription.loop.call_soon_threadsafe(
                _enqueue_signal,
                subscription.queue,
                signal,
            )
        except RuntimeError:
            # The stream cleanup will remove subscriptions whose loop closed.
            continue
