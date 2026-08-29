from backend.app.services.delivery import (
    merge_delivery_status,
    normalize_evolution_delivery_status,
)


def test_baileys_numeric_and_named_delivery_statuses_are_normalized():
    assert normalize_evolution_delivery_status(0) == "failed"
    assert normalize_evolution_delivery_status(1) == "pending"
    assert normalize_evolution_delivery_status(2) == "sent"
    assert normalize_evolution_delivery_status(3) == "delivered"
    assert normalize_evolution_delivery_status(4) == "read"
    assert normalize_evolution_delivery_status(5) == "played"
    assert normalize_evolution_delivery_status("SERVER_ACK") == "sent"
    assert normalize_evolution_delivery_status("delivery-ack") == "delivered"
    assert normalize_evolution_delivery_status("unknown-provider-state") is None


def test_late_or_out_of_order_receipts_never_downgrade_success():
    assert merge_delivery_status("read", "sent") == "read"
    assert merge_delivery_status("delivered", "failed") == "delivered"
    assert merge_delivery_status("failed", "delivered") == "delivered"
