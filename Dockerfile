# Verifier service image. Includes the sample-app source at /scaffold/sample-app
# so the in-cluster verifier can patch & replay it. In production this is
# replaced by a fetch from the service catalog / git.
FROM python:3.12-slim AS base
WORKDIR /srv

# mirrord CLI — verifier uses it to run baseline/patched under the operator.
# Pin a known version and install straight into /usr/local/bin so it's on PATH
# for the non-interactive subprocess the runner spawns.
ARG MIRRORD_VERSION=3.198.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/metalbear-co/mirrord/releases/download/${MIRRORD_VERSION}/mirrord_linux_x86_64" -o /usr/local/bin/mirrord \
    && chmod +x /usr/local/bin/mirrord \
    && mirrord --version

COPY pyproject.toml /srv/pyproject.toml
COPY src/ /srv/src/
RUN pip install --no-cache-dir -e .

COPY sample-app/ /scaffold/sample-app/
COPY scenarios/ /scaffold/scenarios/

ENV VERIFIER_SAMPLE_REPO=/scaffold/sample-app
EXPOSE 8000

CMD ["uvicorn", "verifier.webhook:app", "--host", "0.0.0.0", "--port", "8000"]
