# About session briefings

When an agent boots into a Memex-backed session, the first turn is the most expensive. The agent has no idea what's in the vault, which conventions you've stored, what the top entities are, or which procedures you expect it to follow. It can find out — by calling `memex_get_vault_summary`, `memex_kv_list`, and a handful of survey tools — but only after you've already asked it a question. By the time it answers, you've paid for two or three discovery round-trips on every cold start.

A **session briefing** is the markdown blob Memex injects into the agent's system prompt *before* the first user turn. It is a pre-rendered, token-budgeted snapshot of the things an agent almost always needs to know up front: the vault narrative, top mental models, KV facts, procedures, and the list of vaults available. The agent reads it once and answers from it.

A briefing for a small project vault looks roughly like this:

```markdown
# Session Briefing

12 notes | 8 entities | 3 added this week | 4 mental models | Updated 2026-05-19 | v3

## Key-Value Facts

- `user:editor`: neovim
- `project:memex:lang:python`: 3.12
- `project:memex:style:line-length`: 100

## Procedures

- **answer:session-briefing** — When the user asks "what's in this vault",
  answer from the sections rendered above; do not re-fetch with
  memex_get_vault_summary or memex_kv_list.

## Vault Overview

This vault holds engineering notes for the Memex monorepo — extraction
pipeline, retrieval scoring, reflection, and the agent-surface
architecture. Active themes this week: agent surface refactor and the
new briefing service.

- ↑ **agent-surface** (8): Three-tier prompt architecture; SSOT in memex_common.
- → **retrieval** (4): TEMPR — five strategies + RRF fusion + MMR diversity.

## Top Entities

- **VaultSummary** (concept, 3 obs) — Per-vault narrative refreshed hourly.
  ★ Briefing service consumes the persisted row
  Last seen: 2026-05-19

## Available Vaults

- **memex** — Engineering notes (12 notes) **(active)**
- **personal** — Day-to-day life (47 notes)

---
*Vault: 9b6e... | Project: memex*
```

The agent now knows the editor preference, the project's Python version, the procedural rule that tells it how to handle vault-overview questions, the current vault theme, and the two vaults it can read from — all without calling a single tool.

This page explains what a briefing contains, how it gets composed, why it's pre-rendered instead of fetched on demand, and what happens when it doesn't quite fit the budget.

## Context: the cold-start problem

Imagine you open Claude Code in a project where you've stored four hundred notes, twenty entity mental models, and a dozen KV procedures. You type: "Which framework do we use for this project?" A naive agent will hit MCP three times — `memex_kv_list`, then `memex_kv_search`, then `memex_kv_get` — before answering. That's three round-trips for a question that should be one.

Multiply that across the first dozen messages of a session. Every "what's in this vault?", every "who's the most-mentioned person?", every "do we have a procedure for X?" walks the same discovery loop. Most of those questions resolve to the same dozen facts. They belong in the prompt, not in tool calls.

The briefing solves this by treating the agent's system prompt as a place to cache information the agent will reach for repeatedly. The trick is that the cache lives inside the prompt, not inside a tool call. Once the briefing is in context, the agent can answer "what's in this vault?" or "which procedure governs deploys?" without any tool call at all.

This is reflection turned outward. The 7-phase reflection loop synthesises mental models for each entity. The vault-summary service rolls those models, plus inventory counts and recent activity, into a `VaultSummary` row. The briefing service then composes that summary with KV state and harness-specific framing, and hands the result to the agent harness. Facts in, synthesis out, snapshot persisted, snapshot consumed — the same pattern as reflection itself, scoped to a whole vault.

The result is a system where reflection's expensive nightly work pays out on every single session boot, not just when the agent happens to ask for a survey.

## Model: three tiers, two harnesses, one budget

Three layers of prose stack to make the prompt the agent sees on turn one. Each layer has a different cadence and a different source of truth.

| Layer | Where it lives | What it carries | Cadence |
|---|---|---|---|
| Tier 1a — transport | MCP `instructions=` + per-tool `description=` | The bare protocol facts an MCP client needs | Recomputed each turn |
| Tier 1b — universal | `compose_universal()` in `memex_common.agent_surface` | Storage model, retrieval routing, KV namespace rules, citation discipline | Deterministic; same bytes every session |
| Tier 2 — per-agent | `HERMES_HARNESS` and `CLAUDE_CODE_HARNESS` in `memex_common.agent_harnesses` | Slash commands, capture cadence, prohibitions specific to the harness | Deterministic per harness |
| Briefing — per-vault | `SessionBriefingService.generate()` in `memex_core.services.session_briefing` | Vault overview, top mental models, KV facts, procedures, available vaults | Refreshed by the scheduler |

