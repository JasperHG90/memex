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

# Install python dependencies and pre-commit hooks
setup: install prek_setup

# Run pre-commit
prek:
 uv run prek run -a

# Run pytest
test:
  uv run pytest tests

# Create qmd collections
collections:
  qmd collection add . --name memex_test --mask "**/*.{py,toml,yaml}"
  qmd collection add . --name memex_src --mask "**/test_*.py"
  qmd collection add . --name memex_md --mask "**/*.md"

# Embed code (initial/new docs)
embed_collections:
  qmd embed

# Embed code (update)
update_collections: embed_collections
  qmd update
