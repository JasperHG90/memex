#!/usr/bin/env bash
# Removal-completeness CI gate for the FSRS rip-out.
#
# Asserts that no FSRS-revisit residue remains in active code or tests after
# the FSFM scorer landed. Migrations 026 / 030 / 035 and their integration
# tests are exempt — they intentionally reference the column names because
# 026/030 added them, 035 drops them, and the tests verify those exact
# migration shapes. test_seed_alembic_stubs.py is exempt for the same
# reason: it asserts the migration chain by revision name.
#
# Exits non-zero on the first banned pattern detected and prints the matches.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

# Files that legitimately mention revisit columns by name and must be
# excluded from this check. Migrations are immutable historical records;
# their tests verify the migration's exact shape.
EXEMPT_PATHS=(
    'packages/core/src/memex_core/alembic/versions/026_revisit_columns.py'
    'packages/core/src/memex_core/alembic/versions/030_revisit_last_reviewed_at.py'
    'packages/core/src/memex_core/alembic/versions/031_maintenance_proposals_resolved_by.py'
    'packages/core/src/memex_core/alembic/versions/035_drop_fsrs_revisit_columns.py'
    'packages/core/tests/integration/test_int_alembic_026.py'
    'packages/core/tests/integration/test_int_alembic_030.py'
    'packages/core/tests/integration/test_int_alembic_035_drop_fsrs.py'
    'packages/core/tests/unit/test_seed_alembic_stubs.py'
)

# Build a single regex from the exempt list for grep -E.
EXEMPT_REGEX="$(IFS='|'; printf '%s' "${EXEMPT_PATHS[*]}")"

# Use rg when available (faster, default-ignores .git), else grep -rE.
if command -v rg >/dev/null 2>&1; then
    SEARCH() {
        rg --no-messages -n "$1" packages/ tests/ 2>/dev/null \
            | grep -Ev "^(${EXEMPT_REGEX}):" || true
    }
else
    SEARCH() {
        grep -rEn --include='*.py' --include='*.toml' --include='*.md' \
            --include='*.json' --include='*.sh' --include='*.yml' \
            --include='*.yaml' "$1" packages/ tests/ 2>/dev/null \
            | grep -Ev "^(${EXEMPT_REGEX}):" || true
    }
fi

BANNED_PATTERNS=(
    'from fsrs'
    'import fsrs'
    'py-fsrs'
    'RevisitConfig'
    'RevisitationService'
    'DueUnitDTO'
    'memex_get_due_for_review'
    'memex_memory_review'
    'periodic_revisit_task'
    '_fetch_revisit_due_count'
    'revisit_due_at'
    'revisit_stability'
    'revisit_difficulty'
    'revisit_review_count'
    'revisit_last_reviewed_at'
)

BANNED_QUALITY_REGEX='Quality\.(AGAIN|HARD|GOOD|EASY)'

failed=0

for pattern in "${BANNED_PATTERNS[@]}"; do
    matches="$(SEARCH "$pattern")"
    if [[ -n "$matches" ]]; then
        echo "FSRS residue: '$pattern' still appears in:" >&2
        echo "$matches" >&2
        failed=1
    fi
done

quality_matches="$(SEARCH "$BANNED_QUALITY_REGEX")"
if [[ -n "$quality_matches" ]]; then
    echo "FSRS residue: 'Quality.<rating>' still appears in:" >&2
    echo "$quality_matches" >&2
    failed=1
fi

# Verify py-fsrs is no longer pinned in any package's pyproject.toml.
toml_matches="$(grep -rEn '"fsrs(>=|==|~=|>)' packages/*/pyproject.toml 2>/dev/null || true)"
if [[ -n "$toml_matches" ]]; then
    echo "FSRS residue: 'fsrs' dependency still pinned in:" >&2
    echo "$toml_matches" >&2
    failed=1
fi

if [[ "$failed" -ne 0 ]]; then
    echo >&2
    echo "FSRS removal-completeness check FAILED. The references listed above" >&2
    echo "must be deleted before this PR can merge." >&2
    exit 1
fi

echo "FSRS removal-completeness check passed: no residue found."
