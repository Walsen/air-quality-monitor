# The ingestion-and-serving service image.
#
# Requirements 28.2, 28.9.
#
# THE INTERPRETER IS PINNED AND DEPENDENCIES INSTALL FROM THE LOCKFILE. `uv sync --frozen` FAILS
# rather than re-resolving, which is what makes uv.lock authoritative: an image that resolved at
# build time could ship a different dependency set from the one the test suite gated, and nothing
# would report the divergence.
#
# NO SECRET IS BAKED IN (Requirement 28.9). Every credential arrives at runtime through the
# environment or a runtime-supplied path; a packaging test asserts nothing key-shaped is present.

FROM python:3.12-slim AS base

# uv is copied from its own published image rather than curled, so the version is pinned by
# digest-bearing tag instead of by whatever the install script serves today.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The manifest and lock are copied ALONE first so the dependency layer is cached independently
# of the source: editing a module then does not re-resolve or re-download anything.
COPY pyproject.toml uv.lock ./

# --no-install-project because the project's own source is not present yet in this layer.
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config

RUN uv sync --frozen --no-dev

# §7 least privilege: absent a USER directive the container runs as root, so an escape from the
# process would land as root inside the container.
RUN useradd --create-home --uid 10001 aqm && chown -R aqm:aqm /app
USER aqm

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# The serving API and the ingestion entry points are selected by configuration, so the image has
# ONE entry point and the deployment decides which interfaces it enables (Requirement 26).
CMD ["python", "-m", "aqm_ingestion.cli"]
