# sample-app: checkout

Toy Flask service with a planted bug. Used as the target workload for the
verifier scaffold.

## The bug

`app/checkout.py` calls `fetch_price` with no timeout. The pricing client has a
~10% tail-latency profile (~1.5s). Result: checkout p99 explodes, error rate
stays at 0% because nothing errors — it just hangs. This is the alert pattern
the scenario emulates.

## The fix the AI-SRE is expected to find

Pass `timeout_ms=200` to `fetch_price`. The pricing client already supports
the argument; the bug is just that the caller didn't use it.

## Local usage

```
python -m app smoke      # one request
python -m app replay     # 100 requests, emits VERIFIER_METRICS=<json>
python -m app serve      # Flask on :8080 (for deploy)
```

## Deploy to a real cluster (for the 6-week build)

```
docker build -t ghcr.io/metalbear-co/mirrord-sre-verifier-sample:dev .
docker push ghcr.io/metalbear-co/mirrord-sre-verifier-sample:dev
kubectl apply -f k8s/deployment.yaml
```

Once deployed, point Datadog at the service, configure a p99 monitor, and
configure the monitor webhook at `https://<verifier-host>/webhook/datadog`.
