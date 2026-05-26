# Contradiction detection

Memex detects contradictions in two independent stages. Both run
automatically; neither replaces the other.

## Stage 1: extraction-time linking

When a new note is ingested, the extraction pipeline compares each
newly extracted memory unit against existing units in the vault. If
two units make opposing claims about the same subject, a `contradicts`
edge is written to `memory_links`.

This runs **once per ingestion**. It is structural: the system records
"unit A contradicts unit B" as a graph edge, but does not decide which
unit is correct.

The extraction contradiction detector uses embedding similarity to
find candidate pairs, then asks the LLM whether the pair represents a
genuine semantic inversion. When the LLM confirms, the
`contradicts` link is written. If a `propose_contradiction_winner`
lint check is enabled, it later proposes which side should win.

## Stage 2: periodic lint sweep

The maintenance linter runs every six hours (or on demand via
`memex lint run`). One of its LLM checks is
`llm_semantic_contradiction`:

1. For each memory unit, compute a **surprise score** — how
   dissimilar the unit's embedding is from its nearest neighbours.
2. If the score clears the threshold (static default 0.7, or learned
   via the calibration loop), optionally run an **NLI polarity
   classifier** on the unit and its top neighbour.
3. If the combined gate clears, ask the LLM whether the unit's text
   contradicts the cited related units.
4. If the LLM says yes, write a **maintenance proposal** — a
   pending finding the human reviews in the cockpit.

The lint sweep produces a proposal, not a graph edge. The proposal
asks the human "these two units disagree — which one should stay?"
and offers canned actions: deprioritize the target unit, acknowledge
both sides, or dismiss the finding as noise.

## How they differ

| | Extraction linking | Lint sweep |
|---|---|---|
| **When** | At ingest time | Every 6 hours (or manual `memex lint run`) |
| **Output** | `contradicts` edge in `memory_links` | Maintenance proposal for human review |
| **Gate** | Embedding similarity + LLM confirmation | Surprise score + optional NLI + LLM confirmation |
| **Decides winner?** | No | No — but the `propose_contradiction_winner` follow-up check can propose one |
| **Budget** | Part of the ingestion cost | Gated by the 24h LLM lint quota (skipped on manual runs) |
| **Learns from feedback?** | No | Yes — the auto-learning loop adjusts the surprise threshold from operator verdicts |

## How they interact

They operate independently. A contradiction caught at extraction time
may or may not be flagged again by the lint sweep — it depends on
whether the surprise score clears the threshold. A contradiction
flagged by lint may not have a `contradicts` edge if extraction's
simpler heuristic didn't fire on it.

The `propose_contradiction_winner` lint check bridges the two: it
reads existing `contradicts` edges written by extraction, picks the
pair with the strongest evidence, and proposes a winner that the
operator can apply (mark the loser stale, supersede the loser's note,
or rewrite the `contradicts` link to `refines`).

## Operator workflow

1. Ingest notes normally. Extraction writes contradiction links
   automatically.
2. Periodically review the cockpit (`memex lint review`). Semantic
   contradictions appear as proposals with the `vs.` layout showing
   both sides.
3. Pick an action: deprioritize one side, acknowledge both, or
   dismiss.
4. Over time, the auto-learning loop calibrates the surprise
   threshold — rules with high dismiss rates become quieter; rules
   with high accept rates become more sensitive.
