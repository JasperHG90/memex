# Integrate Memex with Claude Code

This tutorial walks you through wiring Claude Code into your Memex vault. By the end you will have installed the plugin, bound your project to a vault, saved a memory with `/remember`, found it again with `/recall`, told the agent that the suggested fix worked, and learned how to turn off transcript capture for a long session.

You learn the integration by using it. Each step ends with an observable signal you can check before moving on.

## Prerequisites

You should already have:

- Claude Code installed and able to start a session.
- The Memex CLI installed as a `uv` tool:
  ```bash
  uv tool install "memex-cli[mcp,server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
  ```
- A Memex config and at least one vault:
  ```bash
  memex config init
  memex vault create my-vault --description "My notes"
  ```
- The Memex server running:
  ```bash
  memex server start -d
  ```
- A project directory on disk. A git repository with an `origin` remote works best, but any folder is fine.

If any of those is missing, set it up first. The plugin warns at session start when the server is not reachable, but the rest of the tutorial assumes the prerequisites are in place.

## Step 1: Install the plugin

Run the two install commands from your terminal:

```bash
claude plugin marketplace add JasperHG90/memex
claude plugin install memex@memex
```

The first command tells Claude Code where to find the Memex marketplace. The second installs the plugin itself. You only need to add the marketplace once per machine; from then on, every install or update reads from it.

If you prefer to install from inside Claude Code, the same two commands work as slash commands:

```
/plugin marketplace add JasperHG90/memex
/plugin install memex@memex
```

**Check.** Run `claude plugin list`. You should see `memex@memex` in the output.

## Step 2: Restart Claude Code and verify the briefing

Close the Claude Code session you were running (if any) and start a fresh one in your project directory:

```bash
cd ~/code/my-project
claude
```

The plugin runs a `SessionStart` hook the moment the session boots. The hook does four things at once: it installs a rule file at `.claude/rules/memex-agent-surface.md`, generates a session note key, resolves which vault to use, and fetches a token-budgeted briefing from your Memex server. The briefing then rides into the system prompt as additional context.

You will see a status line near the top of the session that looks like one of these:

```
Memex connected (vault: my-vault)
Memex connected · No vault set — tell me which vault to use for this project
Memex connected · Agent surface installed at .claude/rules/memex-agent-surface.md — restart Claude Code to load it
```

The last variant appears on the **first install in this project**. The rule file is written during `SessionStart`, but the system prompt for the current session was already assembled before the hook ran. Restart Claude Code once more and the file loads.

**Check.** The status line says "Memex connected". The file `.claude/rules/memex-agent-surface.md` exists in your project root. The session also includes a briefing section with vault summary, top entities, and KV facts — scroll up in the transcript if you want to read it.

## Step 3: Bind a vault to this project

Ask Claude to point this project at a specific vault. Type a message like:

> Set this project's vault to `my-vault`.

The agent has the KV namespace rules from the briefing, so it should derive the project ID from your git remote (or directory name) and write the binding with `memex_kv_put`. The call looks like this under the hood:

```
memex_kv_put(
  key="app:claude-code:project:github.com/you/my-project:vault",
  value="my-vault"
)
```

The `app:claude-code:project:<id>:vault` namespace is the first rung in the plugin's vault resolution chain. It wins over user-level defaults, env-var overrides, and the server-side default — so this one write tells every future session "use `my-vault` when you are working in this repo".

**Check.** Ask Claude:

> Read the KV key for this project's vault.

The agent should call `memex_kv_get` and return `my-vault`. You can also verify from the shell:

```bash
memex kv get "app:claude-code:project:github.com/you/my-project:vault" --value-only
```

Replace the project ID with whatever the briefing reported when the session started. The binding takes effect on the next session.

## Step 4: Save a memory with /remember

Type the slash command with a piece of content you want to keep:

```
/remember The deploy pipeline switched from GitHub Actions to CircleCI on 2026-04-12 because artifact-size limits broke our nightly builds.
```

The skill decides where the content belongs. A factual statement like the one above is a note, so the agent calls `memex_add_note` with a short title, the body text, and `author: "claude-code"`. The plugin's `PreToolUse` hook then adds ambient tags automatically — your session note key, the project ID, the git branch and SHA, the model name — so you do not have to repeat them on every save. The hook also defaults `background: true`, which means the server queues extraction in the background and the agent does not wait for it.

