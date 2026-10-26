# syntax=docker/dockerfile:1

# Kowan — control-plane server image.
#
# Build from the repository root:
#   docker build -t ghcr.io/kowan-lab/kowan .
#   docker build --platform linux/amd64 -t ghcr.io/kowan-lab/kowan .    # cross-build
#
# Layout (smallest image that keeps every feature):
#   frontend  node, builds the React SPA into web/dist (runs on the BUILD
#             platform: its output is plain JS/CSS, identical for every arch)
#   tools     downloads + strips the static docker CLI and compose plugin
#   pydeps    installs requirements.txt into /opt/venv from prebuilt wheels
#   rootfs    python:alpine + git/bash, pruned of what a server never uses
#   final     FROM scratch: rootfs, then tools, deps, code, SPA as separate
#             layers ordered by how rarely they change, so an upgrade usually
#             re-pulls only the last few MB (code + SPA).
#
# Why alpine (musl) and not debian-slim: every runtime dependency (aiohttp,
# pydantic-core, cryptography, pyyaml, orjson, uvloop, ...) publishes musllinux
# wheels, and pip runs with --only-binary=:all: so a missing wheel fails the
# build loudly instead of silently needing a compiler. Alpine's git also does
# not drag in perl (~50 MB on Debian). Distroless was rejected: workflow script
# steps need bash, and git-backed deployments need git.
#
# The server reads web/dist from disk at runtime (kowan/web/app.py resolves it as
# parents[2]/web/dist, i.e. /app/web/dist here).

# Python 3.14: 3.15 is out, but pyyaml and frozenlist publish no musl wheels for
# it yet, and this build installs prebuilt wheels only. Pinned by digest.
ARG PYTHON_IMAGE=python:3.14.9-alpine3.24@sha256:dbeb11d16bb95c3a7e3c25c842cb9f20cbc8c18f8be0461a4a0dcc861ad386a0
# Node 26 (LTS from 2026-10-28; 24 is in maintenance). Pinned by digest.
ARG NODE_IMAGE=node:26.12.1-alpine3.24@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797

# --- frontend: build the SPA -----------------------------------------------------
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS frontend
WORKDIR /app/web/frontend
# Dependencies first so this layer is cached unless the lockfile changes. Every
# frontend dependency is public on npm: no private registry or token.
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
# vite (web/frontend/vite.config.ts) emits to ../dist (= /app/web/dist), with
# precompressed .gz/.br siblings that the server serves directly.
COPY web/frontend/ ./
RUN npm run build

# --- tools: docker CLI + compose plugin ------------------------------------------
# Container-level operations go over the mounted socket via the Engine API, but
# compose / swarm / build deployments shell out to `docker compose`, `docker
# stack`, `docker build`/`run`/`push` (client-side orchestration, not Engine API
# endpoints). Only the static CLIENT binary + compose plugin are installed (no
# daemon), with debug info stripped (~20 MB less, behaviour unchanged).
FROM ${PYTHON_IMAGE} AS tools
ARG DOCKER_CLI_VERSION=29.9.2
ARG DOCKER_COMPOSE_VERSION=5.6.1
ARG DOCKER_CLI_SHA256_X86_64=9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb
ARG DOCKER_CLI_SHA256_AARCH64=873b3e0207857c3b2f28392fb922f3ea6efe37620c16d4d4aaf2fef409c90b73
RUN set -eux; \
    apk add --no-cache curl binutils; \
    arch="$(uname -m)"; \
    mkdir -p /out/usr/local/bin /out/usr/local/lib/docker/cli-plugins; \
    # Docker publishes no checksums for the static CLI: the expected hash per
    # arch is pinned below (bump them together with DOCKER_CLI_VERSION).
    case "$arch" in \
      x86_64) cli_sha="$DOCKER_CLI_SHA256_X86_64" ;; \
      aarch64) cli_sha="$DOCKER_CLI_SHA256_AARCH64" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz; \
    echo "${cli_sha}  /tmp/docker.tgz" | sha256sum -c -; \
    tar -xzO -f /tmp/docker.tgz docker/docker > /out/usr/local/bin/docker; \
    # Compose publishes an official .sha256 next to every release asset.
    compose_url="https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-${arch}"; \
    curl -fsSL "$compose_url" -o /out/usr/local/lib/docker/cli-plugins/docker-compose; \
    want="$(curl -fsSL "${compose_url}.sha256" | cut -d' ' -f1)"; \
    got="$(sha256sum /out/usr/local/lib/docker/cli-plugins/docker-compose | cut -d' ' -f1)"; \
    [ -n "$want" ] && [ "$want" = "$got" ] || { echo "compose checksum mismatch: $got != $want" >&2; exit 1; }; \
    rm -f /tmp/docker.tgz; \
    strip /out/usr/local/bin/docker /out/usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod 0755 /out/usr/local/bin/docker /out/usr/local/lib/docker/cli-plugins/docker-compose; \
    /out/usr/local/bin/docker --version

