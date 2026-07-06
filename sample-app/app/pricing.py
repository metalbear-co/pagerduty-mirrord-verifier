"""Pricing client. HTTP call to `pricing-svc` in the cluster.

When PRICING_URL is set (production / cluster-targeted verifier runs), this
makes a real HTTP call and the latency comes from the in-cluster pricing pod.
When PRICING_URL is unset (local `make demo`), falls back to an in-process
simulator so the scaffold still runs without a cluster.
"""

from __future__ import annotations

import os

import httpx

from ._pricing_local import fetch_price_local


class PricingError(Exception):
    pass


def fetch_price(item_id: str, timeout_ms: int | None = None) -> float:
    """Returns price. Raises PricingError on timeout or downstream error."""
    url = os.environ.get("PRICING_URL")
    if not url:
        try:
            return fetch_price_local(item_id, timeout_ms)
        except TimeoutError as e:
            raise PricingError(str(e)) from e

    timeout_s = timeout_ms / 1000 if timeout_ms else None
    try:
        r = httpx.get(f"{url.rstrip('/')}/price/{item_id}", timeout=timeout_s)
        r.raise_for_status()
        return float(r.json()["price"])
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        raise PricingError(str(e)) from e
