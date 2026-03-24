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
COPY packages/mcp/pyproject.toml    packages/mcp/pyproject.toml
COPY packages/cli/pyproject.toml    packages/cli/pyproject.toml
COPY packages/eval/pyproject.toml   packages/eval/pyproject.toml
# dummy READMEs so hatchling doesn't fail
RUN mkdir -p packages/common packages/core packages/mcp packages/cli packages/eval \
    && touch packages/common/README.md packages/core/README.md \
             packages/mcp/README.md packages/cli/README.md packages/eval/README.md

# 1b — install third-party deps only (cached until lock changes)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --no-dev \
    --package memex-common --package memex-mcp --package memex-cli \
    --extra mcp

# 1c — copy source + install workspace packages (MCP only)
COPY packages/common/src packages/common/src
COPY packages/mcp/src    packages/mcp/src
COPY packages/cli/src    packages/cli/src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
    --package memex-common --package memex-mcp --package memex-cli \
    --extra mcp

# Stage 2 — slim runtime
FROM python:3.12-slim
WORKDIR /app
COPY --from=uv /app/.venv /app/.venv
COPY --from=uv /app/packages /app/packages
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
ENTRYPOINT ["memex", "mcp", "run"]
CMD ["--transport", "sse", "--host", "0.0.0.0", "--port", "8000"]
