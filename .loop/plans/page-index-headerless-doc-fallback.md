# page-index-headerless-doc-fallback: extract a page index (with summaries) and facts from headerless prose over the short-doc threshold

## Size / Effort

**S–M.** One localized branch in `core.py` reused via the existing
node → block → summary tail, plus unit tests. No schema, API, or config
changes. Effort is driven by wiring the fallback through the existing
summary-generation methods correctly and asserting facts flow downstream.

## Triggered by

A voice-transcription note (`Reflections w3`, 2096 chars, no markdown
headers) produced `PageIndex completed: path=llm_scan, blocks=0,
coverage=0.0%` and `Retained 0 units. Touched 0 entities.` — no page
index, no memory units. The note fell into a dead zone between the
short-doc bypass and the header-driven paths.

## Context

Fact extraction reads **only** from `page_index_output.blocks`. When a
document has no blocks, no facts are extracted:

- `engine.py:1213-1215` — `block_texts = [b.content for b in
  page_index_output.blocks]`; `if not block_texts: return [], set()`.

Blocks come from one of two places:

1. **Short-doc bypass** (`core.py:1552-1583`) — if `len(full_text) <
   short_doc_threshold and not regex_headers`, wraps the whole document
   in a single "Content" node + block, coverage 1.0,
   `path_used='short_doc_bypass'`. Note: this path does **not** run
   summary generation — the node/block carry no summaries.
2. **Header-driven paths** — blocks built from a TOC tree of detected
   headers via `generate_blocks_and_assign_ids(final_tree, ...)`
   (`utils.py:517`). Used for everything at/above the threshold.

The threshold is `short_doc_threshold_tokens (500) × CHARS_PER_TOKEN
(4) = 2000 chars` (`config.py:712`, `config.py:40`, wired at
`engine.py:843` and `engine.py:1149`). The failing note is 2096 chars,
so it missed the bypass by ~96 chars.

Above the threshold with no `#` headers, `index_document` builds an
`AsyncMarkdownPageIndex` and runs `_llm_path` (`core.py:1031`). The LLM
scanner detects **headers**; a transcript with `[00:02] Jasper:`
timestamp markers has none. With no LLM headers and no regex headers,
`_llm_path` returns an **empty** `PageIndexOutput(toc=[], blocks=[],
coverage_ratio=0.0, path_used='llm_scan')`:

- `core.py:1047-1057` — first empty return (LLM scan found nothing, no
  regex headers).
- `core.py:1074-1081` — second empty return (all headers failed
  verification, no regex fallback).

There is no "no headers → treat the whole document as one node"
fallback in the LLM path; that behavior exists **only** in the short-doc
bypass. So any headerless prose document over 2000 chars (voice
transcripts are the canonical case) silently extracts nothing.

## Non-goals / out of scope

- **No LLM-derived multi-section TOC for headerless prose.** The
  operator chose the single-section fallback. Do NOT modify the scanner
  signature/prompt to invent section titles for unstructured text.
- **Do not change the short-doc threshold** (`config.py:712`). Lowering
  or raising it is a different decision; the fix must handle headerless
  docs at *any* length above the threshold, not just re-tune the cutoff.
- **Do not alter the short-doc bypass branch** (`core.py:1552-1583`)
  beyond what is needed to share a node/block helper, if any. Its
  existing behavior and `path_used='short_doc_bypass'` marker stay.
- **No changes to fact extraction, dedup, entity linking, or reflection.**
  Once blocks exist, the existing `engine.py` pipeline handles the rest.
- **No re-ingest / backfill of existing zero-unit notes** in this ticket.

## Requirements & restrictions

1. **A headerless document above the short-doc threshold MUST yield ≥1
   block covering the full text**, so fact extraction runs
   (`engine.py:1213-1215`). This is the core bug fix.
