alias s := setup
alias t := test
alias p := prek
alias uc := update_collections

# Release: bump versions, commit, and tag (e.g., just release 0.1.0)
release version:
  #!/usr/bin/env bash
  set -euo pipefail
  # Validate semver format (allows pre-release suffixes like 0.1.0a, 0.1.0rc1)
  if ! echo "{{version}}" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9]*$'; then
    echo "Error: version must be semver (e.g., 0.1.0 or 0.1.0a)" >&2
    exit 1
  fi
  # Update TypeScript package versions (Python versions are automatic via hatch-vcs)
  for f in packages/dashboard/package.json packages/openclaw/package.json; do
    sed -i 's/"version": ".*"/"version": "{{version}}"/' "$f"
  done
  # Sync lock file
  uv lock
  # Stage, commit, tag
  git add uv.lock packages/dashboard/package.json packages/openclaw/package.json
  git commit -m "chore(release): v{{version}}"
  git tag "v{{version}}"
  echo "Tagged v{{version}}. Push with: git push && git push --tags"

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

# Run performance benchmarks
benchmark:
  uv run pytest packages/core/tests/benchmarks --benchmark-only -v

# Run database migrations to latest
db-upgrade:
  uv run memex database upgrade

# Show current migration revision
db-current:
  uv run memex database current

# Show migration history
db-history:
  uv run memex database history

# Generate a new migration from model changes
db-revision message:
  uv run memex database revision -m "{{message}}"

# Stamp database at head (for existing DBs)
db-stamp:
  uv run memex database stamp

# Install recording dependencies (asciinema, agg, ffmpeg, Playwright)
recording-setup:
    @echo "Checking asciinema..."
    which asciinema || uv tool install asciinema
    @echo "Checking agg..."
    which agg || echo "Install agg: https://github.com/asciinema/agg/releases"
    @echo "Checking ffmpeg..."
    which ffmpeg || echo "Install ffmpeg: apt install ffmpeg / brew install ffmpeg"
    cd recordings/dashboard && npm install && npx playwright install chromium

# Seed demo database for recordings (requires running server)
recording-seed:
    uv run python recordings/seed-data/seed_demo_db.py

# Record CLI GIFs via asciinema + agg
record-cli:
    bash recordings/cli/record-cli.sh

# Record Claude Code + Memex integration GIF (simulated session)
record-claude-code:
    bash recordings/cli/record-claude-code.sh

# Record dashboard GIFs via Playwright
record-dashboard:
    cd recordings/dashboard && npx tsx scripts/record-overview.ts
    cd recordings/dashboard && npx tsx scripts/record-entity-graph.ts
    cd recordings/dashboard && npx tsx scripts/record-memory-search.ts
    cd recordings/dashboard && npx tsx scripts/record-knowledge-flow.ts
    cd recordings/dashboard && npx tsx scripts/record-lineage.ts

# Record all GIFs (server + dashboard must be running)
record-all: recording-seed record-cli record-claude-code record-dashboard
