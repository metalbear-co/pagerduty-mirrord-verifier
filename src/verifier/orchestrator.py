"""AI-SRE orchestrator: Claude wrapper that proposes a patch from an alert."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from anthropic import Anthropic

from .models import Alert, Patch, PatchFile

log = logging.getLogger("verifier.orchestrator")

DEFAULT_MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """You are an AI Site Reliability Engineer. You receive a production
alert and a snapshot of the candidate service's source code. You must respond with
exactly one JSON object matching this schema:

{
  "summary": "<one-line description of the suggested fix>",
  "hypothesis": "<one paragraph: what you think is causing the alert>",
  "repro_steps": ["<step 1>", "<step 2>", ...],
  "files": [{"path": "<rel path>", "before": "<exact original content>",
             "after": "<exact patched content>"}],
  "confidence": 0.0-1.0,
  "expected_signal_change": "<what metric you expect to move and in which direction>"
}

Rules:
- Patches must be syntactically valid in the file's language.
- 'before' must match the file content exactly (including whitespace).
- Be precise. The next stage of the pipeline applies your patch and runs it.
- Do not add commentary outside the JSON.
"""


class AISREOrchestrator:
    """Wraps Claude. Real call, real cost. Override `model` via env for cheap runs."""

    def __init__(self, model: str | None = None) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY missing — orchestrator needs a real Claude key."
            )
        self.client = Anthropic(api_key=api_key)
        self.model = model or os.environ.get("VERIFIER_MODEL", DEFAULT_MODEL)

    def propose_patch(self, alert: Alert) -> Patch:
        repo_snapshot = _snapshot_repo(alert.repo_path) if alert.repo_path else ""
        user_msg = _format_user_prompt(alert, repo_snapshot)
        log.info("calling %s for alert=%s", self.model, alert.id)

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        payload = _extract_json(text)
        return Patch(
            summary=payload["summary"],
            hypothesis=payload["hypothesis"],
            repro_steps=payload["repro_steps"],
            files=[PatchFile(**f) for f in payload["files"]],
            confidence=float(payload["confidence"]),
            expected_signal_change=payload["expected_signal_change"],
            model=self.model,
        )


def _snapshot_repo(repo_path: Path, max_bytes: int = 30_000) -> str:
    """Concatenate the source files Claude will need to reason about.

    INTEGRATION: in production this is replaced by service-catalog lookup +
    targeted file fetch (or RAG over the repo). The naive concat works fine
    for the scaffold because the sample app is ~100 lines.
    """
    chunks: list[str] = []
    used = 0
    for f in sorted(repo_path.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix not in {".py", ".yaml", ".yml", ".toml", ".json"}:
            continue
        if any(part.startswith(".") for part in f.parts):
            continue
        rel = f.relative_to(repo_path)
        body = f.read_text(errors="replace")
        block = f"\n--- {rel} ---\n{body}\n"
        if used + len(block) > max_bytes:
            break
        chunks.append(block)
        used += len(block)
    return "".join(chunks)


def _format_user_prompt(alert: Alert, repo_snapshot: str) -> str:
    return (
        f"# Alert\n"
        f"- Title: {alert.title}\n"
        f"- Service: {alert.service}\n"
        f"- Severity: {alert.severity.value}\n"
        f"- Metric: {alert.metric} (observed={alert.observed}, threshold={alert.threshold})\n"
        f"- Tags: {json.dumps(alert.tags)}\n"
        f"- Body:\n{alert.body}\n\n"
        f"# Candidate code\n"
        f"{repo_snapshot}\n\n"
        f"Respond with the JSON object specified in the system prompt."
    )


def _extract_json(text: str) -> dict:
    """Pull the first {...} block out of the response. Tolerates code fences."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = fence.group(1) if fence else text[text.find("{") : text.rfind("}") + 1]
    return json.loads(blob)
