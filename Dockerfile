# syntax=docker/dockerfile:1

# Chainguard images are used for their near-zero CVE count. The free public tier
# publishes only `latest` and `latest-dev` — there are no version tags — so both
# stages are pinned by digest instead. Dependabot updates these.
#
# Re-resolve a digest by hand with:
#   crane digest cgr.dev/chainguard/python:latest

# --- build ------------------------------------------------------------------
# The -dev variant carries pip and a shell; the runtime variant carries neither.
FROM cgr.dev/chainguard/python:latest-dev@sha256:df9869a05f74ef57bd9fb8d9185f91cd3d93e70d7a89860795525d8c2ddebc50 AS build

# Chainguard's -dev variants still default to the nonroot user, so writing to /
# is denied. Switch to root for the build only — this stage is discarded, and the
# runtime stage below runs as 65532 regardless.
USER root

WORKDIR /src

# Build into a venv so the runtime stage copies exactly one self-contained
# directory. The runtime image has no pip to install with.
#
# The venv path must be identical in both stages: a venv bakes its own absolute
# path into the console-script shebangs, so copying it elsewhere silently
# produces entrypoints that point at a python that isn't there.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# Drop back to nonroot so this stage does not end as root. The stage is discarded
# either way, but leaving it root trips hadolint's DL3002 — and suppressing that
# lint here would also suppress it for the runtime stage, where it matters.
USER nonroot

# --- runtime ----------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest@sha256:231d4a76e8521327dbb3c23094b2c41151501845d2656da3c1a0610981c496c5

LABEL org.opencontainers.image.title="backblaze-b2-exporter" \
      org.opencontainers.image.description="Prometheus exporter for Backblaze B2 bucket usage as billed, including non-current versions and hidden files" \
      org.opencontainers.image.source="https://github.com/scottrus/backblaze-b2-exporter" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="cgr.dev/chainguard/python:latest"

COPY --from=build /venv /venv

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Chainguard runtime images already default to the nonroot user (65532), which
# matches runAsUser in the Helm chart. Stated explicitly so it survives a base
# image change.
USER 65532:65532

EXPOSE 9944

# Exec form keeps the exporter as PID 1, so SIGTERM reaches it directly and both
# `docker stop` and Kubernetes termination are clean — which matters here because
# the refresh loop waits on an Event specifically so it can be interrupted.
ENTRYPOINT ["backblaze-b2-exporter"]

# No shell in this image, so the check is an exec-form python one-liner.
#
# /health is a REAL endpoint, not prometheus_client's catch-all. Its own WSGI app
# answers every path with 200 — including typos — so a check against it would pass
# while proving nothing beyond "a socket is open". This exporter dispatches
# explicitly and 404s unknown paths, which is what makes this check meaningful.
#
# It deliberately does NOT assert collection success: a bad B2 key or an outage
# should surface as b2_collection_success 0 on a scrape someone can alert on, not
# as a container restart loop that destroys the evidence.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9944/health', timeout=4).status==200 else 1)"]
