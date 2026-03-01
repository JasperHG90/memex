# Stage 1 — uv: install deps + build workspace
FROM ghcr.io/astral-sh/uv:0.10.7-python3.12-trixie-slim AS uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION}

# 1a — copy only lock + all pyproject.toml files (cache-friendly layer)
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/pyproject.toml
COPY packages/core/pyproject.toml   packages/core/pyproject.toml
COPY packages/cli/pyproject.toml    packages/cli/pyproject.toml
COPY packages/mcp/pyproject.toml    packages/mcp/pyproject.toml
# dummy READMEs so hatchling doesn't fail
RUN mkdir -p packages/common packages/core packages/cli packages/mcp \
    && touch packages/common/README.md packages/core/README.md \
             packages/cli/README.md packages/mcp/README.md

# 1b — install third-party deps only (cached until lock changes)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --no-dev

# 1c — copy source + install workspace packages
COPY packages/common/src packages/common/src
COPY packages/core/src   packages/core/src
COPY packages/cli/src    packages/cli/src
COPY packages/mcp/src    packages/mcp/src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Stage 2 — slim runtime
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=uv /app/.venv /app/.venv
COPY --from=uv /app/packages /app/packages
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
ENTRYPOINT ["memex"]
CMD ["server"]
