alias s := setup
alias t := test
alias p := prek
alias uc := update_collections

# Install python dependencies
install:
  uv sync --all-groups --all-extras

# Install pre-commit hooks
prek_setup:
  uv run prek install

# Set up rtk
rtk_setup:
  rtk init --global -u

# Install python dependencies and pre-commit hooks
setup: install prek_setup rtk_setup

# Run pre-commit
prek:
 uv run prek run -a

# Run pytest
test:
  uv run pytest tests

# Create qmd collections
collections:
  qmd collection add . --name memex_test --mask "**/*.{py,toml,yaml,tsx,ts,svg,json}"
  qmd collection add . --name memex_src --mask "**/test_*.py"
  qmd collection add . --name memex_md --mask "**/*.md"

# Embed code (initial/new docs)
embed_collections:
  qmd embed

# Embed code (update)
update_collections: embed_collections
  qmd update

# Build OpenClaw memory plugin
build-openclaw:
  cd packages/openclaw && npm install --no-bin-links && node node_modules/typescript/lib/tsc.js

# Test OpenClaw memory plugin
test-openclaw:
  cd packages/openclaw && npx vitest run

# Start new dashboard in dev mode
dashboard-dev:
  cd packages/dashboard && npm run dev

# Build new dashboard for production
dashboard-build:
  cd packages/dashboard && npm run build

# Generate API types from OpenAPI spec
dashboard-generate-api:
  cd packages/dashboard && npm run generate-api