2. **The fallback page index MUST carry summaries** — node summary(ies)
   and block summary(ies) — by running the same summary tail the normal
   paths use (`_generate_summaries_parallel` `core.py:1398`,
   `_generate_block_summaries` `core.py:1462`). This is the operator's
   explicit requirement ("I still want a page index with a summary").
   The short-doc bypass's summary-less shape is NOT acceptable here.
3. **Coverage MUST reflect the whole document** (a single node spanning
   `[0, len(full_text)]` gives coverage 1.0 via `compute_coverage`),
   not 0.0%.
4. **Reuse the existing node → block → summary machinery**
   (`generate_blocks_and_assign_ids`, `_generate_summaries_parallel`,
   `_generate_block_summaries`). Do not hand-roll a parallel path.
   Simplicity-first per `CLAUDE.md` §2 and surgical-changes §3.
5. **Preserve the regex-header fallbacks already in `_llm_path`**
   (`core.py:1048-1054`): if LLM finds nothing but regex headers exist,
   the existing `_fast_path` fallback still wins. The new single-node
   fallback only fires when there are genuinely **no** headers from
   either source.
6. **Give the fallback a distinct `path_used` marker** (e.g.
   `'llm_scan_no_headers'` or `'whole_doc_fallback'`) so the log line at
   `engine.py:1156-1160` distinguishes "fell back" from a real
   `llm_scan`. Aids future diagnosis of exactly this class of note.
7. **Every code change ships with a test** (`.claude/rules/python-testing.md`,
   `all-code-needs-tests`). Bug fix ⇒ a reproducing test first
   (headerless >2000-char doc → ≥1 block, coverage > 0).
8. **Gates must pass**: `just test` and `just prek` (`.loop/config.json`
   `gates`). Tests run via `uv run pytest`; never bare `pytest`.

## Code surface

- `packages/core/src/memex_core/memory/extraction/core.py`
  - `_llm_path` `~1047-1057` and `~1074-1081` — replace the two empty
    `PageIndexOutput(...)` returns (only when there are no regex headers
    to fall back to) with a call to a new single-node fallback helper.
  - **New helper** (private method on `AsyncMarkdownPageIndex`, e.g.
    `_whole_document_fallback(full_text) -> PageIndexOutput`): build one
    `TOCNode` spanning the whole doc (mirror the field set at
    `core.py:1555-1565`: `original_header_id=0`, `title='Content'`,
    `level=1`, `content=full_text`, `start_index=0`,
    `end_index=len(full_text)`, `token_estimate`, id via
    `content_hash_md5`/`_assign_content_hash_ids`), then run the shared
    tail: `generate_blocks_and_assign_ids` (`utils.py:517`),
    `_generate_summaries_parallel` (`core.py:1398`),
    `_generate_block_summaries` (`core.py:1462`); return
    `PageIndexOutput` with `coverage_ratio=1.0` and the new
    `path_used` marker. Reuse `block_size`/`max_node_length` already in
    scope in `_llm_path`.
  - Consider optionally routing through `_refine_tree_recursively`
    (`core.py:1328`) so very long headerless docs split into
    size-bounded blocks rather than one monster block; if included, keep
    it behind the same helper. (Recommendation: include it — a 50k-token
    transcript as a single block would strain fact-extraction chunking.)
- `packages/core/tests/unit/memory/extraction/test_page_index.py`
  - Add tests (see gates below). Existing patterns to mirror:
    `test_short_doc_bypass` (`:461`), and `_process_single_chunk`
    mocked to `return_value=[]` to simulate "no headers found"
    (`:814`, `:828`, `:934`). Summary methods can be patched with
    `AsyncMock` to keep the unit test offline.

No other files should change. Needing an edit outside this list is the
`out-of-scope-fix-needed` blocker.

## Tests & validation gates

**Eval marker (Definition of Done):** `.loop/evals/page-index-headerless-doc-fallback.md`
(5 scenarios; validated with `loopctl eval`). The eval is the acceptance
layer above the unit tests below.

**Gates (discovered from repo):**
- `just test` → `uv run pytest` over the suite (offline/fast; integration
  and llm markers excluded by default per `.claude/rules/python-testing.md`).
