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
    """Prints the bundle to stdout. Default fallback when no PAGERDUTY_REST_API_KEY is set."""

    def post(self, bundle: ProofBundle) -> None:
        print(bundle.as_markdown())


class PagerDutyIncidentNotePoster:
    """Posts the proof bundle as a note on the originating PagerDuty incident.

    PD REST API: POST /incidents/{id}/notes
      Auth:     Authorization: Token token=<REST API key>
      From:     required email header (any user in the account)
      Accept:   application/vnd.pagerduty+json;version=2
      Body:     {"note": {"content": "..."}}

    The incident id is expected on bundle.alert.raw["event"]["data"]["id"],
    which is where the PagerDuty V3 webhook parser writes it.
    """

    BASE = "https://api.pagerduty.com/incidents"

    def __init__(self, api_key: str | None = None, from_email: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("PAGERDUTY_REST_API_KEY")
        if not self.api_key:
            raise RuntimeError("PAGERDUTY_REST_API_KEY required for PagerDutyIncidentNotePoster")
        self.from_email = from_email or os.environ.get("PAGERDUTY_FROM_EMAIL", "verifier@metalbear.com")

    def post(self, bundle: ProofBundle) -> None:
        incident_id = _extract_incident_id(bundle)
        if not incident_id:
            raise RuntimeError("no PagerDuty incident id on the alert; cannot post note")
        url = f"{self.BASE}/{incident_id}/notes"
        r = httpx.post(
            url,
            headers={
                "Authorization": f"Token token={self.api_key}",
                "From": self.from_email,
                "Accept": "application/vnd.pagerduty+json;version=2",
                "Content-Type": "application/json",
            },
            json={"note": {"content": bundle.as_markdown()}},
            timeout=15,
        )
        if r.status_code >= 400:
            log.error("PagerDuty note POST failed (%d) for incident %s: %s",
                      r.status_code, incident_id, r.text[:1000])
        r.raise_for_status()
        log.info("posted verification to PagerDuty incident %s", incident_id)


def _extract_incident_id(bundle: ProofBundle) -> str | None:
    raw = bundle.alert.raw or {}
    # PagerDuty V3 webhook: raw["event"]["data"]["id"]
    return (raw.get("event") or {}).get("data", {}).get("id") or raw.get("incident_id")
