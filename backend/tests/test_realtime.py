from __future__ import annotations

import asyncio

from backend.app.services.realtime import (
    publish_inbox_updated,
    subscribe_inbox,
    unsubscribe_inbox,
)


def test_message_activity_wakes_realtime_subscriber_without_polling() -> None:
    async def scenario():
        subscription = subscribe_inbox(tenant_id=42)
        try:
            publish_inbox_updated(
                42,
                activity="message",
                conversation_id=7,
                sender_type="customer",
            )
            return await asyncio.wait_for(subscription.queue.get(), timeout=0.2)
        finally:
            unsubscribe_inbox(subscription)

    signal = asyncio.run(scenario())
    assert signal.activity == "message"
    assert signal.conversation_id == 7
    assert signal.sender_type == "customer"