If the content had been a preference, convention, or setting ("I prefer pytest over unittest"), the skill would have routed the same call to `memex_kv_put` instead, under a key like `user:testing:framework`. The shape of the content picks the storage layer — `/remember` does not always create a note.

Try the same trick with a preference to see the routing in action:

```
/remember For this project, we use Python 3.12 as the minimum supported version.
```

The phrase "for this project" pins the scope to project-level, so the agent should call `memex_kv_put` with a `project:<id>:lang:python:min` key. No note gets created.

**Check.** Run two confirmations:

```bash
memex note search "CircleCI deploy" --vault my-vault
memex kv list --namespace "project:" | grep python
```

The first command lists the note you saved. The second shows the KV entry for the Python version. You now have one factual note and one project-scoped convention — captured from two superficially identical commands, routed differently by content shape.

## Step 5: Find it again with /recall

Search for the same memory with the recall command:

```
/recall when did we switch our deploy pipeline?
```

The skill runs `memex_memory_search` and `memex_note_search` in parallel against your vault and summarises the hits. A good recall response cites the source — you should see something like:

> The team migrated from GitHub Actions to CircleCI on 2026-04-12 because of artifact-size limits in the nightly build [note: deploy-pipeline-migration].

The query format matters. The skill is trained to write natural-language queries that preserve the proper nouns and qualifiers from your question — not keyword lists. "when did we switch our deploy pipeline" beats "deploy switch date" because the first preserves the topic ("deploy pipeline") and the question shape, which feed the embedding model.

If the first pass returns nothing, the skill retries with `expand_query=true`, which asks the LLM to broaden the phrasing. If that also fails, the skill says so — it will not guess.

You can run `/recall` with no arguments at all. A `UserPromptExpansion` hook reads the last three turns of the transcript and composes a query for you. The skill prints a one-line summary of the composed query before searching, so you can tell whether the inferred question matches your intent.

**Check.** The agent returns the note you saved in step 4, with the title (or ID) cited inline. If you also ran the Python-version capture, ask:

```
/recall what is our minimum Python version on this project?
```

The skill should now also check the `project:` KV namespace and surface the value you wrote.

## Step 6: Record an outcome

Outcomes are the way Memex learns which memories actually helped. When Claude suggests a fix and it works, you stamp the underlying memory unit as helpful so it ranks higher next time.

Start a fresh exchange where the agent recommends something concrete. For example:

> The nightly build is failing again. Have we seen this before?

The agent calls `/recall`-style tools, finds the CircleCI migration note, and proposes that you check the artifact-size limit. Apply the fix yourself. Then come back and say:

> That worked — record it as a success.

The agent should now call `memex_record_outcome` on the unit it just surfaced. The call shape looks like:

```
memex_record_outcome(units=[
  {unit_id: "<id from the search>", verb: "helpful", reason: "artifact-size cap was the cause again"}
])
```

`memex_record_outcome` updates the unit's Memory Worth score — an append-only counter that biases future ranking. The agent does **not** create a new note saying "fix confirmed"; the outcome is a counter increment on the existing unit. Writing a new note to mark a success is the wrong path and the plugin will not detect it as an outcome.

If you ever say "stop suggesting that" instead, the agent flips the paired write: it stamps `verb: "not_helpful"` and **also** calls `memex_memory_deprioritize(unit_id, reason)` on the same unit. The first records the negative signal; the second drops the unit out of normal retrieval. Deprioritization is reversible via `memex_memory_restore`, so a wrong call here is not permanent.

**Check.** The agent reports that an outcome was recorded against a specific unit. Run a follow-up `/recall` on the same topic — the helpful unit should appear at or near the top of the results.

## Step 7: Disable transcript capture for a long session

By default, the plugin captures every Claude Code session to a Memex note keyed by `session:<timestamp>`. Two hooks share the work: `PreCompact` appends transcript-since-last-compact when the context window is about to compress, and `SessionEnd` appends the remainder when the session closes. Both write to the same note, so you get one document per session rather than many fragments.

Long sessions produce big notes, and big notes pay the extraction cost when Memex processes them. If you know up front that today's session will be long — a multi-hour refactor, a debugging marathon — you can turn off transcript capture for that session.

Set the environment variable in your shell profile or as a one-off:

```bash
export MEMEX_CC_TRANSCRIPT_CAPTURE=off
claude
```

