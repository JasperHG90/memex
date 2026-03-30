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
  # Sync lock file
  uv lock
  # Stage, commit, tag
  git add uv.lock
  git commit -m "chore(release): v{{version}}"
  git tag "v{{version}}"
  echo "Tagged v{{version}}. Push with: git push && git push --tags"

# Serve documentation locally with live reload
docs-serve:
  uv run zensical serve --dev-addr localhost:8005 --open

# Build documentation site
docs-build:
  uv run zensical build

# Build docs with clean cache
docs-clean:
  uv run zensical build --clean

# Install python dependencies
install:
  uv sync --all-groups --all-extras
  uv tool install -e ./packages/cli

# Install pre-commit hooks
prek_setup:
  uv run prek install

# Set up rtk
rtk_setup:
  rtk init --global -u

# gh cli setup
gh_setup:
  gh auth login
  gh auth setup-git

# Install python dependencies and pre-commit hooks
setup: install prek_setup rtk_setup

# Audit dependencies for known vulnerabilities
audit:
  uv run pip-audit

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

# Run performance benchmarks
benchmark:
  uv run pytest packages/core/tests/benchmarks --benchmark-only -v

# Start postgres, run server in a temp dir, execute benchmark, then tear down
benchmark-internal server='http://localhost:8001/api/v1/' *args='':
  #!/usr/bin/env bash
  set -euo pipefail
  docker compose up -d db
  echo "Waiting for postgres..."
  until docker compose exec db pg_isready -U postgres -q 2>/dev/null; do sleep 1; done
  TMPDIR=$(mktemp -d)
  mkdir -p "$TMPDIR/filestore"
  trap 'kill $SERVER_PID 2>/dev/null || true; rm -rf "$TMPDIR"; docker compose stop db' EXIT
  MEMEX_PORT=8001 MEMEX_SERVER__FILE_STORE__TYPE=local MEMEX_SERVER__FILE_STORE__ROOT="$TMPDIR/filestore" uv run memex server start &
  SERVER_PID=$!
  echo "Waiting for server on :8001..."
  until curl -sf http://localhost:8001/api/v1/vaults >/dev/null 2>&1; do sleep 1; done
  uv run memex-eval run --server {{server}} {{args}}

# Start postgres, run server in a temp dir, execute benchmark (no LLM judge), then tear down
benchmark-internal-fast server='http://localhost:8001/api/v1/' *args='':
  #!/usr/bin/env bash
  set -euo pipefail
  docker compose up -d db
  echo "Waiting for postgres..."
  until docker compose exec db pg_isready -U postgres -q 2>/dev/null; do sleep 1; done
  TMPDIR=$(mktemp -d)
  mkdir -p "$TMPDIR/filestore"
  trap 'kill $SERVER_PID 2>/dev/null || true; rm -rf "$TMPDIR"; docker compose stop db' EXIT
  MEMEX_PORT=8001 MEMEX_SERVER__FILE_STORE__TYPE=local MEMEX_SERVER__FILE_STORE__ROOT="$TMPDIR/filestore" uv run memex server start &
  SERVER_PID=$!
  echo "Waiting for server on :8001..."
  until curl -sf http://localhost:8001/api/v1/vaults >/dev/null 2>&1; do sleep 1; done
  uv run memex-eval run --server {{server}} --no-llm-judge {{args}}

# Run LongMemEval external benchmark
benchmark-longmemeval dataset_path server='http://localhost:8001/api/v1/':
  uv run memex-eval longmemeval --dataset-path {{dataset_path}} --server {{server}}

# Start the benchmark server (persistent data dir, stays running)
bench-server datadir='.temp/bench-data':
  #!/usr/bin/env bash
  set -euo pipefail
  docker compose up -d db
  echo "Waiting for postgres..."
  until docker compose exec db pg_isready -U postgres -q 2>/dev/null; do sleep 1; done
  mkdir -p "{{datadir}}/filestore"
  echo "Starting server on :8001 (data: {{datadir}}/filestore)..."
  MEMEX_PORT=8001 MEMEX_SERVER__FILE_STORE__TYPE=local MEMEX_SERVER__FILE_STORE__ROOT="{{datadir}}/filestore" uv run memex server start

# Run LoCoMo external benchmark (assumes bench-server is running)
benchmark-locomo dataset_path='data/locomo' outdir='.temp/locomo-eval' server='http://localhost:8001/api/v1/' *args='':
  #!/usr/bin/env bash
  set -euo pipefail
  curl -sf {{server}}vaults >/dev/null 2>&1 || { echo "Server not running. Start with: just bench-server"; exit 1; }
  mkdir -p {{outdir}}
  echo "=== Phase 0: Ingest (skips if vault has data) ==="
  uv run memex-eval locomo-ingest -d {{dataset_path}} -s {{server}} -v {{args}}
  echo "=== Phase 1: Answer ==="
  uv run memex-eval locomo-answer -q {{outdir}}/questions.jsonl -o {{outdir}}/answers.jsonl -s {{server}} -v
  echo "=== Phase 2: Judge ==="
  uv run memex-eval locomo-judge -q {{outdir}}/questions.jsonl -a {{outdir}}/answers.jsonl -o {{outdir}}/results.json -v
  echo "=== Phase 3: Report ==="
  uv run memex-eval locomo-report -r {{outdir}}/results.json -a {{outdir}}/answers.jsonl -t {{outdir}}/traces -o {{outdir}}/report -v

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
    @echo "Recording setup complete."

# Seed demo database for recordings (requires running server)
recording-seed:
    uv run python recordings/seed-data/seed_demo_db.py

# Record CLI GIFs via asciinema + agg
record-cli:
    bash recordings/cli/record-cli.sh

# Record Claude Code + Memex integration GIF (simulated session)
record-claude-code:
    bash recordings/cli/record-claude-code.sh

# Record all GIFs (server must be running)
record-all: recording-seed record-cli record-claude-code
