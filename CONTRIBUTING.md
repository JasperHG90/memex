# Contributing to Memex

Thanks for your interest in contributing to Memex! This guide will help you get started.

## Prerequisites

- Python >= 3.12
- Node.js >= 22
- Docker (for integration tests)
- [uv](https://docs.astral.sh/uv/) (Python package manager)

## Getting Started

1. Clone the repository:
   ```bash
   git clone https://github.com/JasperHG90/memex.git
   cd memex
   ```

2. Set up the Python environment:
   ```bash
   just setup
   ```

3. Set up TypeScript packages (if working on dashboard or openclaw):
   ```bash
   cd packages/dashboard && npm install
   cd packages/openclaw && npm install
   ```

## Project Structure

Memex is a Python monorepo managed by `uv` with TypeScript packages for the dashboard and OpenClaw plugin.

| Package | Description |
|---------|-------------|
| `packages/core` | Core library: storage, memory system, API, FastAPI server |
| `packages/cli` | Typer CLI (`memex` command) |
| `packages/mcp` | FastMCP server for LLM integration |
| `packages/common` | Shared Pydantic models, config, exceptions |
| `packages/dashboard` | React + Vite web UI |
| `packages/openclaw` | Memex memory plugin for OpenClaw (TypeScript) |

For detailed architecture information, see `CLAUDE.md`.

## Code Style

### Python
- Single quotes for strings
- Line length: 100 characters
- Formatter/linter: [ruff](https://docs.astral.sh/ruff/)
- Type hints: strict (enforced by mypy)
- All I/O must be async (asyncio)
- Python >= 3.12 features are encouraged

### TypeScript
- Follow existing ESLint configuration in each package
- Strict TypeScript (`strict: true`)

## Running Tests

### Python
```bash
just test                                       # all tests
uv run pytest tests/test_file.py::test_name -v  # single test
uv run pytest -m integration                    # integration tests (require Docker)
uv run pytest -m llm                            # LLM tests (require API key)
```

### Dashboard
```bash
cd packages/dashboard
npm test
```

### OpenClaw
```bash
cd packages/openclaw
npm test
```

## Pre-commit Hooks

Run linting and formatting checks before committing:

```bash
just prek
```

This runs ruff (formatting + linting) and mypy (type checking) across the Python codebase.

## Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/). Format:

```
<type>(<scope>): <description>
```

**Types**: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

**Scopes** (when relevant): `core`, `cli`, `mcp`, `common`, `dashboard`, `openclaw`

**Examples**:
```
feat(dashboard): add entity graph visualization
fix(core): handle null embeddings in retrieval
refactor(common): simplify config loading
test(core): add reflection worker unit tests
docs: update API reference
chore: bump ruff to 0.8.0
```

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with conventional commits
3. Ensure CI passes (`just prek` + `just test`)
4. Open a PR against `main`
5. Address review feedback
6. Squash and merge once approved

## Dependencies

- Use `uv` for Python dependencies (never `pip`)
- Add dev dependencies: `uv add --dev <package>`
- Add package-specific dependencies: `uv add <package> --package memex_core`
- TypeScript packages use `npm`
