"""Checkout endpoint.

PLANTED BUG: `fetch_price` is called with no client-side timeout. The pricing
service has a 10% tail-latency profile (~1.5s per affected call), so any
request that lands on a tail call inherits the full 1.5s wait. With even
modest traffic, p99 latency drifts well above SLO and the alert fires.
"""

from __future__ import annotations

from .pricing import fetch_price


def checkout(item_id: str, qty: int) -> dict:
    # BUG: no timeout on the downstream pricing call.
    price = fetch_price(item_id)
    return {"item_id": item_id, "qty": qty, "total": price * qty}
