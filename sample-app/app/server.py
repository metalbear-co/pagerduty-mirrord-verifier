"""Flask server with Prometheus instrumentation.

Exposes /checkout, /healthz, /metrics. The histogram on request duration is
what the Prometheus alert rule queries; when p99 exceeds threshold, the alert
fires and Alertmanager webhooks the verifier.
"""

from __future__ import annotations

import os
import time

from flask import Flask, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .checkout import checkout

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("checkout")

app = Flask(__name__)

REQUEST_DURATION = Histogram(
    "checkout_request_duration_seconds",
    "checkout HTTP request latency",
    buckets=(0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0),
)
REQUEST_ERRORS = Counter(
    "checkout_request_errors_total",
    "checkout 5xx responses by exception type",
    ["exception"],
)
REQUEST_TOTAL = Counter(
    "checkout_requests_total",
    "checkout requests by status class",
    ["status"],
)


@app.route("/checkout", methods=["POST"])
def post_checkout():
    t0 = time.perf_counter()
    try:
        body = request.get_json(force=True)
        result = checkout(item_id=body["item_id"], qty=int(body["qty"]))
        REQUEST_TOTAL.labels(status="ok").inc()
        return jsonify(result)
    except Exception as e:
        log.exception("checkout failed for body=%s", body if 'body' in dir() else '?')
        REQUEST_ERRORS.labels(exception=type(e).__name__).inc()
        REQUEST_TOTAL.labels(status="error").inc()
        raise
    finally:
        REQUEST_DURATION.observe(time.perf_counter() - t0)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


def run() -> None:
    port = int(os.environ.get("PORT", "8080"))
    # threaded=True so the verifier's concurrent load driver doesn't queue
    # requests on this single-threaded Flask dev server (would inflate p99
    # artificially with queueing delay rather than real work).
    app.run(host="0.0.0.0", port=port, threaded=True)
