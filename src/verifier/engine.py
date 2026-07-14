"""Verification orchestrator.

Stage 1 strategy: 'load_compare'
  1. Snapshot the candidate workload to a tempdir.
  2. Apply the AI-SRE's patch to a second tempdir.
  3. For each (baseline, patched):
       - Launch the candidate as a Flask server under `mirrord exec` so its
         outbound calls land at real in-cluster downstreams.
       - Wait for /healthz.
       - Drive N synthetic HTTP requests from this process, measure each.
       - Send SIGTERM, wait, collect stderr if useful.
  4. Compare metrics, classify PASS/REJECT, emit ProofBundle.

The application is just a server. All measurement lives here. Same shape as a
production load test, except the downstream is the real in-cluster pricing
service via mirrord's network steering.

Picking a port: each run binds 127.0.0.1:<port>. We pick a free port up-front
so baseline and patched can't collide if they overlap (they don't today, but
the tempdir/process pattern is easier to reason about if the port is explicit).
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx

from .mirrord_runner import MirrordRunner
from .models import (
    Alert,
    Patch,
    ProofBundle,
    RunMetrics,
    Slo,
    SloOperator,
    SloSignal,
    VerificationResult,
)

log = logging.getLogger("verifier.engine")

# Regression watchlist used alongside the alert's own SLO check.
# Tuple is (kind, tolerance). 'rel' = relative growth, 'abs' = absolute growth.
# The signal the alert itself fires on is excluded — improving that one is the
# point; regressing it would mean the alert is still firing, which the SLO
# check catches.
DEFAULT_WATCHLIST: dict[SloSignal, tuple[str, float]] = {
    SloSignal.P50_MS: ("rel", 0.05),       # p50 may grow up to 5% relative
    SloSignal.P99_MS: ("rel", 0.10),       # p99 may grow up to 10% relative
    SloSignal.ERROR_RATE: ("abs", 0.01),   # error rate may rise up to 1 pp
}

# Legacy classifier thresholds, used only when the alert has no Slo attached.
_LEGACY_P99_IMPROVEMENT_THRESHOLD = 0.30
_LEGACY_ERROR_IMPROVEMENT_THRESHOLD = 0.50
_LEGACY_REGRESSION_TOLERANCE = 0.05

# Load shape — concurrency keeps the run bounded even when the candidate
# hangs on some fraction of requests. 100 requests / 10 concurrent = ~10 batches,
# worst-case 10 * _REQUEST_TIMEOUT_S = 50s per run.
_LOAD_REQUESTS = 100
_LOAD_CONCURRENCY = 10
_REQUEST_TIMEOUT_S = 5.0
_READY_TIMEOUT_S = 30
_SHUTDOWN_TIMEOUT_S = 15


class VerificationEngine:
    def __init__(self, runner: MirrordRunner | None = None) -> None:
        self._injected_runner = runner

    def verify(self, alert: Alert, patch: Patch) -> ProofBundle:
        if alert.repo_path is None:
            raise RuntimeError("alert.repo_path required — verifier can't run without code")
        runner = self._injected_runner or MirrordRunner(
            target=alert.target, namespace=alert.namespace
        )
        self.runner = runner

        with tempfile.TemporaryDirectory(prefix="verifier-") as tmp:
            tmp_root = Path(tmp)
            baseline_dir = tmp_root / "baseline"
            patched_dir = tmp_root / "patched"
            shutil.copytree(alert.repo_path, baseline_dir)
            shutil.copytree(alert.repo_path, patched_dir)
            self._apply_patch(patched_dir, patch)

            baseline, _ = self._run_candidate(baseline_dir, label="baseline")
            patched, patched_stderr = self._run_candidate(patched_dir, label="patched")

        result, rationale = self._classify(baseline, patched, alert, patch)

        # On REJECT, ask Claude to explain why its patch failed. Wrapped so a
        # failure here never blocks the verdict from being posted.
        explanation: str | None = None
        if result == VerificationResult.REJECT:
            try:
                from .orchestrator import AISREOrchestrator

                explanation = AISREOrchestrator().explain_rejection(
                    patch, baseline, patched, patched_stderr
                )
            except Exception:
                log.exception("explain_rejection failed; posting verdict without explanation")

        return ProofBundle(
            alert=alert,
            patch=patch,
            baseline=baseline,
            patched=patched,
            result=result,
            rationale=rationale,
            explanation=explanation,
            generated_at=datetime.utcnow(),
        )

    # ---- per-run lifecycle ----------------------------------------------------

    def _run_candidate(self, candidate_dir: Path, label: str) -> tuple[RunMetrics, str]:
        port = _pick_free_port()
        url_root = f"http://127.0.0.1:{port}"
        cmd = [sys.executable, "-m", "app"]
        env = {
            "PYTHONPATH": str(candidate_dir),
            "PORT": str(port),
            "PRICING_URL": os.environ.get("PRICING_URL", "http://pricing"),
        }

        log.info("[%s] starting candidate server on %s", label, url_root)
        proc = self.runner.start_server(cmd, cwd=candidate_dir, label=label, extra_env=env)
        stderr_tail = ""
        try:
            self._wait_ready(f"{url_root}/healthz", proc, label)
            metrics = self._drive_load(f"{url_root}/checkout", label)
        finally:
            log.info("[%s] terminating candidate", label)
            proc.terminate()
            try:
                proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            # Drain pipes so the kernel doesn't keep buffers around.
            try:
                _ = proc.stdout.read() if proc.stdout else None
                stderr_tail = proc.stderr.read()[-3000:] if proc.stderr else ""
                if stderr_tail:
                    log.info("[%s] candidate stderr tail:\n%s", label, stderr_tail)
            except Exception:
                pass
        return metrics, stderr_tail

    def _wait_ready(
        self, healthz_url: str, proc: subprocess.Popen[str], label: str
    ) -> None:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr_tail = ""
                stdout_tail = ""
                try:
                    if proc.stderr:
                        stderr_tail = proc.stderr.read()[-2000:]
                    if proc.stdout:
                        stdout_tail = proc.stdout.read()[-2000:]
                except Exception:
                    pass
                raise RuntimeError(
                    f"[{label}] candidate exited before becoming ready "
                    f"(exit={proc.returncode}).\n"
                    f"stderr tail:\n{stderr_tail}\n---\nstdout tail:\n{stdout_tail}"
                )
            try:
                r = httpx.get(healthz_url, timeout=1.0)
                if r.status_code == 200:
                    log.info("[%s] candidate ready", label)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"[{label}] candidate did not become ready in {_READY_TIMEOUT_S}s")

    def _drive_load(self, url: str, label: str) -> RunMetrics:
        latencies_ms: list[float] = [0.0] * _LOAD_REQUESTS
        error_flags: list[int] = [0] * _LOAD_REQUESTS
        completed = [0]  # mutable counter shared across threads
        completed_lock = threading.Lock()

        client = httpx.Client(timeout=_REQUEST_TIMEOUT_S)

        def _one(i: int) -> None:
            t0 = time.perf_counter()
            try:
                r = client.post(url, json={"item_id": f"item-{i % 10}", "qty": 1})
                if r.status_code >= 500:
                    error_flags[i] = 1
            except httpx.HTTPError:
                error_flags[i] = 1
            latencies_ms[i] = (time.perf_counter() - t0) * 1000.0
            with completed_lock:
                completed[0] += 1
                # Emit progress every 10 completions so the run's visible in logs.
                if completed[0] % 10 == 0 or completed[0] == _LOAD_REQUESTS:
                    err_so_far = sum(error_flags[:i+1])
                    log.info("[%s] progress: %d/%d completed, %d errors so far",
                             label, completed[0], _LOAD_REQUESTS, err_so_far)

        try:
            with ThreadPoolExecutor(max_workers=_LOAD_CONCURRENCY) as pool:
                list(pool.map(_one, range(_LOAD_REQUESTS)))
        finally:
            client.close()

        errors = sum(error_flags)

        latencies_ms.sort()
        metrics = RunMetrics(
            duration_ms_p50=statistics.median(latencies_ms),
            duration_ms_p99=latencies_ms[int(0.99 * len(latencies_ms)) - 1],
            error_rate=errors / _LOAD_REQUESTS,
            request_count=_LOAD_REQUESTS,
        )
        log.info(
            "[%s] metrics: p50=%.0fms p99=%.0fms err=%.0f%%",
            label, metrics.duration_ms_p50, metrics.duration_ms_p99,
            metrics.error_rate * 100,
        )
        return metrics

    # ---- patch + classification (unchanged) -----------------------------------

    def _apply_patch(self, root: Path, patch: Patch) -> None:
        for pf in patch.files:
            target = root / pf.path
            if not target.exists():
                raise RuntimeError(f"patch targets missing file: {pf.path}")
            actual = target.read_text()
            if actual.strip() != pf.before.strip():
                raise RuntimeError(
                    f"patch.before does not match {pf.path}; AI-SRE drifted from source"
                )
            target.write_text(pf.after)
            log.info("patched %s", pf.path)

    def _classify(
        self,
        baseline: RunMetrics,
        patched: RunMetrics,
        alert: Alert,
        patch: Patch,
    ) -> tuple[VerificationResult, str]:
        """Two-part check: alert's SLO condition + regression watchlist.

        When alert.slo is present the classifier asks the only question that
        actually matters operationally: would the alert that fired still be
        firing on the patched code? Plus a regression watchlist on the other
        signals so the fix doesn't quietly break something adjacent. When
        alert.slo is absent the classifier falls back to legacy hardcoded
        rules (relative improvement thresholds).
        """
        if alert.slo is None:
            return self._classify_legacy(baseline, patched, patch)

        # 1. SLO check: does the alert's condition still hold on the patched run?
        patched_signal = _signal_value(patched, alert.slo.signal)
        baseline_signal = _signal_value(baseline, alert.slo.signal)
        still_firing = _evaluate(patched_signal, alert.slo.operator, alert.slo.threshold)
        slo_desc = (
            f"{alert.slo.signal.value} {alert.slo.operator.value} {alert.slo.threshold}"
        )

        # 2. Regression watchlist: did any other signal degrade past tolerance?
        regressions: list[str] = []
        for sig, (kind, tol) in DEFAULT_WATCHLIST.items():
            if sig == alert.slo.signal:
                continue
            b = _signal_value(baseline, sig)
            p = _signal_value(patched, sig)
            if kind == "rel":
                if b > 0 and (p - b) / b > tol:
                    regressions.append(
                        f"{sig.value} regressed {(p - b) / b:+.0%} (>{tol:.0%} tolerance)"
                    )
            else:  # abs
                if p - b > tol:
                    regressions.append(
                        f"{sig.value} grew {p - b:+.2f} (>{tol:.2f} tolerance)"
                    )

        # Verdict.
        if still_firing and regressions:
            return (
                VerificationResult.REJECT,
                f"Alert condition still satisfied: {slo_desc} (patched: {patched_signal:.3f}); "
                f"plus {len(regressions)} regression(s): {'; '.join(regressions)}.",
            )
        if still_firing:
            return (
                VerificationResult.REJECT,
                f"Alert would still be firing post-merge. "
                f"SLO `{slo_desc}` (baseline {baseline_signal:.3f}, patched {patched_signal:.3f}).",
            )
        if regressions:
            return (
                VerificationResult.REJECT,
                f"Alert condition resolved (`{slo_desc}` no longer satisfied at patched {patched_signal:.3f}), "
                f"but the patch introduced {len(regressions)} regression(s): {'; '.join(regressions)}.",
            )
        return (
            VerificationResult.PASS,
            f"Alert condition no longer satisfied: `{slo_desc}` "
            f"(baseline {baseline_signal:.3f}, patched {patched_signal:.3f}). "
            f"Regression watchlist clean.",
        )

    # ---- legacy classifier (used when alert.slo is missing) -----------------

    def _classify_legacy(
        self, baseline: RunMetrics, patched: RunMetrics, patch: Patch
    ) -> tuple[VerificationResult, str]:
        p99_delta = _delta(baseline.duration_ms_p99, patched.duration_ms_p99)
        err_delta = _delta(baseline.error_rate, patched.error_rate)
        p50_delta = _delta(baseline.duration_ms_p50, patched.duration_ms_p50)

        if p50_delta > _LEGACY_REGRESSION_TOLERANCE:
            return (
                VerificationResult.REJECT,
                f"Patch regressed p50 latency by {p50_delta:.0%}. "
                f"Rejecting even if p99 improved.",
            )

        improved_p99 = p99_delta < -_LEGACY_P99_IMPROVEMENT_THRESHOLD
        improved_err = err_delta < -_LEGACY_ERROR_IMPROVEMENT_THRESHOLD

        if improved_p99 or improved_err:
            wins = []
            if improved_p99:
                wins.append(f"p99 dropped {abs(p99_delta):.0%}")
            if improved_err:
                wins.append(f"error rate dropped {abs(err_delta):.0%}")
            return (
                VerificationResult.PASS,
                f"Patch moved the failing signal: {', '.join(wins)}. "
                f"Matches AI-SRE expected change: '{patch.expected_signal_change}'.",
            )

        return (
            VerificationResult.REJECT,
            f"Patch did not move the failing signal materially "
            f"(p99 {p99_delta:+.0%}, error rate {err_delta:+.0%}). "
            f"AI-SRE expected '{patch.expected_signal_change}' — observation disagrees.",
        )


def _delta(before: float, after: float) -> float:
    if before == 0:
        return 0.0 if after == 0 else 1.0
    return (after - before) / before


def _signal_value(m: RunMetrics, signal: SloSignal) -> float:
    return {
        SloSignal.P50_MS: m.duration_ms_p50,
        SloSignal.P99_MS: m.duration_ms_p99,
        SloSignal.ERROR_RATE: m.error_rate,
    }[signal]


def _evaluate(value: float, op: SloOperator, threshold: float) -> bool:
    return {
        SloOperator.GT: value > threshold,
        SloOperator.GTE: value >= threshold,
        SloOperator.LT: value < threshold,
        SloOperator.LTE: value <= threshold,
    }[op]


def _pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
