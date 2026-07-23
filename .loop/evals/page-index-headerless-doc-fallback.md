eval: page-index-headerless-doc-fallback

**Definition of Done:** a headerless prose document above the short-doc
threshold (>2000 chars, e.g. a voice transcript) produces a page index with
≥1 block covering the full text AND populated node/block summaries, so fact
extraction runs — while documents that already yield headers are unchanged
and the existing regex-header fallback still wins when regex headers exist.

Scoring policy: all rows are deterministic assertions at a hard 100% bar.
Rows 4 and 5 are the load-bearing guardrails (fallback precedence and
no-regression); a hole in either regresses the status quo, so they must pass
100%. Row 2 encodes the operator's explicit "I still want a page index with
a summary" requirement.

Fork-dependent notes (planner's recommended resolutions; re-pin if the
operator decides otherwise):
- Ticket Q1 (route the single node through `_refine_tree_recursively`) →
  rows here assert ≥1 block, not exactly 1, so they hold whether or not a
  long doc splits. The 527-token repro stays a single block either way.
- Ticket Q2 (`path_used` marker value) → row 1 asserts a distinct fallback
  marker (recommended `'llm_scan_no_headers'`) rather than the bare
  `'llm_scan'`; re-pin the literal if the operator picks another name.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| Headerless prose over the short-doc threshold yields a non-empty page index | A ~2100-char document with no `#` markdown headers (timestamped transcript lines only), scanner mocked to find no headers, no regex headers | `PageIndexOutput` has `len(blocks) >= 1`, `coverage_ratio > 0` (≈1.0 for one whole-doc node), and `path_used` == the distinct fallback marker (`'llm_scan_no_headers'`) | Deterministic (unit test) | 100% |
| The fallback page index carries summaries | Same headerless doc; summary LM stubbed to return known section/block summary text | The fallback node's section summary field AND the block's block-summary field are populated (non-empty) after indexing — summary generation ran, not skipped | Deterministic (unit test: assert node summary != '' AND block summary != '') | 100% |
| The whole document is covered by block content | Same headerless doc | The single fallback block's `content` equals the full input text and spans `[0, len(full_text)]` (coverage 1.0 via `compute_coverage`) | Deterministic (unit test) | 100% |
| **[GUARDRAIL — fallback precedence]** Regex headers still route to the fast path | A doc where the LLM scan finds nothing BUT `detect_markdown_headers_regex` returns ≥1 header | Routes to `_fast_path` (blocks built from the regex headers); the new single-node fallback does NOT fire and `path_used` is not the fallback marker | Deterministic (unit test) | 100% |
| **[GUARDRAIL — no regression]** Documents with detected headers are unchanged | A well-structured markdown doc with multiple `#`/`##` headers | Produces the same header-driven page index as before this change (`path_used` in {`regex_fast`, `llm_scan`}, blocks from the TOC tree); the fallback branch is never entered | Deterministic (unit test) | 100% |
