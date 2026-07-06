"""Where the proof bundle goes. Pluggable so each vendor integration is one class."""

from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

from .models import ProofBundle

log = logging.getLogger("verifier.poster")


class Poster(Protocol):
    def post(self, bundle: ProofBundle) -> None: ...


class StdoutPoster:
    """Prints the bundle to stdout. Default for the demo."""

    def post(self, bundle: ProofBundle) -> None:
        print(bundle.as_markdown())


class DatadogEventPoster:
    """Posts the bundle as a Datadog event tagged to the original alert.

    Datadog API ref: https://docs.datadoghq.com/api/latest/events/#post-an-event
    """

    def __init__(self, api_key: str | None = None, site: str = "datadoghq.com") -> None:
        self.api_key = api_key or os.environ.get("DATADOG_API_KEY")
        if not self.api_key:
            raise RuntimeError("DATADOG_API_KEY required for DatadogEventPoster")
        self.base = f"https://api.{site}/api/v1/events"

    def post(self, bundle: ProofBundle) -> None:
        payload = {
            "title": f"mirrord verification: {bundle.result.value} — {bundle.alert.title}",
            "text": bundle.as_markdown(),
            "tags": [
                f"alert_id:{bundle.alert.id}",
                f"verifier:mirrord",
                f"result:{bundle.result.value}",
                f"service:{bundle.alert.service}",
            ],
            "alert_type": "info" if bundle.result.value == "pass" else "warning",
            "source_type_name": "mirrord-sre-verifier",
        }
        r = httpx.post(self.base, headers={"DD-API-KEY": self.api_key}, json=payload, timeout=10)
        r.raise_for_status()
        log.info("posted verification to Datadog: status=%s", r.status_code)


class GitHubPRCommentPoster:
    """Posts the bundle as a comment on a GitHub PR.

    INTEGRATION: in production the alert payload (or the AI-SRE's output)
    carries the PR number to comment on. The scaffold leaves PR routing as a
    TODO because it depends on the partner vendor's workflow.
    """

    def __init__(self, repo: str, pr_number: int, token: str | None = None) -> None:
        self.repo = repo
        self.pr_number = pr_number
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN required for GitHubPRCommentPoster")

    def post(self, bundle: ProofBundle) -> None:
        url = f"https://api.github.com/repos/{self.repo}/issues/{self.pr_number}/comments"
        r = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": bundle.as_markdown()},
            timeout=10,
        )
        r.raise_for_status()
        log.info("posted verification to %s#%s", self.repo, self.pr_number)
