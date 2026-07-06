"""Wraps `mirrord exec`. Spawns the candidate as a long-running process whose
network identity matches the target pod; the caller (the engine) drives load
against it externally.

The engine calls `start_server` twice per alert — once for baseline, once for
patched. Both runs share the same downstream cluster state. Running both under
mirrord is what makes the comparison fair.

Earlier versions of this module captured stdout from a one-shot `replay`
subroutine bundled with the application. That conflated the application with
its own test harness. The current shape keeps the application a pure server
and puts measurement on the verifier's side of the wall.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("verifier.mirrord")


class MirrordRunner:
    """Spawns a process under `mirrord exec` and returns the Popen handle."""

    def __init__(
        self,
        mirrord_binary: str | None = None,
        target: str | None = None,
        namespace: str | None = None,
    ) -> None:
        self.binary = mirrord_binary or os.environ.get("MIRRORD_BIN") or "mirrord"
        self.target = target or os.environ.get("MIRRORD_TARGET")
        self.namespace = namespace or os.environ.get("MIRRORD_NAMESPACE", "default")

    def start_server(
        self,
        command: list[str],
        cwd: Path,
        label: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        """Spawn `command` under `mirrord exec`. Returns the Popen handle so the
        engine can wait for readiness, drive load, then SIGTERM.

        Falls back to running `command` directly (no mirrord wrap) when
        mirrord isn't on PATH or no target is set — the verdict is still
        meaningful, just no live-cluster steering.
        """
        env = {**os.environ, **(extra_env or {}), "VERIFIER_RUN_LABEL": label}
        resolved = shutil.which(self.binary)
        wrapped = bool(resolved) and bool(self.target)

        if wrapped:
            argv = [
                resolved, "exec",
                "--target", self.target,
                "--target-namespace", self.namespace,
                "--", *command,
            ]
            log.info("mirrord exec [%s]: cwd=%s argv=%s", label, cwd, argv)
        else:
            argv = command
            log.warning(
                "LOCAL-ONLY run [%s] — mirrord=%s target=%s. Cluster steering disabled.",
                label, resolved, self.target,
            )

        # Pipe stdout/stderr to PIPE so we can drain them after termination;
        # otherwise a chatty server can fill the pipe buffer and stall.
        return subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
