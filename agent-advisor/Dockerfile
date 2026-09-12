# The AI Advisor Agent image, for Amazon Bedrock AgentCore Runtime.
#
# Requirements 26.2, 26.7, 32.1.
#
# ARM64 IS THE RUNTIME'S REQUIREMENT, NOT A PREFERENCE (Requirement 32.1). AgentCore Runtime takes a
# `linux/arm64` image, so the platform is pinned on the base rather than left to the builder's host.
# A build on an x86 laptop that silently produced an amd64 image would pass every local check and
# fail at deploy, which is the failure this pin exists to prevent.
#
# THE PORT AND HOST ARE THE RUNTIME'S CONTRACT TOO. Requirement 32.1 fixes `0.0.0.0:8080`, and the
# SDK's own `app.run()` binds exactly that. Neither is configurable here: a container that listened
# somewhere else would be unreachable by the platform rather than merely misconfigured.
#
# DEPENDENCIES INSTALL FROM THE COMMITTED LOCKFILE WITH A FROZEN RESOLVE. `uv sync --frozen` fails
# rather than re-resolving, which is what makes uv.lock authoritative: an image that resolved at
# build time could ship a dependency set the test suite never gated, and nothing would report the
# divergence.
#
# NO SECRET IS BAKED IN (Requirement 26.7). Every credential arrives at runtime through the
# environment or a runtime-supplied path, and a packaging test asserts nothing key-shaped is here.

FROM --platform=linux/arm64 python:3.12.14-slim AS base

# uv comes from its own published image rather than a curled install script, so the version is
# pinned by tag instead of by whatever that script serves today. Same version as Service 2's image.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The manifest and lock are copied ALONE first so the dependency layer caches independently of the
# source: editing a module then re-resolves and re-downloads nothing.
COPY pyproject.toml uv.lock ./

# --no-install-project because the project's own source is not in this layer yet.
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src

RUN uv sync --frozen --no-dev

# Engineering practices §7, least privilege: with no USER directive the container runs as root, so
# an escape from the process would land as root inside the container.
RUN useradd --create-home --uid 10001 aqm && chown -R aqm:aqm /app
USER aqm

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

# THE ENTRY MODULE IS TASK 21'S, AND NAMING IT HERE IS DELIBERATE. Task 21.1 wires the composition
# root — the pipeline, the tools, the hooks and the entrypoint built from configuration — and this
# image cannot start until it exists. Task 20 does not invent a stand-in: a module that loaded
# config and then failed to build a turn runner would be a broken image that looked packaged, and
# the packaging tests would then be asserting against a placeholder rather than the real entry.
#
# A test records the absence and fails once the module appears, which forces this comment to be
# deleted at the same commit that makes the image runnable.
CMD ["python", "-m", "aqm_advisor.main"]
