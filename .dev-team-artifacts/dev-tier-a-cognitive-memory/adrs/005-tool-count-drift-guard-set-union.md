# ADR-005: Tool-Count Drift-Guard Reconciliation by Set Union Across Multi-Stream Merges

## Status

Accepted

## Context

The Hermes plugin and the MCP surface both ship a "drift guard" — a strict-equality assertion on the number of registered tools (and the exact tool name set). The guard exists so that any unintended addition or removal of a tool fails CI loudly, instead of silently expanding the agent surface.

In a multi-stream dev-team workflow, several feature branches independently land tools and bump the guard. Each branch sees only its own additions plus the base set. When two such branches merge into the integration branch, the three-way merge produces a textual conflict on:

1. The numeric count assertion (`assert len(tools) == N`).
2. The exact-set assertion (`assert tool_names == { ... }`).

Naively taking either side of the conflict drops the OTHER branch's feature from the guard, which then accepts a regression that removes that feature without failing.

This pattern was first observed during the F38/F6 substrate merges and re-applied for F20/F9 surface merges. It needs to be a written rule, not tribal knowledge.

## Decision

When multiple feature branches independently bump the Hermes tool-count drift guard, three-way merge conflicts on the count and the name set are resolved by **set union across both feature clusters**, never by taking either side wholesale. The numeric count assertion is reconciled to `len(union)`.

Demonstrated in `packages/hermes-plugin/tests/test_provider.py` and `packages/hermes-plugin/tests/test_tools.py`. Applies anywhere strict-equality assertions over an evolving set are merged across parallel streams.

## Consequences

**Positive:**
- Each merge preserves every feature's contribution to the guard.
- The guard remains strict: post-merge, removing any tool from any merged feature still fails CI.
- The rule is mechanical — reviewers can verify by listing the tools added on each side and checking the result is the union.

**Negative:**
- Requires manual conflict resolution with explicit awareness of what each side added; cannot be auto-resolved by `git merge -X ours` or `theirs`.
- Easy to get wrong under time pressure. Mitigated by a CI step that runs the drift guard immediately after merge and by a PR-checklist item.

## Alternatives Considered

- **Replace strict equality with a "contains at least" assertion** — rejected: defeats the purpose of the drift guard, which exists to catch unintended additions as well as removals.
- **Generate the expected set at runtime from the registry** — rejected: the assertion would always pass; the test would no longer detect drift.
- **Single-stream feature delivery** — rejected: parallel work is the whole point of the dev-team workflow.
