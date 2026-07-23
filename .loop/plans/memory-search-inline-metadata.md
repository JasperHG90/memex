# memory-search-inline-metadata: opt-in inline note metadata on memex_memory_search

## 1. Title

Add an opt-in `include_metadata: bool = False` parameter to
`memex_memory_search` that inlines per-note metadata into the results, so a
caller can get search results plus note metadata in one round trip and the
tool stops diverging from `memex_note_search` (which already returns metadata
inline).

## 2. Size / Effort

**S.** One new boolean parameter, one optional nested field on the shared
memory-unit model, and a population branch that reuses metadata the tool
*already fetches*. The dominant work is not new plumbing but three
constraints: (a) keeping the default path byte-identical, (b) picking one
metadata shape and not inventing a third, and (c) landing the gating test in
the one test directory the loop actually collects. No service-layer, HTTP, or
retrieval change.

## 3. Triggered by

A documented agent-surface asymmetry stated in
`.claude/rules/memex-agent-surface.md:38`: "After `memory_search`: call
`memex_get_notes_metadata`. After `note_search`: metadata is inline — do NOT
call `memex_get_notes_metadata`." (Restated at
`.claude/rules/memex-agent-surface.md:214`.) `memex_memory_search` forces a
mandatory second round trip that `memex_note_search` does not. The request is
to remove that asymmetry behind an opt-in flag.

## 4. Context

`memex_memory_search` is defined at
`packages/mcp/src/memex_mcp/server.py:1565`, returning
`list[McpFact | McpEvent | McpObservation]`
(`packages/mcp/src/memex_mcp/server.py:1710`). Each result carries a
`note_id` but no note-level metadata block; the agent must follow up with
`memex_get_notes_metadata`
(`packages/mcp/src/memex_mcp/server.py:2321`) to get title, tags,
`total_tokens`, `has_assets`, etc.

Key finding that makes this Size S and defuses the perf concern: the tool
**already** calls `api.get_notes_metadata(note_ids)` on every non-empty
search at `packages/mcp/src/memex_mcp/server.py:1793`, but only to extract
`title` into `note_titles` (`server.py:1794-1800`). The full metadata dicts
are already in hand and then discarded. Inlining them behind the flag reuses
that existing fetch — it adds **no new hydration or JOIN** for the common
case where `note_ids` is already collected. (`api.get_notes_metadata` is a
thin delegate to `NoteService`: `packages/core/src/memex_core/api.py:1316`.)

The per-unit model is `McpMemoryUnitBase`
(`packages/mcp/src/memex_mcp/models.py:111`), the shared base of `McpFact`,
`McpEvent`, and `McpObservation` (`models.py:156`, `:162`, `:170`). Units are
built from `MemoryUnitDTO` by `_build_memory_unit_model`
(`packages/mcp/src/memex_mcp/server.py:1436`), which currently receives only
`note_titles` (`server.py:1840`), not the full metadata.

The metadata object that `memex_get_notes_metadata` returns is
`McpNoteMetadata` (`packages/mcp/src/memex_mcp/models.py:272`): `note_id`,
`title`, `total_tokens`, `vault_name`, `tags`, `has_assets`, `created_at`,
`publish_date` — constructed from the same `get_notes_metadata` dicts at
`packages/mcp/src/memex_mcp/server.py:2374-2385`. `memex_note_search`
(`packages/mcp/src/memex_mcp/server.py:1906`) inlines a *flat, partly
different* field set directly onto its result model `McpNoteSearchResult`
(`packages/mcp/src/memex_mcp/models.py:213`; populated at `server.py:2115`):
it has `description`, `source_uri`, `status` that `McpNoteMetadata` lacks, and
lacks `total_tokens` that `McpNoteMetadata` has. This shape mismatch is a fork
— see §11.

**Why this matters (value, not just convenience).** The load-bearing field
is `total_tokens`: it gates the read-routing decision — the doctrine reads a
source note via `read_note` only when `total_tokens < 500`, otherwise it must
paginate via `get_page_indices` + `get_nodes`. So today an agent cannot decide
how to read any `memory_search` result without a second `get_notes_metadata`
round trip. Inlining removes that mandatory size-check on every search. The
other `McpNoteMetadata` fields are minor; `total_tokens` is the reason.

**Scope decision (operator, 2026-07-23): two-tier, `note_total_tokens` +
`node_has_assets`.** The flag adds exactly two lean fields per unit, matching the
MU → Node → Note hierarchy:
- `note_total_tokens: int | None` — **note-level**, the parent note's size (gates
  read_note-vs-paginate).