Or paste it into `~/.claude/settings.json` to disable across all sessions:

```jsonc
{
  "env": {
    "MEMEX_CC_TRANSCRIPT_CAPTURE": "off"
  }
}
```

The parser accepts `off`, `0`, `false`, `no`, or `disabled` as the off switch. Anything else — including unsetting the variable or setting it to `on` — leaves transcript capture enabled. Unset the variable to switch it back on; that is the cleanest path.

Transcript capture is a safety net, not a substitute for `/remember`. With capture off, you still get `/remember` for curated saves and the session briefing on start; only the automatic background save is suppressed.

A sibling toggle, `MEMEX_CC_SESSION_BRIEFING=off`, suppresses the briefing fetch on session start instead. Turn it off if you want a faster start and do not need the vault snapshot in every session.

**Check.** Transcript capture has no status-line indicator — the plugin suppresses it silently — so verify by its absence. Start a new Claude Code session after setting `MEMEX_CC_TRANSCRIPT_CAPTURE=off`, run a few exchanges, then exit. Search your vault for the session note:

```bash
memex note search "Session transcript" --vault my-vault
```

With capture on, each session writes one note titled `Session transcript: session:<timestamp>`. With capture off, no such note appears for this run.

(If you instead set `MEMEX_CC_SESSION_BRIEFING=off`, the session *does* announce it — the start-up status line gains a `· Briefing disabled (MEMEX_CC_SESSION_BRIEFING)` segment.)

## If a step does not show the expected signal

The tutorial is designed so each step ends with something you can see. When the signal is missing, work back through the most common causes before retrying:

- **The status line says "Memex server is not reachable".** Run `memex server start -d` and start a fresh Claude Code session. The briefing is fetched once at session start, so a server you started mid-session will not retroactively appear in the current context.
- **The status line says "No vault set" even after step 3.** The plugin caches the resolved vault for the session. The KV write you made took effect, but you need to restart Claude Code to pick it up. Step 2's restart pattern applies here too.
- **`/remember` runs but no note appears in `memex note search`.** Check that the agent saved into the vault you bound the project to. Run `memex note list --vault my-vault` directly. If the note is in a different vault, check the briefing's resolved-vault line — your binding may have written under a project ID different from what you expected.
- **`/recall` returns nothing for content you saved seconds ago.** Extraction runs in the background and may not have finished. Wait a few seconds and retry, or pass `background: false` on the `memex_add_note` call to make ingestion synchronous.
- **The outcome step does not stamp anything.** The agent needs a specific unit ID to target. If your "that worked" message is vague — no concrete referent in the conversation — the skill asks which suggestion you mean rather than guessing. Be specific: "the artifact-size cap fix worked".

If a step still fails, run `memex report-bug` to open a pre-filled GitHub issue with your system info attached.

## What you built

You now have a working Claude Code integration that:

- Loads the Memex agent surface and a per-vault briefing on every session.
- Routes your captures to the right storage layer — KV for preferences, notes for facts.
- Surfaces relevant memories via `/recall` with proper citations.
- Learns from outcomes so helpful memories rank higher over time.
- Lets you opt out of expensive behaviours when the session does not warrant them.

Everything you store lives in your own Memex vault on your own server. The plugin is a thin client over the same MCP tools any other Memex-aware agent uses, so the data is portable.

`/remember` and `/recall` are the two you will reach for most, but the plugin ships more: `/handoff` and `/continue` carry context between sessions, `/case` captures a reusable how-to procedure and `/learnings` distils the session's durable takeaways, `/ingest` pulls a file or web page into memory, `/lint` triages memory-hygiene findings, and `/correct` flags a memory that surfaced wrongly. Run `/help` inside Claude Code to see the full set.

## Next steps

- [Tutorial: Get started with Memex](getting-started.md) — for the wider tour of vaults, notes, and search.
- [How-to: Set up Claude Code](../how-to/integrations/claude-code.md) — the recipe form of this tutorial, for when you need a quick reference rather than a walkthrough.
- [Reference: MCP tools](../reference/mcp-tools.md) — full parameter shapes for `memex_kv_put`, `memex_record_outcome`, and the rest.
- [Explanation: Hindsight framework](../explanation/session-briefings.md) — why Memex separates notes, units, and observations, and how outcomes feed the ranking loop.