- `just prek` → ruff + mypy (`.pre-commit-config.yaml`). Tests are real
  code — they lint and type-check.
- Adversarial review pass (`loop-reviewer`, enabled in `.loop/config.json`).

**Tests to add (all in `test_page_index.py`, §7):**
1. **Reproducing test (write first, must fail pre-fix):** call
   `index_document` (or drive `AsyncMarkdownPageIndex` directly) on a
   >2000-char headerless document with the scanner mocked to find no
   headers; assert `len(result.blocks) >= 1`, `result.coverage_ratio >
   0`, and `result.path_used` == the new fallback marker.
2. **Summaries present:** assert the fallback node carries a section
   summary and the block carries a block summary (summary methods run,
   not skipped) — patch `_generate_summaries_parallel` /
   `_generate_block_summaries` with `AsyncMock` and assert they were
   awaited, OR assert the populated summary fields with a stubbed LM.
3. **Regex-header fallback still wins:** headerless-to-LLM but
   `regex_headers` present ⇒ still routes to `_fast_path`
   (`core.py:1048-1054`), not the new single-node fallback.
4. **Whole-doc block content:** the single block's `content` equals the
   full input text (coverage 1.0 via `compute_coverage`).
5. **(Optional, engine-level)** in `test_extraction_engine.py`: a
   headerless >2000-char doc drives `_extract_page_index` past the
   `if not block_texts` guard (`engine.py:1214`) so fact extraction is
   invoked — only if it can be done offline with the existing engine
   test harness; otherwise cite it as covered by the eval.

## Risk assessment

- **Blast radius: low.** The change adds a branch that only fires on the
  currently-broken case (no headers from either source). Documents that
  already produced headers/blocks are untouched; the regex fallback
  ordering is preserved (Req 5).
- **Reversibility: high.** Single localized helper + two call-site
  swaps; revert is clean.
- **Likeliest failure modes:**
  - Firing the fallback when regex headers exist, regressing the
    `_fast_path` fallback → guarded by Req 5 + test 3.
  - Producing a block but skipping summaries (satisfying the bug but not
    the operator's requirement) → guarded by Req 2 + test 2.
  - A single monster block for very long headerless docs straining
    fact-extraction chunking → mitigated by routing through
    `_refine_tree_recursively` (Code surface note).
  - `content_hash_md5` id collisions if two synthetic nodes share text
    — not applicable for a single whole-doc node.

## Subtickets

1. Write the reproducing unit test (test 1) and confirm it fails against
   current `_llm_path` (empty return). → verify: test red pre-fix.
2. Add the `_whole_document_fallback` helper and route both empty
   returns in `_llm_path` through it (only when no regex headers). →
   verify: test 1 green; `path_used` marker set.
3. Wire summary generation into the helper; add tests 2 and 4. →
   verify: node + block summaries populated; block content == full text.
4. Add test 3 (regex-header fallback precedence). → verify: green.
5. (Optional) engine-level test 5 / confirm eval covers the
   fact-extraction path. → verify: green.
6. Run `just test` and `just prek`; fix any lint/type fallout. →
   verify: both gates green.

## Open questions

1. **Route the single node through `_refine_tree_recursively`, or emit
   exactly one block?** *Recommendation: route through refinement* so
   long headerless transcripts split into size-bounded blocks (matching
   how the normal paths chunk), while short ones (like the 527-token
   repro) stay a single node/block. Costs a few extra LLM calls only
   when the doc exceeds `max_node_length`.
2. **New `path_used` value name.** *Recommendation:
   `'llm_scan_no_headers'`* — keeps the `llm_scan` prefix (same routing
   family) while making the log line at `engine.py:1156` self-explaining.
3. **Backfill the existing zero-unit note(s)?** Out of scope here
   (Non-goals). *Recommendation:* after this lands, re-ingest
   `Reflections w3` to confirm it now yields units — track as a
   follow-up, not part of this ticket.