- `node_has_assets: bool` — **node-level**, whether the section(s) this unit came from
  contain an image, computed as `bool(unit.node_ids ∩ note.asset_node_ids)`.

Both ride the `get_notes_metadata(note_ids)` fetch the tool ALREADY makes on every
non-empty search (`server.py:1793`) — no new query. `total_tokens` is already in that
payload; `asset_node_ids` is added to it by the prerequisite ticket
`surface-node-assets-in-note-metadata` (node-hash-space ids, intersectable with a
unit's `node_ids`). Every other `McpNoteMetadata` field (title, tags, created_at, …)
is omitted — redundant with the unit or pure token cost.

**DEPENDENCY:** this ticket depends on `surface-node-assets-in-note-metadata` landing
first (it provides `asset_node_ids` in note metadata). `note_total_tokens` alone does
not need it; `node_has_assets` does.

**Scope note — note-level vs node-level.** `note_total_tokens` describes the parent
*note*: because multiple units share a note, the same value attaches to every unit from
that note (the field name makes the scope explicit). `node_has_assets` is genuinely
*per-unit*: two units from the same note can differ (different sections). It is derived
in-memory by intersecting each unit's `node_ids` with the note's `asset_node_ids` — no
extra query, since both sides are already in hand.

## 5. Non-goals / out of scope

- Do NOT change `memex_note_search` or `McpNoteSearchResult` in any way.
- Do NOT change the default (`include_metadata=False`) output of
  `memex_memory_search` — it must stay byte-identical (§8).
- Do NOT deprecate, alter, or touch `memex_get_notes_metadata`, other than
  updating its description string to reflect that inline is now opt-in on
  `memory_search`.
- Do NOT alter ranking, retrieval, `api.search`, the service layer, or any
  HTTP endpoint. This flag lives entirely in the MCP tool layer.
- Do NOT touch `token_budget`, `slim`, dedup, or degradation-warning
  behavior beyond attaching metadata to the already-built units.
- No new metadata shape invented beyond the one chosen in §11 Q1.

## 6. Requirements & restrictions

R1. Add `include_metadata: bool = False` to `memex_memory_search`
(`packages/mcp/src/memex_mcp/server.py:1565`), annotated with a
`Field(description=...)` matching the surrounding parameter style (e.g.
`server.py:1600-1611`), coerced with `_coerce_bool` like the other bool
params (`server.py:1596-1599`).

R2. When `include_metadata=True`, attach a note-metadata block to each
returned unit, reusing the `metas` already fetched at
`packages/mcp/src/memex_mcp/server.py:1793`. Do NOT add a second
`get_notes_metadata` call.

R3. When `include_metadata=False`, the returned payload MUST be byte-identical
to today's. Because the models do not set `exclude_none`
(`packages/mcp/src/memex_mcp/models.py` has only per-model `extra: 'forbid'`
configs, e.g. `models.py:483`) and FastMCP serializes the model as-is (see
`parse_tool_result`, `packages/mcp/tests/helpers.py:9`), a new
`note_metadata: ... | None = None` field would otherwise emit
`"note_metadata": null` on every unit and break byte-identity. The
implementer MUST suppress the key when unset (e.g. a field/model serializer
that drops it when `None`) and prove it with the test in §8. This is the
load-bearing restriction; see §11 Q2.

R4. Reuse the existing metadata shape, not a new one. Per repo principle
"Simplicity First / no third shape" (`CLAUDE.md` §2) and the request's
explicit instruction not to invent a third shape.

R5. Repo principles that bound the change:
- Surgical changes only; every changed line traces to this request
  (`CLAUDE.md` §3).
- Every code change ships with a test; assertions exercise real behavior
  (`.claude/rules/python-testing.md`, `<constraint
  name="all-code-needs-tests">`).
- Do not silence gates; fix causes, no `# type: ignore` / `skip`
  (`.claude/rules/prek-code-quality.md`, `.claude/rules/python-testing.md`).
- Docs touched (the agent-surface rule + tool description strings) must pass
  the slop-scan checks in `.claude/rules/slop-scan-for-docs.md`.
- Run an adversarial sub-agent review before declaring done
  (`.claude/rules/adversarial-reviews.md`).

R6. Update the two prose surfaces that assert the old asymmetry so they no
longer misdescribe the tool:
- `.claude/rules/memex-agent-surface.md:38` and `:214` — note the follow-up
  is now optional / avoidable via `include_metadata=True`.
- The `memex_get_notes_metadata` description string at
  `packages/mcp/src/memex_mcp/server.py:2325-2326` ("Use after
  memex_memory_search to filter results before reading").
- The `memex_memory_search` description block (`server.py:1553-1560`) should
  mention the new flag.

## 7. Code surface

- `packages/mcp/src/memex_mcp/server.py:1565` — `memex_memory_search`
  signature: add `include_metadata` param.
- `packages/mcp/src/memex_mcp/server.py:1788-1801` — the block that already
  fetches `metas`; retain the `metas` list (not just titles) for reuse when
  the flag is set.
- `packages/mcp/src/memex_mcp/server.py:1826-1842` — the output loop /
  `_build_memory_unit_model` call site: thread the metadata through so it
  lands on each unit when the flag is set. Also handle the
  `previously_returned` compressed branch (`server.py:1830-1838`) — decide
  whether metadata attaches there too (recommend: no, keep compressed units
  minimal; state in the test).
- `packages/mcp/src/memex_mcp/server.py:1436` — `_build_memory_unit_model`:
  extend to accept and set the optional metadata block (or attach it at the
  call site — implementer's choice, keep it surgical).
- `packages/mcp/src/memex_mcp/models.py:111` — `McpMemoryUnitBase`: add the
  optional `note_metadata` field with None-suppressing serialization (R3).
- `packages/mcp/src/memex_mcp/models.py:272` — `McpNoteMetadata`: the shape
  to reuse (no change expected unless Q1 resolves otherwise).
- `packages/mcp/src/memex_mcp/server.py:2325-2326` — `get_notes_metadata`
  description string (R6).
- `packages/mcp/src/memex_mcp/server.py:1553-1560` — `memory_search`
  description string (R6).
- `.claude/rules/memex-agent-surface.md:38`, `:214` — asymmetry prose (R6).
- **`tests/test_memory_search_inline_metadata.py`** (NEW, root `./tests/`) —
  the gating test. See §8 for why it must live here and how it exercises the
  tool.

## 8. Tests & validation gates

**Eval marker (acceptance layer):** `.loop/evals/memory-search-inline-metadata.md`
— 4 deterministic scenarios at 100%. Guardrails: default output byte-identical; no null-metadata leak.
Fork-dependent row: inlined shape (Q1→get_notes_metadata/McpNoteMetadata shape).

Gates (verified this session):
- `just test` → `uv run pytest tests` (`justfile:65-66`). Collects the root
  `./tests/` directory ONLY; `packages/**/tests` are NOT collected by this
  recipe. `addopts` excludes integration by default
  (`pyproject.toml:78`: `-m 'not integration'`).
- `just prek` → `uv run prek run -a` (`justfile:61-62`) — ruff/mypy/etc. per
  `.pre-commit-config.yaml`.

Critical placement constraint: the existing `memex_memory_search` tool tests
use `mock_api` + `mcp_client` fixtures and `parse_tool_result`
(`packages/mcp/tests/test_mcp_server.py:108`, fixtures at
`packages/mcp/tests/conftest.py:17` and `:137`, helper at
`packages/mcp/tests/helpers.py:9`). Those live under `packages/mcp/tests/`,
which `just test` does NOT collect. **The loop-gating behavior test MUST live
in root `./tests/`** so the gate actually runs it.

New test file `tests/test_memory_search_inline_metadata.py`, offline and
non-integration (default gate must stay fast/offline per
`.claude/rules/python-testing.md`), must assert:

(a) **Default output unchanged.** Call `memex_memory_search` with
`include_metadata` omitted / False against a fake `api` whose `.search`
returns a `MemoryUnitDTO` and `.get_notes_metadata` returns a metadata dict;
assert the serialized unit has NO `note_metadata` key (byte-identity guard
for R3).

(b) **Flag true inlines metadata.** With `include_metadata=True`, assert each
unit carries the note-metadata block with the chosen shape (Q1) and the same
field values the fake `.get_notes_metadata` returned, and that
`.get_notes_metadata` was called exactly once (no extra round trip, R2).

Recommended harness (avoids importing the not-collected `packages/mcp`
conftest and avoids Docker): invoke the tool's underlying function via its
`.fn` attribute with a lightweight fake/`MagicMock` `api` and a stub `ctx`.
The server explicitly supports this direct-call path — see the comment at
`packages/mcp/src/memex_mcp/server.py:1730-1746` describing "internal Python
callers invoking the underlying `.fn`". `get_api(ctx)`
(`server.py:1713`) and `_default_read_vaults` / `_resolve_vault_ids`
(`server.py:1714-1718`) must be satisfiable by the stub; the implementer
confirms the minimal stub shape while wiring the test. If `.fn` proves
impractical, the fallback is to lift the `mock_api`/`mcp_client` pattern into
root `./tests/` — but keep it offline and unmarked. This harness choice is
Q3.

Every test named here has its home declared in §7
(`tests/test_memory_search_inline_metadata.py`).

## 9. Risk assessment

- **Blast radius.** `McpMemoryUnitBase` is the base for all three memory-unit
  result models and is returned by `memex_memory_search`,
  `memex_search_user_notes` (`server.py:1864`, which delegates to
  `memex_memory_search`), and any other consumer of these models. Adding an
  optional field touches the shared model, so the byte-identity guard (R3)
  protects every one of those consumers. Low if R3 holds; the default path is
  unchanged.
- **Reversibility.** High. The flag defaults False and the field is additive;
  reverting is deleting the param, the field, and the population branch.
- **Likeliest failure modes.** (1) `"note_metadata": null` leaking into the
  default payload and breaking byte-identity (R3) — caught by test 8(a). (2)
  Landing the test under `packages/mcp/tests/` where the loop gate never runs
  it — caught by requiring the root path in §7/§8. (3) Choosing a metadata
  shape that duplicates note_search's flat fields and creates a third variant
  — pinned by Q1. (4) A `search_user_notes`-style delegator not passing the
  new param through — the delegator at `server.py:1881` passes only a subset;
  confirm it need not forward `include_metadata` (it currently cannot set it,
  which is fine — default False).

## 10. Subtickets

1. Decide Q1 (metadata shape) and Q2 (None-suppression mechanism) — these
   gate the model change. (Operator/implementer, before code.)
2. Add the optional `note_metadata` field to `McpMemoryUnitBase` with
   None-suppressing serialization (`models.py:111`).
3. Add the `include_metadata` param to `memex_memory_search` and populate the
   field from the already-fetched `metas`, reusing `server.py:1793`
   (`server.py:1565`, `:1788-1842`, `_build_memory_unit_model` at `:1436`).
4. Write `tests/test_memory_search_inline_metadata.py` asserting 8(a) and
   8(b); make `just test` pass.
5. Update the prose/description surfaces (R6): agent-surface rule lines 38 and
   214, `get_notes_metadata` and `memory_search` description strings.
6. Run `just prek`, run the slop-scan checks on the edited `.md`, and run the
   adversarial review.

## 11. Open questions

**Q1 — RESOLVED (operator, 2026-07-23): two-tier, `note_total_tokens: int | None`
(note-level) + `node_has_assets: bool` (node-level).** Not the full `McpNoteMetadata`
or `note_search` field set — just these two flat fields. `note_total_tokens` is read
from the `metas` already fetched at `server.py:1793`. `node_has_assets` =
`bool(unit.node_ids ∩ note.asset_node_ids)`, where `asset_node_ids` is added to note
metadata by the prerequisite ticket `surface-node-assets-in-note-metadata` — so this
ticket is SEQUENCED AFTER it. No extra query for either field.

**Q2 — How to keep the default payload byte-identical when adding a field?**
The models do not set `exclude_none`, so a naive `note_metadata: ... | None =
None` emits `"note_metadata": null` everywhere. *Recommendation:* add the
field but suppress it when `None` via a Pydantic v2 serializer (a
`@field_serializer`/`@model_serializer` that omits the key when unset), and
lock the behavior with test 8(a). Do not switch the whole model to
`exclude_none` — that would drop other legitimately-null fields (e.g.
`created_at`, `event_date`, `staleness` at `models.py:126,140,141`) and change
existing output.

**Q3 — Test harness: `.fn` direct-call vs. lifting `mcp_client` into root
`./tests/`?** *Recommendation:* prefer the `.fn` direct-call with a stub
`api`/`ctx` (supported per `server.py:1730-1746`) because it is self-contained
and offline; fall back to porting the `mock_api`/`mcp_client` fixtures only if
`.fn` proves impractical. Either way the file lives in root `./tests/` and
carries no `integration` marker.

**Q4 — Attach metadata to `previously_returned` (compressed) units?** The
compressed branch (`server.py:1830-1838`) deliberately strips units to
`{id, note_id, note_title, previously_returned}` to save tokens.
*Recommendation:* do NOT attach metadata there; keep compression intact and
assert the compressed shape is unchanged. Surface for the operator in case the
intent is full metadata even on re-returns.
