"""Shared types passed between webhook → orchestrator → engine → poster."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class SloSignal(str, Enum):
    """Names of metrics from RunMetrics that an alert's SLO can refer to."""

    P50_MS = "p50_ms"
    P99_MS = "p99_ms"
    ERROR_RATE = "error_rate"


class SloOperator(str, Enum):
    """Comparison the alert fires on. Alert fires when (signal OP threshold) is true."""

    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="


class Slo(BaseModel):
    """The alert's actual condition: the verifier checks this against the patched run.

    Example: CheckoutP99High fires when p99 > 0.3s — so signal=P99_MS, op=GT, threshold=300.
    The verifier wants to know whether (patched_signal OP threshold) is still true.
    """

    signal: SloSignal
    operator: SloOperator
    threshold: float


class Alert(BaseModel):
    """Normalized alert shape. Datadog webhook payloads are mapped into this."""

    id: str
    title: str
    body: str
    severity: Severity
    service: str
    metric: str | None = None
    threshold: float | None = None
    observed: float | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    fired_at: datetime
    source: str = "datadog"
    # Where to find the candidate code to patch. In production this is resolved
    # from service catalog; in the scaffold it is provided by the scenario.
    repo_path: Path | None = None
    # mirrord target + namespace to run baseline/patched against. Resolved per
    # alert (Prometheus rule annotations / Datadog tags); fall back to env.
    target: str | None = None
    namespace: str | None = None
    # The alert's actual SLO condition. The verifier evaluates this against the
    # patched run to decide whether the alert would still fire post-merge. If
    # absent, the classifier falls back to hardcoded relative-improvement rules.
    slo: Slo | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class PatchFile(BaseModel):
    path: str
    before: str
    after: str


class Patch(BaseModel):
    """Structured output from the AI-SRE orchestrator."""

    summary: str
    hypothesis: str
    repro_steps: list[str]
    files: list[PatchFile]
    confidence: float = Field(ge=0.0, le=1.0)
    expected_signal_change: str
    model: str


class RunMetrics(BaseModel):
    """One run's observed metrics. Comes from sample-app instrumentation."""

    duration_ms_p50: float
    duration_ms_p99: float
    error_rate: float
    request_count: int
    raw_log: str = ""


class VerificationResult(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    INCONCLUSIVE = "inconclusive"


class PreviewArtifact(BaseModel):
    """Stage 2 deliverable: a live preview env for human inspection."""

    env_key: str
    image: str
    target: str
    namespace: str
    ttl_minutes: int
    service_hostname: str
    curl_recipe: str


class ProofBundle(BaseModel):
    """The receipt. What gets posted back to the incident."""

    alert: Alert
    patch: Patch
    baseline: RunMetrics
    patched: RunMetrics
    result: VerificationResult
    rationale: str
    preview: PreviewArtifact | None = None  # populated only on stage-1 PASS
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    def as_markdown(self) -> str:
        from .proof_bundle import render_markdown

        return render_markdown(self)
