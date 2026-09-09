# Sensor Simulator — single image running the whole Swarm in ONE process
# (Requirement 16.5). With no configuration file supplied it starts REST-only
# and answers GET /health shortly after start.
#
# Versions match the pinned devbox toolchain so the image and a contributor's
# shell resolve the same interpreter and installer (dev-environment steering).
FROM python:3.12.14-slim AS build

# uv resolves and installs from the COMMITTED lockfile, so the image gets the
# same exact versions as every other environment.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Copy only the manifests first so a source-only change reuses the dependency
# layer. --frozen fails rather than silently re-resolving if the lock is stale.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev


FROM python:3.12.14-slim AS runtime

# Run as an unprivileged user; nothing in the image needs root at runtime.
RUN useradd --create-home --uid 10001 simulator
WORKDIR /app

COPY --from=build --chown=simulator:simulator /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # The listener must bind the container's external interface to be reachable
    # from the host or a sibling service. The routes it exposes are gated by
    # X-API-KEY (Requirement 14.2); /health is deliberately unauthenticated
    # (Requirement 14.8) so an orchestrator can probe it.
    AQM_LISTEN_HOST=0.0.0.0 \
    AQM_LISTEN_PORT=8000 \
    # Documented defaults; every one is overridable at run time (Req 15.1).
    AQM_INTERFACE=rest

# NO secret is baked into the image (Requirement 16.8): AQM_API_KEY, broker
# certificates, and private keys are all supplied at run time.

EXPOSE 8000
USER simulator

# The unauthenticated /health route is the readiness signal (Requirement 14.8).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

ENTRYPOINT ["python", "-m", "aqm_simulator.cli"]
