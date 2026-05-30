# =============================================================================
# bg-zammad-mcp — BAUER GROUP Zammad MCP Server
# =============================================================================
# Remote MCP bridge for self-hosted Zammad. OAuth 2.1 inbound (Zammad as IdP
# or external OIDC), Bearer/Token forwarding outbound, FastMCP + streamable-http
# transport. Compatible with Zammad v6.x and v7.x.
#
# Multi-stage build with integrated testing.
#
#   docker build --target production -t bg-zammad-mcp .   # production only
#   docker build --target test -t bg-zammad-mcp-test .    # run tests
#   docker build -t bg-zammad-mcp .                       # full pipeline (test gates production)
# =============================================================================

# ── Stage 1: Builder — install Python dependencies into a relocatable prefix ──
FROM python:3.14-alpine AS builder

RUN apk add --no-cache build-base libffi-dev

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install into /install so the production stage can copy a clean, isolated tree.
# `--no-deps` is intentionally NOT used - we want transitive deps included.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install .


# ── Stage 2: Test — run pytest; image build fails on any test failure ────────
FROM python:3.14-alpine AS test

RUN apk add --no-cache build-base libffi-dev

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY tests/ ./tests/

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir ".[test]"

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# `env -i` wipes CI build args that would otherwise leak into Pydantic Settings
# during test-time validation (e.g. a GitHub Actions runner exporting
# ZAMMAD_OAUTH_CLIENT_SECRET into the shell).
RUN env -i HOME="$HOME" PATH="$PATH" PYTHONPATH="$PYTHONPATH" \
        PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
        pytest tests/ -q --tb=short


# ── Stage 3: Production — minimal runtime image ──────────────────────────────
FROM python:3.14-alpine AS production

# ── OCI / BAUER GROUP labels ────────────────────────────────────────────────
LABEL vendor="BAUER GROUP"
LABEL maintainer="Karl Bauer <karl.bauer@bauer-group.com>"

LABEL org.opencontainers.image.title="bg-zammad-mcp — BAUER GROUP Zammad MCP Server"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.authors="Karl Bauer <karl.bauer@bauer-group.com>"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/ZAMMAD-MCPServer"
LABEL org.opencontainers.image.url="https://github.com/bauer-group/ZAMMAD-MCPServer"
LABEL org.opencontainers.image.documentation="https://github.com/bauer-group/ZAMMAD-MCPServer#readme"
LABEL org.opencontainers.image.description="bg-zammad-mcp — OAuth-gated remote MCP for self-hosted Zammad v6/v7 (BAUER GROUP)"
LABEL com.bauer-group.image.component="mcp-server"
LABEL com.bauer-group.image.stack="zammad"
LABEL org.opencontainers.image.version="0.1.0"

RUN apk add --no-cache \
        tini \
        tzdata \
        ca-certificates \
        curl \
    && rm -rf /var/cache/apk/* \
    && addgroup -g 1001 -S mcp \
    && adduser -S -D -u 1001 -G mcp -h /home/mcp mcp

# Python deps from the builder stage (relocatable prefix install).
COPY --from=builder /install /usr/local

# Hard test-stage gate — if tests fail upstream, this COPY cannot resolve and
# the production build fails. The file path is arbitrary; we use README.md
# because it's guaranteed to exist in the test stage.
COPY --from=test /app/README.md /tmp/_test_passed
RUN rm /tmp/_test_passed

WORKDIR /app
COPY --chown=mcp:mcp src/ ./src/

# OAuth state directory for the disk-storage fallback (when AUTH_REDIS_URL is
# unset). Created and chowned at build time so the mcp user can write to it
# even before a volume is mounted. Compose stacks mount this path as a named
# volume; bare `docker run` invocations get a working but ephemeral default.
RUN mkdir -p /app/data/oauth-storage \
    && chown -R mcp:mcp /app/data

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER mcp
EXPOSE 8000

# /healthz returns 200 OK as soon as the FastMCP lifespan reports ready.
# Distinct from Zammad's /api/v1/monitoring/health_check (which sits behind
# Zammad's own auth wall).
#
# Shell-form CMD so ${MCP_PORT:-8000} is expanded by /bin/sh at runtime.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${MCP_PORT:-8000}/healthz || exit 1

# tini as PID 1 forwards SIGTERM cleanly to the python process so `docker stop`
# triggers a graceful FastMCP lifespan shutdown instead of a 10s SIGKILL wait.
ENTRYPOINT ["/sbin/tini", "--", "python", "src/main.py"]
CMD ["serve"]


# -----------------------------------------------------------------------------
# Usage
# -----------------------------------------------------------------------------
# Default (serve):        docker run ... bg-zammad-mcp
# Health probe:           docker run ... bg-zammad-mcp health
# Zammad version probe:   docker run ... bg-zammad-mcp probe
# Print tool catalogue:   docker run ... bg-zammad-mcp tools