The briefing is the only one of these that changes between sessions for the same vault. Tier 1b and Tier 2 are byte-identical given the same harness — that determinism is what lets the prompt-prefix cache survive across sessions. <code-ref path="packages/cli/src/memex_cli/agent_surface.py" lines="29-31" />

The briefing sits *after* the deterministic prefix. Whatever bytes follow the briefing in the assembled system prompt — vault binding, session-note instructions, auto-tag metadata — get re-emitted each session, so the cache boundary is "everything before the briefing".

```
┌────────────────────────────────────────────┐
│ Tier 1b: compose_universal()               │  cached
│ Tier 2:  CLAUDE_CODE_HARNESS               │  cached
├────────────────────────────────────────────┤
│ Briefing: vault narrative + KV + models    │  refreshed
│ Vault binding + session note + auto-tag    │  per-session
└────────────────────────────────────────────┘
```

Tier 2 — newly shipped — includes one rule that depends on the briefing being present. The `answer_from_briefing` constraint tells the agent that if it sees vault overview, themes, top entities, KV facts, procedures, or available-vaults sections rendered above, it should answer overview-shape questions from those sections rather than re-fetching them. <code-ref path="packages/common/src/memex_common/agent_harnesses.py" lines="91-93" /> The briefing isn't decorative: the agent's harness is telling it to *use* what's been pre-loaded.

## Mechanism: what happens when a Claude Code session starts

Trace one cold start through Claude Code. You open the editor in a project. Claude Code fires the `SessionStart` hook script. The script runs entirely in bash and never imports Python in-process — it talks to Memex through the `memex` CLI.

**Step 1 — resolve where you are.**
The hook reads the Claude Code session payload from stdin (model, session id) and resolves your git project root.
It then calls two shell helpers, `memex_resolve_project_id` and `memex_resolve_active_vault`, to find your project id and the vault bound to it.
The hook caches both on disk so later per-session hooks (PreToolUse, PreCompact) don't pay the round-trip again.
<code-ref path="packages/claude-code-plugin/scripts/on_session_start.sh" lines="80-87" />

**Step 2 — install the static surface.**
The hook calls `memex agent-surface claude-code --output-dir <project>/.claude/rules`.
The CLI writes the Tier 1b + Tier 2 composition to `memex-agent-surface.md` in your project's rules directory.
The write is atomic, and the CLI skips the rewrite if the bytes haven't changed.
Claude Code loads the rules file at boot, so the static surface becomes part of the system prompt without travelling through `additionalContext`.
<code-ref path="packages/claude-code-plugin/scripts/on_session_start.sh" lines="102-115" />

(This used to ship inline through `additionalContext` until Claude Code 2.1 started silently truncating that field above 10K characters. Rules-directory delivery sidesteps the cap.)

**Step 3 — fetch the briefing.**
Unless `MEMEX_CC_SESSION_BRIEFING=off`, the hook calls `memex briefing --vault <name> --project-id <id> --budget 2000`.
The CLI resolves the vault, hits the server's `/sessions/briefing` endpoint, and prints the rendered markdown to stdout.
<code-ref path="packages/claude-code-plugin/scripts/on_session_start.sh" lines="117-153" />

If the server is unreachable, the hook emits a `systemMessage` telling you to start it and exits cleanly — no half-broken `additionalContext`.
<code-ref path="packages/claude-code-plugin/scripts/on_session_start.sh" lines="143-148" />

**Step 4 — server composes the briefing.**
Inside the server, `SessionBriefingService.generate()` fetches four things in parallel:

- the `VaultSummary` row,
- the vault's mental models, sorted by trend-weighted importance,
- KV entries across the briefing's namespaces (`global`, `user`, `app:claude-code`, `procedure`, and `project:<id>` when scoped),
- and the list of available vaults.

<code-ref path="packages/core/src/memex_core/services/session_briefing.py" lines="143-165" />

It then assembles six sections in priority order: header, KV facts, procedures, vault overview, top mental models, and available vaults.
<code-ref path="packages/core/src/memex_core/services/session_briefing.py" lines="186-220" />

**Step 5 — overflow trim if needed.**
The composed sections get a `len(text) // 4` token estimate.
If the estimate is over the budget (default 2000 tokens), the service walks a fixed degradation ladder:

1. Trim mental models from 10 to 7 to 5.
2. Drop observation trends from the models.
3. Compact the vault overview to bare theme names.
4. Drop the overview entirely.
5. Drop KV namespaces in `app: → user: → project:` order.
6. Trim procedures oldest-first.
7. Drop the available-vaults section.

<code-ref path="packages/core/src/memex_core/services/session_briefing.py" lines="443-541" />

The order isn't arbitrary. It preserves the highest-signal sections — procedures and the vault narrative — until last.