# --- pydeps: Python dependencies into a self-contained venv ----------------------
FROM ${PYTHON_IMAGE} AS pydeps
COPY requirements.txt /tmp/requirements.txt
# --only-binary: wheels only, never compile (no toolchain in any stage).
# --no-compile + __pycache__ removal: no .pyc in the image (Python writes them on
# first import inside the container). pip itself is not needed at runtime.
# Extension modules are stripped of debug symbols (~10 MB); auditwheel-vendored
# libs (*.libs/) are left untouched.
RUN set -eux; \
    python -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --no-compile --only-binary=:all: \
        --disable-pip-version-check -r /tmp/requirements.txt; \
    /opt/venv/bin/pip uninstall -y --no-cache-dir pip; \
    find /opt/venv \( -name __pycache__ -o -name tests -o -name '*.pyi' \) -prune -exec rm -rf {} +; \
    apk add --no-cache binutils; \
    find /opt/venv -name '*.so' ! -path '*.libs/*' -exec strip --strip-unneeded {} +; \
    rm -f /opt/venv/bin/activate* /opt/venv/bin/Activate.ps1

# --- rootfs: runtime OS, pruned ---------------------------------------------------
# git: git-backed stacks and `docker build` from a Git URL. bash: workflow
# "script" steps. Everything removed below is dev/REPL tooling the server never
# imports (pip, ensurepip, idle, tkinter, lib2to3, pydoc data, C headers).
FROM ${PYTHON_IMAGE} AS rootfs
RUN set -eux; \
    apk add --no-cache git bash; \
    rm -rf /var/cache/apk/* /usr/local/include \
        /usr/local/bin/pip* /usr/local/bin/idle* /usr/local/bin/pydoc* /usr/local/bin/2to3* \
        /usr/local/lib/python3.14/site-packages/pip* \
        /usr/local/lib/python3.14/ensurepip /usr/local/lib/python3.14/idlelib \
        /usr/local/lib/python3.14/tkinter /usr/local/lib/python3.14/lib2to3 \
        /usr/local/lib/python3.14/pydoc_data /usr/local/lib/python3.14/turtledemo \
        /usr/local/lib/python3.14/config-3.14-*; \
    find / -xdev -name __pycache__ -prune -exec rm -rf {} +; \
    mkdir -p /data

# --- final -------------------------------------------------------------------------
FROM scratch AS final
COPY --from=rootfs / /
COPY --from=tools /out/ /
COPY --from=pydeps /opt/venv /opt/venv
WORKDIR /app
COPY kowan/ ./kowan/
COPY --from=frontend /app/web/dist ./web/dist

# Version metadata, read by the self-update flow (docs/updates-design.md).
ARG VERSION=dev
ARG SOURCE_URL=https://github.com/kowan-lab/kowan
LABEL org.opencontainers.image.title="kowan" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.url="https://kowan-lab.github.io/kowan"

# FROM scratch does not inherit the base image's ENV, so restate what matters.
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    KOWAN_DATA_DIR=/data

# Persist the SQLite database and compose stacks under KOWAN_DATA_DIR.
VOLUME /data
EXPOSE 5081

# Liveness probe used by the self-update verify step (State.Health). The
# `healthcheck` subcommand GETs /health on the local listener.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-m", "kowan.server", "healthcheck"]

# Runs as root (Docker's default — no USER directive), matching the agent image.
# The server can manage its own host's Docker daemon when you mount the socket
# (-v /var/run/docker.sock:/var/run/docker.sock); that socket is root:docker
# (0660), so running as root lets it drive it out of the box with no --group-add.
# Mounting docker.sock is already root-equivalent control of that host regardless
# of the in-container uid, so this is a convenience, not a real escalation.
ENTRYPOINT ["python", "-m", "kowan.server"]
