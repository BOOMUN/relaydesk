from __future__ import annotations

from langchain_core.tools import tool


@tool
def lookup_order(order_reference: str, customer_phone: str) -> dict[str, str | bool]:
    """Look up a customer's order after an order reference and phone are available."""

    reference = order_reference.strip().upper()
    if reference == "ORD-1001":
        return {
            "found": True,
            "order_reference": reference,
            "status": "已发货",
            "carrier": "顺丰",
            "eta": "预计两个工作日内送达",
            "verified_by": "whatsapp_phone",
        }
    return {
        "found": False,
        "order_reference": reference,
        "message": "没有找到对应订单，请检查订单号或转交人工客服。",
    }


ORDER_LOOKUP_TOOL = lookup_order