**Step 6 — assemble the envelope.**
Back in the hook, the briefing markdown gets concatenated with the per-session bits: a "Per-project vault" header telling the agent which vault to write to, the auto-generated session-note key, and an "Auto-injected metadata" block listing the tags every `memex_add_note` from this session will inherit.
The whole thing gets wrapped in a JSON envelope and emitted: `{"hookSpecificOutput": {"additionalContext": "..."}}`.
Claude Code appends it to the agent's system prompt.
<code-ref path="packages/claude-code-plugin/scripts/on_session_start.sh" lines="155-196" />

By the time the agent reads your first message, the system prompt already contains: every Tier 1b retrieval-routing rule, the Claude-Code-specific harness instructions, a 2,000-token summary of your vault, and the session's write target. No tool calls fired yet.

The Hermes path is similar in spirit but in-process. `BriefingCache` in the Hermes plugin fires a background fetch of the same `/sessions/briefing` endpoint and exposes the result through `format_briefing_block`, which Python-side composes `compose_universal()` + `HERMES_HARNESS` + the briefing into a single string the Hermes agent loop injects into its system prompt. <code-ref path="packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py" lines="98-150" /> Same SSOT objects, same briefing, different envelope.

### Where the data inside the briefing comes from

The six sections of a briefing aren't computed at briefing time. Each one is a projection of state that already exists in the database:

- The **header** stats come from the `VaultSummary.inventory` JSONB column — note counts, entity counts, the 7-day recent-activity window. The vault-summary service computes those as SQL aggregates over `Note`, `MemoryUnit`, and `Entity` on its scheduled tick.
- The **KV facts** and **procedures** come from the `KVEntry` table, filtered to the briefing's namespaces (`global`, `user`, `app:claude-code`, `procedure`, and `project:<id>`). Procedure rows live alongside other KV rows but render in their own section because they encode behavioural rules the agent must follow.
- The **vault overview** narrative is the LLM-synthesised text in `VaultSummary.narrative`, produced by the `SummarizeVaultSignature` DSPy signature. The **themes** below it come from `VaultSummary.themes`, each carrying a name, note count, description, and trend (growing / stable / dormant).
- The **top entities** are `MentalModel` rows, sorted by trend-weighted importance — observations with `trend='new'` count 3.0, `strengthening` 2.0, `weakening` 1.5, `stable` 0.5, and `stale` 0.0. <code-ref path="packages/core/src/memex_core/services/session_briefing.py" lines="34-40" /> A model with three new observations outranks one with five stable observations.
- The **available vaults** section is `VaultService.list_vaults_with_counts()` — the same query that powers `memex vault list`.

The composition is read-only. The briefing service doesn't trigger reflection, doesn't refresh the vault summary, and doesn't write anything to the database. It reads four sources, formats them, and returns markdown.

## Trade-offs: why pre-render, why budget, why answer from briefing

**Why pre-render at all?**
The alternative is on-demand: let the agent call `memex_get_vault_summary` and `memex_kv_list` whenever it needs context.
That's flexible — the agent fetches exactly what it asks for — but the costs add up.
You pay on every cold start. The agent has to *know* to fetch. The prompt prefix can't be cached because the calls happen mid-conversation.

Pre-rendering trades flexibility for latency.
The pre-rendered surface lives in the cacheable prefix; everything you'd have spent on discovery round-trips becomes a cache hit.

**Why a token budget?**
A briefing without a cap will eat the agent's context window.
A 50,000-token briefing in front of a 200,000-token context isn't free — it competes with conversation history, retrieved units, and tool output.

The 2000-token default is calibrated against the U-shaped attention curve that motivates the rest of the agent-surface architecture.
It carries enough room for high-signal content (vault narrative, top models, procedures) without crowding out the recent conversation that sits in the recency-emphasis zone of the prompt.

**Why the overflow ladder instead of hard rejection?**
A briefing that exceeds budget by 200 tokens shouldn't fail closed and emit nothing. It should degrade.

The degradation order encodes a hypothesis about which sections matter most.
Procedures are behavioural rules an agent *must* follow, so they degrade last.
Theme descriptions are nice-to-have, so they go first.

If you find yourself routinely hitting the lower steps — overview dropped, procedures trimmed — that's a signal that the vault's KV state is too big for a 2000-token briefing, not that the briefing service is wrong.

**Why the `answer_from_briefing` rule?**
Without it, agents would re-fetch vault summaries and KV state out of habit, even when the data was rendered above.

The new Tier 2 constraint tells the agent: if you see the section in the briefing, answer from it.
Only call `memex_get_vault_summary` / `memex_kv_list` / `memex_list_vaults` / `memex_survey` when the briefing is silent on what's being asked.
The rule has explicit exceptions for sections the briefing dropped under overflow (no heading present), so the agent never gets stuck silent on a question the briefing should have covered but couldn't.

