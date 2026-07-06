"""pricing-svc: the downstream service that checkout calls.

Has a ~10% tail-latency profile (~1.5s) so calls have a heavy tail. This is
the "live cluster state" the verifier targets — mirrord steers checkout's HTTP
call here. The verifier classifies a checkout patch by whether it survives
this latency profile, not by what's in the code.
"""

from __future__ import annotations

import random
import time

from flask import Flask, jsonify

app = Flask(__name__)

# Deterministic seed so successive replay runs see the same latency pattern,
# which is what makes baseline-vs-patched comparison fair.
_rng = random.Random(42)


@app.route("/price/<item_id>")
def price(item_id: str):
    base_latency_s = 0.020 + _rng.random() * 0.020
    is_tail = _rng.random() < 0.10
    time.sleep(base_latency_s + (1.5 if is_tail else 0.0))
    return jsonify({"price": 9.99 + hash(item_id) % 100 / 10})


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
