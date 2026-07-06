"""In-process pricing simulator. Only used when PRICING_URL is unset.

Kept for the offline `make demo` path so the scaffold runs without a cluster.
Real verification runs hit `pricing-svc` in-cluster — that is where the
"requires real cluster state" claim becomes literally true.
"""

from __future__ import annotations

import random
import time


# Deterministic seed so 'baseline' and 'patched' see the same latency pattern.
_rng = random.Random(42)


def fetch_price_local(item_id: str, timeout_ms: int | None = None) -> float:
    base_latency_s = 0.020 + _rng.random() * 0.020
    is_tail = _rng.random() < 0.10
    actual_latency_s = base_latency_s + (1.5 if is_tail else 0.0)

    if timeout_ms is not None and actual_latency_s * 1000 > timeout_ms:
        time.sleep(timeout_ms / 1000)
        raise TimeoutError(f"pricing call exceeded {timeout_ms}ms")

    time.sleep(actual_latency_s)
    return 9.99 + hash(item_id) % 100 / 10
