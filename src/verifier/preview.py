"""Stage 2: build the patched candidate as a container image and start a
mirrord preview environment so a human can click into the patched candidate
directly.

This module is only invoked by the pipeline when stage 1 (`mirrord exec`
verification) classified the patch as PASS — we don't burn an image build or
a preview pod on patches the verifier already rejected.

Build path:
  - Patched source files + Dockerfile go into a ConfigMap
  - A Kaniko Job mounts the ConfigMap as /workspace and builds + pushes to
    a registry the cluster can pull from (gcr.io/mirrord-test/... here)
  - Image tag is deterministic on a session id so re-runs don't collide

Preview path:
  - Shell out to `mirrord preview start -i <image> -t <target> -k <key> --ttl`
  - The CLI talks to the operator; the operator copies the target pod spec,
    swaps in our image, and brings up an isolated pod
  - Returns the env-key — clients reach the patched pod by sending requests
    to the same service URL with `baggage: mirrord-session=<key>`
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from kubernetes import client, config

log = logging.getLogger("verifier.preview")

KANIKO_IMAGE = "gcr.io/kaniko-project/executor:v1.23.2"
DEFAULT_TTL_MINUTES = 30
BUILD_TIMEOUT_S = 300
PREVIEW_TIMEOUT_S = 120


@dataclass
class PreviewInfo:
    """What stage 2 hands back to the poster."""

    env_key: str
    image: str
    target: str
    namespace: str
    ttl_minutes: int
    service_hostname: str  # what the client points at; the header decides routing

    def curl_recipe(self) -> str:
        return (
            f"curl -H 'baggage: mirrord-session={self.env_key}' "
            f"http://{self.service_hostname}/checkout "
            f"-X POST -H 'content-type: application/json' "
            f"-d '{{\"item_id\":\"item-1\",\"qty\":1}}'"
        )


class PreviewBuilder:
    """Builds a patched image via Kaniko and starts a mirrord preview env.

    Pure orchestrator — no patch logic. Assumes engine.py already produced a
    directory containing the patched source + the Dockerfile.
    """

    def __init__(
        self,
        registry: str | None = None,
        builder_ns: str | None = None,
        mirrord_binary: str | None = None,
    ) -> None:
        self.registry = registry or os.environ.get(
            "VERIFIER_REGISTRY",
            "gcr.io/mirrord-test/mirrord-sre-verifier-candidate",
        )
        self.builder_ns = builder_ns or os.environ.get(
            "VERIFIER_BUILDER_NAMESPACE", "verifier-poc"
        )
        self.mirrord = mirrord_binary or os.environ.get("MIRRORD_BIN", "mirrord")
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.k8s_core = client.CoreV1Api()
        self.k8s_batch = client.BatchV1Api()

    def build_and_start(
        self,
        patched_dir: Path,
        session_id: str,
        target: str,
        namespace: str,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> PreviewInfo:
        image = f"{self.registry}:{session_id}"
        self._build_image(patched_dir, session_id, image)
        env_key = self._start_preview(image, target, namespace, session_id, ttl_minutes)
        service_hostname = self._infer_service_hostname(target, namespace)
        return PreviewInfo(
            env_key=env_key,
            image=image,
            target=target,
            namespace=namespace,
            ttl_minutes=ttl_minutes,
            service_hostname=service_hostname,
        )

    # ---- image build (kaniko) -------------------------------------------------

    def _build_image(self, source_dir: Path, session_id: str, image: str) -> None:
        cm_name = f"verifier-build-ctx-{session_id}"
        job_name = f"verifier-build-{session_id}"

        self._cleanup_build_artifacts(cm_name, job_name)
        self._create_source_configmap(cm_name, source_dir)
        self._create_kaniko_job(job_name, cm_name, image)
        self._wait_for_job(job_name)
        # Leave the ConfigMap + Job around briefly for debugging — TTL-cleaned
        # by a separate sweep job in production. For the POC we delete eagerly.
        self._cleanup_build_artifacts(cm_name, job_name)

    def _create_source_configmap(self, cm_name: str, source_dir: Path) -> None:
        # Pack the source as a tar.gz. Avoids any per-file key encoding (the
        # earlier slash→`__` scheme collided with `__main__.py`). ConfigMap
        # limit is 1 MiB; sample app tars to ~6 KB.
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for f in sorted(source_dir.rglob("*")):
                if not f.is_file() or "__pycache__" in f.parts:
                    continue
                tar.add(f, arcname=str(f.relative_to(source_dir)))
        archive = buf.getvalue()
        log.info("creating build context ConfigMap %s (tar.gz, %d bytes)", cm_name, len(archive))
        self.k8s_core.create_namespaced_config_map(
            namespace=self.builder_ns,
            body=client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name=cm_name,
                    labels={"app.kubernetes.io/managed-by": "verifier"},
                ),
                binary_data={"source.tar.gz": base64.b64encode(archive).decode("ascii")},
            ),
        )

    def _create_kaniko_job(self, job_name: str, cm_name: str, image: str) -> None:
        # Init container untars the source archive into /workspace; kaniko
        # then builds from there.
        init_script = (
            "set -e\n"
            "mkdir -p /workspace\n"
            "tar -xzf /src/source.tar.gz -C /workspace\n"
            "ls -la /workspace\n"
        )
        log.info("creating kaniko Job %s → %s", job_name, image)
        self.k8s_batch.create_namespaced_job(
            namespace=self.builder_ns,
            body=client.V1Job(
                metadata=client.V1ObjectMeta(
                    name=job_name,
                    labels={"app.kubernetes.io/managed-by": "verifier"},
                ),
                spec=client.V1JobSpec(
                    backoff_limit=0,
                    ttl_seconds_after_finished=300,
                    template=client.V1PodTemplateSpec(
                        spec=client.V1PodSpec(
                            restart_policy="Never",
                            # Use the verifier SA so kaniko gets GCR push perms
                            # via Workload Identity (k8s SA → kaniko-verifier
                            # GCP SA → storage.admin on mirrord-test).
                            service_account_name="verifier",
                            init_containers=[
                                client.V1Container(
                                    name="unpack",
                                    image="busybox:1.36",
                                    command=["/bin/sh", "-c", init_script],
                                    volume_mounts=[
                                        client.V1VolumeMount(
                                            name="src", mount_path="/src", read_only=True
                                        ),
                                        client.V1VolumeMount(
                                            name="workspace", mount_path="/workspace"
                                        ),
                                    ],
                                )
                            ],
                            containers=[
                                client.V1Container(
                                    name="kaniko",
                                    image=KANIKO_IMAGE,
                                    args=[
                                        "--dockerfile=/workspace/Dockerfile",
                                        "--context=/workspace",
                                        f"--destination={image}",
                                        "--single-snapshot",
                                        "--use-new-run",
                                    ],
                                    volume_mounts=[
                                        client.V1VolumeMount(
                                            name="workspace", mount_path="/workspace"
                                        ),
                                    ],
                                )
                            ],
                            volumes=[
                                client.V1Volume(
                                    name="src",
                                    config_map=client.V1ConfigMapVolumeSource(name=cm_name),
                                ),
                                client.V1Volume(
                                    name="workspace",
                                    empty_dir=client.V1EmptyDirVolumeSource(),
                                ),
                            ],
                        )
                    ),
                ),
            ),
        )

    def _wait_for_job(self, job_name: str) -> None:
        deadline = time.monotonic() + BUILD_TIMEOUT_S
        while time.monotonic() < deadline:
            j = self.k8s_batch.read_namespaced_job(name=job_name, namespace=self.builder_ns)
            if j.status.succeeded:
                log.info("kaniko Job %s succeeded", job_name)
                return
            if j.status.failed:
                logs = self._tail_job_logs(job_name)
                raise RuntimeError(f"kaniko Job {job_name} failed. Last logs:\n{logs}")
            time.sleep(3)
        raise RuntimeError(f"kaniko Job {job_name} timed out after {BUILD_TIMEOUT_S}s")

    def _tail_job_logs(self, job_name: str) -> str:
        try:
            pods = self.k8s_core.list_namespaced_pod(
                namespace=self.builder_ns, label_selector=f"job-name={job_name}"
            )
            if not pods.items:
                return "(no pod found)"
            return self.k8s_core.read_namespaced_pod_log(
                name=pods.items[0].metadata.name,
                namespace=self.builder_ns,
                container="kaniko",
                tail_lines=40,
            )
        except Exception as e:
            return f"(failed to read logs: {e})"

    def _cleanup_build_artifacts(self, cm_name: str, job_name: str) -> None:
        for delete in (
            lambda: self.k8s_core.delete_namespaced_config_map(cm_name, self.builder_ns),
            lambda: self.k8s_batch.delete_namespaced_job(
                job_name,
                self.builder_ns,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            ),
        ):
            try:
                delete()
            except client.ApiException as e:
                if e.status != 404:
                    log.warning("cleanup error: %s", e)

    # ---- preview env ----------------------------------------------------------

    def _start_preview(
        self, image: str, target: str, namespace: str, key: str, ttl_minutes: int
    ) -> str:
        argv = [
            self.mirrord, "preview", "start",
            "-k", key,
            "-i", image,
            "-t", target,
            "-n", namespace,
            "--ttl", str(ttl_minutes),
            "--timeout", str(PREVIEW_TIMEOUT_S),
            "--force",
        ]
        log.info("mirrord preview start: %s", " ".join(argv))
        env = {**os.environ, "MIRRORD_TELEMETRY": "false"}
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=PREVIEW_TIMEOUT_S + 30
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"mirrord preview start failed (exit={proc.returncode})\n"
                f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}"
            )
        return key

    def stop_preview(self, key: str, target: str, namespace: str) -> None:
        argv = [
            self.mirrord, "preview", "stop",
            "--key", key, "-t", target, "-n", namespace,
        ]
        env = {**os.environ, "MIRRORD_TELEMETRY": "false"}
        try:
            subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        except subprocess.SubprocessError as e:
            log.warning("preview stop failed: %s", e)

    def _infer_service_hostname(self, target: str, namespace: str) -> str:
        # target is "deployment/checkout" or similar; pull the bare name and
        # assume there's a Service of the same name in the namespace. Correct
        # for the POC; production should resolve via service catalog.
        bare = target.split("/")[-1]
        return f"{bare}.{namespace}.svc.cluster.local"
