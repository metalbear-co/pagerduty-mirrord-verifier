"""FastAPI webhook receiver. Accepts PagerDuty V3 webhook payloads, kicks off verification."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# uvicorn doesn't configure the root logger, so our module loggers go nowhere
# by default. Wire them to stdout at INFO so engine/preview diagnostics show
# up in `kubectl logs`.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .engine import VerificationEngine
from .models import Alert, PreviewArtifact, Severity, Slo, SloOperator, SloSignal, VerificationResult
from .poster import PagerDutyIncidentNotePoster, Poster, StdoutPoster

log = logging.getLogger("verifier.webhook")

app = FastAPI(title="mirrord-sre-verifier", version="0.1.0")

# INTEGRATION: in production these are constructed per-tenant from config.
# Lazy so importing this module doesn't require ANTHROPIC_API_KEY.
_orchestrator = None
_engine: VerificationEngine | None = None
_poster: StdoutPoster | None = None


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        from .orchestrator import AISREOrchestrator

        _orchestrator = AISREOrchestrator()
    return _orchestrator


def _get_engine() -> VerificationEngine:
    global _engine
    if _engine is None:
        _engine = VerificationEngine()
    return _engine


def _get_poster() -> Poster:
    global _poster
    if _poster is None:
        if os.environ.get("PAGERDUTY_REST_API_KEY"):
            _poster = PagerDutyIncidentNotePoster()
            log.info("poster: PagerDutyIncidentNotePoster")
        else:
            _poster = StdoutPoster()
            log.info("poster: StdoutPoster (PAGERDUTY_REST_API_KEY unset)")
    return _poster


def _verify_pagerduty_signature(raw_body: bytes, header: str | None) -> None:
    """PagerDuty V3 webhook signature check.

    Header shape: `v1=<hex>[,v1=<hex>...]` (multiple during rotation).
    Body must be verified byte-for-byte, so this runs BEFORE FastAPI json-parses.
    """
    secret = os.environ.get("PAGERDUTY_WEBHOOK_SIGNING_SECRET")
    if not secret:
        log.warning("PAGERDUTY_WEBHOOK_SIGNING_SECRET unset; skipping signature check")
        return
    if not header:
        raise HTTPException(status_code=401, detail="missing x-pagerduty-signature")

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    presented = [p.split("=", 1)[1] for p in header.split(",") if p.startswith("v1=")]
    if not any(hmac.compare_digest(expected, p) for p in presented):
        raise HTTPException(status_code=401, detail="signature mismatch")


def _derive_slo_from_title(title: str) -> Slo | None:
    """Best-effort SLO extraction from a PagerDuty incident title.

    Demo-quality: matches the incident-title conventions we use in the demo
    ('checkout p99 above SLO' → p99>300ms). In production the SLO would be
    resolved from the alert-rule metadata linked to the incident, not the
    human-readable title.
    """
    t = title.lower()
    if "slo" not in t:
        return None
    if "p99" in t:
        return Slo(signal=SloSignal.P99_MS, operator=SloOperator.GT, threshold=300.0)
    if "p50" in t:
        return Slo(signal=SloSignal.P50_MS, operator=SloOperator.GT, threshold=200.0)
    if "error rate" in t or "error_rate" in t:
        return Slo(signal=SloSignal.ERROR_RATE, operator=SloOperator.GT, threshold=0.05)
    return None


def parse_pagerduty_v3_payload(payload: dict[str, Any]) -> Alert | None:
    """Map a PagerDuty V3 webhook body into our Alert shape.

    PD V3 webhook envelope:
      { "event": { "event_type": "incident.triggered" | "incident.annotated",
                   "data": { "id": "P123ABC", "title": "...", "service": {...} } } }

    Returns None for events we don't act on (e.g. annotations we posted ourselves).
    """
    event = payload.get("event") or {}
    et = event.get("event_type", "")
    if et not in {"incident.triggered", "incident.annotated"}:
        return None

    data = event.get("data") or {}
    incident_id = data.get("id")
    if not incident_id:
        raise HTTPException(status_code=400, detail="missing event.data.id")

    # Skip annotations that carry our own signature to avoid feedback loops.
    if et == "incident.annotated":
        content = (data.get("content") or "").lower()
        if "mirrord verification" in content:
            log.info("skipping self-annotation on incident %s", incident_id)
            return None

    service = (data.get("service") or {}).get("summary", "unknown")
    try:
        fired_at = datetime.fromisoformat(
            (data.get("created_at") or event.get("occurred_at", "")).replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except ValueError:
        fired_at = datetime.utcnow()

    # PagerDuty webhook payloads don't carry the service's code location. For
    # the demo, resolve it from VERIFIER_SAMPLE_REPO. In production this would
    # come from a service→repo catalog lookup keyed on the PD service id.
    sample_repo = os.environ.get("VERIFIER_SAMPLE_REPO")

    title = data.get("title") or f"PagerDuty incident {incident_id}"
    return Alert(
        id=incident_id,
        title=title,
        body=data.get("description") or title,
        severity=Severity.ERROR,
        service=service,
        fired_at=fired_at,
        source="pagerduty",
        repo_path=Path(sample_repo) if sample_repo else None,
        slo=_derive_slo_from_title(title),
        raw=payload,
    )


def parse_datadog_payload(payload: dict[str, Any]) -> Alert:
    """Map a Datadog webhook body into our Alert shape.

    Datadog's payload schema is configurable per-monitor; this maps the default
    @webhook-* template fields documented at
    https://docs.datadoghq.com/integrations/webhooks/. INTEGRATION: tighten to
    whatever template the production monitors use.
    """
    try:
        return Alert(
            id=str(payload.get("id") or payload.get("alert_id") or payload["event_id"]),
            title=payload.get("title") or payload["alert_title"],
            body=payload.get("body") or payload.get("event_msg", ""),
            severity=Severity(payload.get("alert_transition", "error").lower())
            if payload.get("alert_transition", "").lower() in {s.value for s in Severity}
            else Severity.ERROR,
            service=payload.get("service") or payload.get("tags", {}).get("service", "unknown"),
            metric=payload.get("metric"),
            threshold=payload.get("alert_threshold"),
            observed=payload.get("last_value"),
            tags=payload.get("tags", {}) if isinstance(payload.get("tags"), dict) else {},
            fired_at=datetime.utcnow(),
            repo_path=Path(payload["repo_path"]) if payload.get("repo_path") else None,
            raw=payload,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Unparseable alert: {e}") from e


def parse_alertmanager_payload(payload: dict[str, Any]) -> list[Alert]:
    """Map an Alertmanager v4 webhook body into Alerts.

    AM bundles multiple alerts in one POST. We return one Alert per firing
    item. The verifier only acts on `status=firing` — resolved alerts skip the
    pipeline. AM webhook schema:
    https://prometheus.io/docs/alerting/latest/configuration/#webhook_config

    The repo location and mirrord target are read from the rule's annotations:
      repo_path, verifier_target, verifier_namespace
    """
    if payload.get("version") not in (None, "4"):
        raise HTTPException(status_code=400, detail=f"unsupported AM version {payload.get('version')}")

    out: list[Alert] = []
    for item in payload.get("alerts", []):
        if item.get("status") != "firing":
            continue
        labels = item.get("labels") or {}
        annotations = item.get("annotations") or {}
        try:
            fired_at = datetime.fromisoformat(item["startsAt"].replace("Z", "+00:00")).replace(tzinfo=None)
        except (KeyError, ValueError):
            fired_at = datetime.utcnow()

        out.append(Alert(
            id=item.get("fingerprint") or f"{labels.get('alertname')}:{labels.get('service')}",
            title=annotations.get("summary") or labels.get("alertname", "alert"),
            body=annotations.get("description", ""),
            severity=Severity(labels["severity"].lower())
            if labels.get("severity", "").lower() in {s.value for s in Severity}
            else Severity.ERROR,
            service=labels.get("service", "unknown"),
            metric=labels.get("alertname"),
            tags=labels,
            fired_at=fired_at,
            source="alertmanager",
            repo_path=Path(annotations["repo_path"]) if annotations.get("repo_path") else None,
            target=annotations.get("verifier_target"),
            namespace=annotations.get("verifier_namespace"),
            raw=item,
        ))
    return out


@app.post("/webhook/pagerduty")
async def pagerduty_webhook(
    request: Request,
    bg: BackgroundTasks,
    x_pagerduty_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = await request.body()
    _verify_pagerduty_signature(raw, x_pagerduty_signature)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e

    alert = parse_pagerduty_v3_payload(payload)
    if alert is None:
        return {"status": "ignored", "reason": "event_type not actionable"}
    log.info("alert received src=pagerduty incident=%s service=%s", alert.id, alert.service)
    bg.add_task(_run_pipeline, alert)
    return {"status": "accepted", "incident_id": alert.id}


@app.post("/webhook/datadog")
async def datadog_webhook(payload: dict[str, Any], bg: BackgroundTasks) -> dict[str, str]:
    alert = parse_datadog_payload(payload)
    log.info("alert received src=datadog id=%s service=%s", alert.id, alert.service)
    bg.add_task(_run_pipeline, alert)
    return {"status": "accepted", "alert_id": alert.id}


@app.post("/webhook/alertmanager")
async def alertmanager_webhook(payload: dict[str, Any], bg: BackgroundTasks) -> dict[str, Any]:
    alerts = parse_alertmanager_payload(payload)
    log.info("alertmanager batch: %d firing alerts", len(alerts))
    accepted = []
    for alert in alerts:
        bg.add_task(_run_pipeline, alert)
        accepted.append(alert.id)
    return {"status": "accepted", "alert_ids": accepted}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug")
async def debug() -> dict[str, Any]:
    """Quick pod introspection. Useful when `kubectl exec` is restricted."""
    import shutil as _sh
    import subprocess as _sp
    import sys as _sys

    def _safe_run(argv: list[str]) -> dict[str, Any]:
        try:
            p = _sp.run(argv, capture_output=True, text=True, timeout=15)
            return {"exit": p.returncode, "stdout": p.stdout[-2000:], "stderr": p.stderr[-2000:]}
        except Exception as e:
            return {"error": repr(e)}

    return {
        "python": _sys.executable,
        "mirrord_path": _sh.which("mirrord"),
        "mirrord_version": _safe_run(["mirrord", "--version"]),
        "scaffold_sample_ls": _safe_run(["ls", "-la", "/scaffold/sample-app/app"]),
        "env_relevant": {
            k: v for k, v in os.environ.items()
            if k in {"PATH", "PYTHONPATH", "PRICING_URL", "VERIFIER_MODEL",
                     "MIRRORD_TARGET", "MIRRORD_NAMESPACE", "VERIFIER_SAMPLE_REPO"}
        },
    }


def _load_prepared_patch(path: str):
    """Load a prepared Patch from a scenario JSON file. Used by the demo to make
    the PASS run deterministic (skips the stochastic Claude call).
    """
    import json as _json

    from .models import Patch, PatchFile

    with open(path) as f:
        blob = _json.load(f)
    pp = blob["prepared_patch"]
    return Patch(
        summary=pp["summary"],
        hypothesis=pp["hypothesis"],
        repro_steps=pp.get("repro_steps", []),
        files=[PatchFile(**f) for f in pp["files"]],
        confidence=pp.get("confidence", 0.9),
        expected_signal_change=pp.get("expected_signal_change", ""),
        model=pp.get("model", "hand-crafted"),
    )


async def _run_pipeline(alert: Alert) -> None:
    """Two-stage flow: exec-verify (always) → preview env (only on PASS)."""
    try:
        prepared = os.environ.get("PREPARED_PATCH_FILE")
        if prepared:
            log.info("using prepared patch: %s", prepared)
            patch = _load_prepared_patch(prepared)
        else:
            patch = await asyncio.to_thread(_get_orchestrator().propose_patch, alert)
        bundle = await asyncio.to_thread(_get_engine().verify, alert, patch)

        if bundle.result == VerificationResult.PASS:
            try:
                bundle.preview = await asyncio.to_thread(_build_preview, alert, patch)
            except Exception:
                # Don't fail the pipeline on stage-2 errors — the stage-1 verdict
                # is still useful, and preview is the human-facing nice-to-have.
                log.exception("stage 2 (preview env) failed; posting stage-1 only")

        await asyncio.to_thread(_get_poster().post, bundle)
    except Exception:
        log.exception("pipeline failed for alert %s", alert.id)


def _build_preview(alert: Alert, patch) -> PreviewArtifact:
    """Stage 2: re-apply the patch to a fresh dir, build image, start preview.

    Re-applying (vs. reusing the temp dir from stage 1) keeps the engine
    cleanly scoped to verification — stage 2 doesn't need to coordinate
    with stage 1's tempfile lifecycle.
    """
    import shutil
    import tempfile
    import time as _t
    from pathlib import Path as _Path

    from .engine import VerificationEngine as _Engine
    from .preview import PreviewBuilder

    if alert.target is None or alert.namespace is None:
        raise RuntimeError("alert must carry target+namespace for stage 2 preview")

    builder = PreviewBuilder()
    session_id = f"verify-{alert.id[:12].replace(':', '-')}-{int(_t.time())}".lower()

    with tempfile.TemporaryDirectory(prefix="verifier-stage2-") as tmp:
        patched_dir = _Path(tmp) / "patched"
        shutil.copytree(alert.repo_path, patched_dir)
        # Reuse the engine's patch application logic. Static method would be
        # cleaner; for now we instantiate.
        _Engine()._apply_patch(patched_dir, patch)
        info = builder.build_and_start(
            patched_dir=patched_dir,
            session_id=session_id,
            target=alert.target,
            namespace=alert.namespace,
        )

    return PreviewArtifact(
        env_key=info.env_key,
        image=info.image,
        target=info.target,
        namespace=info.namespace,
        ttl_minutes=info.ttl_minutes,
        service_hostname=info.service_hostname,
        curl_recipe=info.curl_recipe(),
    )
