# Submit cases and derive procedures

You just got an unfamiliar deploy unstuck, and you want Memex to remember the fix as a reusable recipe — not as scattered facts, but as a procedure it can recall the next time the same trigger fires. This guide takes you from one worked episode to a published procedure: submit the case, derive the procedure, review the draft, and publish it.

## Prerequisites

- A running Memex server (`memex server start`) reachable by the CLI.
- The `memex` CLI installed and pointed at that server.
- For the activation step on a server with auth disabled: shell access to set an environment variable on the server process (see Troubleshooting).

## Procedure

### 1. Submit the case

A case is a worked episode in five parts: a trigger, the situation going in, the ordered actions you took, and the outcome with its lesson. Submit it with flags:

```bash
memex case submit \
  --title "Unstick a hung Nomad deploy" \
  --trigger "Nomad deploy hangs on stuck allocations" \
  --situation "Deploy stalled; allocations stuck in 'pending'." \
  --action "Drain the Nomad job" \
  --action "Wait for allocations to clear" \
  --action "Re-submit the job" \
  --outcome success \
  --lesson "Always drain before re-submitting; don't force-restart."
```

`--title`, `--trigger`, and `--outcome` are required; `--outcome` must be `success`, `failure`, or `mixed`. Pass `--action`/`-a` once per ordered step. <code-ref path="packages/cli/src/memex_cli/procedural.py" lines="843-856" />

The case is filed as a note into a hidden system vault — there is no `--vault` flag; the server owns the placement. <code-ref path="packages/cli/src/memex_cli/procedural.py" lines="813-823" />

On success the CLI prints the filed note id and the assignment outcome — for a brand-new recipe, `seeded draft procedure <id>`. <code-ref path="packages/cli/src/memex_cli/procedural.py" lines="906-917" />

### 1b. (Alternative) Submit from a markdown file

If you already wrote the episode up as markdown, pass it with `--file`/`-f` instead of the flags. The file uses the same five-section template (`## Trigger` / `## Situation` / `## Actions` / `## Outcome / Lesson`), optionally with YAML frontmatter for `title`, `outcome`, and `tags`:

```markdown
---
title: Unstick a hung Nomad deploy
outcome: success
tags: [deploy, nomad]
---

# Unstick a hung Nomad deploy

## Trigger
Nomad deploy hangs on stuck allocations

## Situation
Deploy stalled; allocations stuck in 'pending'.

## Actions
1. Drain the Nomad job
2. Wait for allocations to clear
3. Re-submit the job

## Outcome / Lesson
success. **Lesson:** Always drain before re-submitting.
```

```bash
memex case submit --file ./nomad-deploy-case.md
```

Any flag you also pass overrides the parsed value. <code-ref path="packages/cli/src/memex_cli/procedural.py" lines="830-841" />

### 2. Attach to a known procedure (optional)

If you know which procedure this episode is an instance of, name it with `--case-of <entry-uuid>`. That skips the assignment judge and attaches the case directly, bumping the procedure's outcome counters: <code-ref path="packages/cli/src/memex_cli/procedural.py" lines="794-800" />

```bash
memex case submit --title "..." --trigger "..." --outcome success \
  --case-of 7b2e1d4a-0000-0000-0000-000000000000
```

Without `--case-of`, the server judges the assignment for you. A contested judgment lands in the lint queue rather than blocking — you resolve it in step 4.

### 3. Derive the procedure

Assignment creates the procedure anchor but leaves its body empty. Derivation distils the steps from the attached cases. A background worker normally does this on a cadence; to materialise the draft now, drain the queue by hand:

```bash
memex procedure derive
```

This drains up to `--limit` pending tasks (default 10) and prints how many entries were derived. <code-ref path="packages/cli/src/memex_cli/procedural.py" lines="604-633" /> One case is enough — distillation does not wait for repeats. <code-ref path="packages/core/src/memex_core/memory/procedural_distillation.py" lines="42" />

### 4. Review and activate the draft

A derived procedure is written as a `draft` and stays invisible to search and briefing until a human confirms it. <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="2077-2081" /> Confirmation runs through the lint queue, where the new draft appears as a `governance` finding with the `activate_procedural_entry` action pre-selected. <code-ref path="packages/core/src/memex_core/services/case_service.py" lines="515-569" />

Open the review cockpit:

```bash
memex lint review
```

Find the "new procedure anchor, ready to activate" finding, accept it, and pick the `activate_procedural_entry` action. The entry flips `draft → published` and becomes retrievable. The step is reversible — undoing the activation re-drafts the entry. <code-ref path="packages/core/src/memex_core/services/proposal_actions/activate_procedural_entry.py" lines="56-124" />

## Verification

Confirm the entry is published and searchable:

```bash
# Should now list the entry with status=published
memex procedure list --status published

# Should return the entry by its trigger
memex procedure search "deploy is stuck on Nomad"
```

A published entry appears in both. If `procedure search` returns nothing but `procedure list --status draft` shows the entry, activation has not happened yet — go back to step 4. <code-ref path="packages/cli/src/memex_cli/procedural.py" lines="410-442" />

## Troubleshooting

**Activation returns 403 on a server with auth disabled.** The lint apply/reverse routes refuse destructive mutations when `server.auth.enabled` is false, because no human principal is on the request. Opt in by starting the server with the override set: <code-ref path="packages/core/src/memex_core/server/lint.py" lines="119-140" />

```bash
MEMEX_LINT_ALLOW_UNATTENDED_APPLY=1 memex server start
```

Accepted values are `1`, `true`, or `yes`. This is the common local-dev gotcha — a freshly started dev server has auth off, so activation fails until you set this.

**`procedure derive` reports zero entries derived.** Either the queue was already drained, or the anchor has no attached cases yet. Re-run `memex case submit` and confirm the printed assignment mode was `auto-assigned` or `seeded draft procedure` (not `escalated`). An escalated case is waiting in the lint queue for you to assign it via `memex lint review`.

**The case came back as "Already filed".** You submitted byte-identical content; ingest is content-idempotent and skipped the duplicate. Change the content (or accept that the case already exists). <code-ref path="packages/cli/src/memex_cli/procedural.py" lines="899-905" />

## See also

- [Explanation: the procedural memory plane](../explanation/how-memex-works/procedural-memory.md)
