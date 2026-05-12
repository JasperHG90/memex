# Resolve Contradictions

Memex's lint pipeline detects when two memory units about the same fact
disagree. When the disagreement carries a high enough signal, the
LLM-driven lint pass proposes a winner, a recommended action, and a
calibrated confidence — all packaged as a normal lint finding you can
review, apply, or dismiss like any other.

This guide walks through the full loop:

1. Confirm a pending winner-proposal finding exists.
2. Apply the recommended action.
3. Reverse the action if the apply was premature.

## Prerequisites

- A vault that has accumulated contradictions (typically when the FSFM
  lint pass has emitted `composite_deprioritize_candidate` findings
  whose `flag_reason` is `low_credibility_contradiction_only` or
  `components_disagree`).
- The LLM-gated lint pass enabled
  (`server.memory.lint_llm.enabled=true`) and the per-check flag
  `server.memory.lint_llm.checks.propose_contradiction_winner.enabled=true`.

## 1. Find pending winner-proposal findings

```bash
memex lint findings --vault my-vault --type quality
```

In the output, look for rows whose `rule_name` is
`propose_contradiction_winner`. Each row's `evidence` payload carries:

- `winner_unit_id` / `loser_unit_id` — the two units in tension.
- `action` — one of `mark_loser_stale`, `supersede_loser_note`,
  `refine_not_contradict`, `inconclusive`.
- `confidence` — the LLM's calibrated confidence in the proposal.
- `rationale` — a one- or two-sentence justification.
- `linked_to_finding` — the upstream FSFM finding that triggered this
  proposal.

## 2. Apply the recommended action

```bash
memex lint apply <finding_id>
```

The action drives the mutation:

| Action                  | Effect                                                                                                                  |
|-------------------------|-------------------------------------------------------------------------------------------------------------------------|
| `mark_loser_stale`      | Flips `MemoryUnit.status` of the loser to `'stale'`.                                                                    |
| `supersede_loser_note`  | Sets `Note.superseded_by` of the loser's parent note to the winner's parent note id. Falls back to `mark_loser_stale` when both units share the same parent note (the fallback reason is recorded under `evidence.resolution.fallback_reason`). |
| `refine_not_contradict` | Rewrites the inbound `MemoryLink.link_type` from `'contradicts'` to `'refines'` (graph-pressure weight `0.0`).          |
| `inconclusive`          | No-op write; flips the finding to `resolved`.                                                                           |

Each apply captures the affected row's pre-mutation state under
`evidence.resolution.prior_state` so the action is fully reversible.

## 3. Reverse a previously applied action

```bash
memex lint reverse <finding_id>
```

The reverse path reads `evidence.resolution.prior_state` and atomically
restores the affected row(s) in a single transaction. It also writes a
paired audit row (`rule_name=propose_contradiction_winner_reversal`) so
the audit trail records both the apply and the reverse. The original
finding stays in `resolved` status — the unique partial index on
pending findings is preserved.

## Agent surfaces

The same actions are available via MCP and the Hermes plugin:

- MCP: `memex_lint_apply_winner(finding_id)` /
  `memex_lint_reverse_winner(finding_id)`.
- Hermes: `memex_lint_apply_winner` / `memex_lint_reverse_winner`
  (registered alongside `memex_get_lint_flags`).

The Claude Code `recall` skill is wired to call
`memex_lint_apply_winner` after surfacing the proposal to the user, so
the agent does not auto-apply silently.

## Tuning

- `server.memory.lint_llm.propose_winner_min_confidence` — minimum
  DSPy-reported confidence for a definitive winner verdict. Definitive
  (`unit_a` / `unit_b`) verdicts with `confidence < min_confidence` are
  emitted with `action='inconclusive'` so the audit trail survives but
  the apply path is blocked. Increase the threshold to require higher
  LLM confidence before mutations can land; decrease to permit
  lower-confidence verdicts. Inconclusive verdicts from the LLM itself
  are always emitted regardless of confidence. Env:
  `MEMEX_SERVER_LINT_LLM_PROPOSE_WINNER_MIN_CONFIDENCE`. Default `0.6`.
- `server.memory.lint_llm.checks.propose_contradiction_winner.enabled`
  — per-check feature flag.