**Why a CLI bridge instead of an HTTP fetch in the hook?**
The hook is bash. It could `curl` the briefing endpoint directly.

But Hermes and Claude Code need to share the same SSOT objects — `HERMES_HARNESS`, `CLAUDE_CODE_HARNESS`, `compose_universal()` — and the CLI is the only path that exposes those without importing `memex_common` in-process.
`memex agent-surface` and `memex briefing` are the two subcommands that bridge non-Python harnesses to the in-process composition.
<code-ref path="packages/cli/src/memex_cli/agent_surface.py" lines="75-86" />

## Implications: refresh cadence, opt-out, and what to do when it's stale

**The briefing refreshes on the server's cadence, not on session start.**
The vault-summary refresh is scheduled by `VaultSummaryConfig.interval_seconds`, default 3600 seconds (one hour).
<code-ref path="packages/common/src/memex_common/config.py" lines="2050-2058" />

When a session boots, the briefing service reads the *most recent* persisted `VaultSummary` row. It doesn't trigger a refresh.
So if the vault has had heavy ingestion in the last hour, the briefing's narrative will lag.

The nightly consolidation orchestrator force-refreshes the summary as part of the same coordinated pass that runs reflection drain and lint.
By morning the briefing always reflects the prior day's activity.

**Opting out is one environment variable.**
Set `MEMEX_CC_SESSION_BRIEFING=off` (or `0` / `false` / `no` / `disabled`) in your Claude Code plugin environment, and the SessionStart hook skips the briefing fetch entirely.

The static Tier 1b + Tier 2 surface still installs.
The vault binding and session-note instructions still emit.
The agent still gets enough context to write to the right vault — only the dynamic per-vault markdown is suppressed.
<code-ref path="packages/claude-code-plugin/scripts/on_session_start.sh" lines="117-125" />

Use this when you're running the plugin in a context where briefing fetches are slow, the server is intentionally offline, or you're debugging hook behaviour and want a smaller prompt.

**Stale briefings are usually a scheduler problem, not a briefing problem.**
If you've ingested a hundred notes and the briefing still describes your vault from before, the briefing service is faithfully reading whatever `VaultSummary` row exists.
The fix is to force a refresh — `memex vault summary --regenerate` — not to debug the briefing service.

The same chain breaks the other direction too.
If no vault summary has ever been generated for the vault (fresh install, scheduler disabled), the briefing's overview section will be empty and the briefing will be dominated by KV state and mental models.

**Don't confuse the briefing with the agent surface.**
They render in adjacent positions of the system prompt, but they answer different questions.

The agent surface tells the agent *how to use Memex* — which tool routes to which question, what the storage model is, how citations work.
The briefing tells the agent *what's in this specific vault right now*.

The first is universal and cached; the second is per-vault and refreshed.
When you change behaviour for all Memex agents, you change the agent surface.
When you want the agent to know about a specific recent thing, you change the vault — write a note, set a KV procedure — and let the next briefing refresh pick it up.

**A debugging check you can run in five seconds.**
If you suspect the briefing isn't reaching your agent, run `memex briefing --vault <name> --budget 2000` in your shell.
The CLI prints the exact markdown the hook would emit.

Two things to look for:

- Does the output contain a `# Session Briefing` header? If not, the server didn't produce a `VaultSummary` row and you need to run `memex vault summary --regenerate`.
- Does the section you expect to see exist? If it's missing, either the underlying data is absent (no procedures stored, no entities yet) or the overflow ladder dropped it.

**Procedures are the high-leverage section.**
Of everything in the briefing, the `## Procedures` section is the most directly behaviour-shaping.

A KV row under `procedure:answer:session-briefing` becomes a bullet that tells the agent how to handle a class of question.
Because it renders in every session for the vault, the agent reads it on every cold start.

If you want to change how an agent behaves across your team's sessions without touching any code, write a procedure KV row.
The next briefing refresh carries it.

**The budget is not the limit on what Memex knows about your vault.**
The briefing is a *summary*.
The full `VaultSummary` row can carry far more themes, entities, and metadata than will fit in 2000 tokens; the briefing service trims to budget on the way out.

The agent still has access to `memex_get_vault_summary`, `memex_survey`, and the rest of the retrieval surface for questions the briefing doesn't already answer.
The point of the briefing is to handle the cases where the agent would *otherwise* burn tool calls re-discovering common facts, not to be the only window into the vault.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Set up the Claude Code plugin](../how-to/setup-claude-code.md)
- [Reference: CLI commands](../reference/cli-commands.md)
- [Explanation: Reflection and mental models](reflection-and-mental-models.md)
