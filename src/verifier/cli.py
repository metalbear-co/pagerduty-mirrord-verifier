"""CLI entrypoint. Run a scenario through the full verification pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .engine import VerificationEngine
from .models import Alert, Patch, PatchFile, Severity, Slo, SloOperator, SloSignal
from .poster import StdoutPoster
from .webhook import parse_datadog_payload


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_scenario(path: Path, repo_root: Path) -> tuple[Alert, Patch | None, bool]:
    blob = json.loads(path.read_text())
    alert = parse_datadog_payload(blob["alert"])
    alert.repo_path = (repo_root / blob["repo_path"]).resolve()
    slo_blob = blob.get("alert_slo")
    if slo_blob:
        alert.slo = Slo(
            signal=SloSignal(slo_blob["signal"]),
            operator=SloOperator(slo_blob["operator"]),
            threshold=float(slo_blob["threshold"]),
        )
    use_claude = bool(blob.get("use_claude", True))
    prepared = blob.get("prepared_patch")
    patch = (
        Patch(
            summary=prepared["summary"],
            hypothesis=prepared["hypothesis"],
            repro_steps=prepared["repro_steps"],
            files=[PatchFile(**f) for f in prepared["files"]],
            confidence=prepared["confidence"],
            expected_signal_change=prepared["expected_signal_change"],
            model=prepared["model"],
        )
        if prepared
        else None
    )
    return alert, patch, use_claude


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    alert, prepared_patch, use_claude = _load_scenario(Path(args.scenario), repo_root)

    if use_claude and prepared_patch is None:
        from .orchestrator import AISREOrchestrator

        patch = AISREOrchestrator().propose_patch(alert)
    elif prepared_patch is not None:
        patch = prepared_patch
    else:
        print("scenario has neither use_claude=true nor prepared_patch", file=sys.stderr)
        return 2

    bundle = VerificationEngine().verify(alert, patch)
    StdoutPoster().post(bundle)

    if args.bundle_out:
        Path(args.bundle_out).write_text(bundle.model_dump_json(indent=2))
    return 0 if bundle.result.value == "pass" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("verifier.webhook:app", host=args.host, port=args.port, log_level="info")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="verifier")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run one scenario end-to-end")
    run.add_argument("scenario", help="Path to scenario JSON")
    run.add_argument("--repo-root", default=".", help="Repo root (resolves scenario.repo_path)")
    run.add_argument("--bundle-out", help="Write the proof bundle JSON here")
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve", help="Run the webhook receiver")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    args = p.parse_args()
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
