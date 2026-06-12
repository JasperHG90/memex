---
name: extract-case
description: "Turn a note, a markdown file, or a URL into a Memex case — but only when the content actually contains a reusable how-to / worked episode. Gated: if there's no case in it, files nothing."
argument-hint: "[note-key | note-id | path/to/file.md | https://url]"
---

# /extract-case — Convert content into a case (gated)

Cases feed Memex's procedural plane: the system derives a reusable procedure from each worked episode you file. This skill reads a piece of content, decides whether it holds such an episode, and files one case if it does. A single case now seeds a procedure, so a junk case becomes a junk procedure — when the content is not clearly a how-to, file nothing.

## 1. Resolve the input

`$ARGUMENTS` tells you what to read. Read the actual content first — never judge from the title or URL alone.

- **A Memex note** (a `note_key` like `session:2026-…`, or a UUID): `memex_find_note` / `memex_read_note` (use `memex_get_page_indices` + `memex_get_nodes` if it's over ~500 tokens).
- **A local markdown/text file** (a path): read it with the `Read` tool.
- **A URL**: fetch it with `WebFetch`.
- **Empty `$ARGUMENTS`**: ask which note/file/URL to use, or offer the current session note.

## 2. Gate — is there a case in this content?

A case is a **reusable worked episode**: a concrete task someone performed, the steps they took, and how it turned out — content you'd want back next time you hit the same situation. Judge the substance, not whether the word "deploy" or "procedure" appears.

File a case **only when the content gives you all three**:
1. a **trigger / situation** — the problem faced or task undertaken,
2. **actions** — the concrete steps actually taken,
3. an **outcome** — it worked, failed, or was mixed.

If any of the three is missing — or the content is a standalone fact, a decision, a reference, an opinion, or a not-yet-done plan — there is **no case**. Stop, say so in one line, and (if it's a durable fact worth keeping) suggest `memex_add_note` instead. Filing nothing is the correct result here, not a failure.

<examples>
<example>IS a case → file it. "Staging deploys kept timing out. The Nomad spec had the health-check on the wrong port; I set it to 8080 and redeployed — green." (trigger + actions + outcome all present)</example>
<example>IS a case → file it. A how-to doc: "To rotate the API key: issue a new key, update the CI secret, roll the old key after CI goes green, verify." (a repeatable procedure with ordered steps)</example>
<example>NOT a case → file nothing. "We standardised on Postgres 16 in production." (a fact — suggest memex_add_note)</example>
<example>NOT a case → file nothing. "TODO: investigate why CI is slow." (no actions, no outcome — nothing happened yet)</example>
<example>NOT a case → file nothing. A blog post arguing for trunk-based development. (an opinion/essay, not a performed episode)</example>
</examples>

For long content (a fetched page, a big note), first pull the 1–3 passages that look like a performed task into `<evidence>` notes, then apply the three-part test to that evidence. This keeps you grounded in what the content actually says rather than what its topic suggests.

## 3. Compose the case (only if the gate passed)

Build the fields from the content — ground every step in what the source actually says; do not invent steps the content doesn't support.

- **title**: the task in a few words.
- **trigger**: the when-to-use / the symptom that started it.
- **situation**: the context (one or two sentences).
- **actions**: the ordered steps, service-agnostic where possible.
- **outcome**: `"success"` | `"failure"` | `"mixed"`.
- **lesson**: the one takeaway worth carrying forward.

If the content genuinely lacks a clear outcome but clearly describes a repeatable how-to (e.g. a runbook), use `"success"` and note the absence in `lesson`. If you have to invent more than the outcome, the gate was too generous — reconsider step 2.

## 4. Link to an existing procedure (optional but preferred)

Probe for a procedure already derived on this anchor and link the case to it so it lands in the right cluster:

```text
# (scope, verb, context) — e.g. verb="deploy", context="payments"
memex_procedural_get_by_identity(kind="procedure", scope="global", verb="<verb>", context="<context>")
# → if it returns an entry, pass case_of=<that id> in step 5
```

There is no procedure-write tool — you never author the procedure; `case_of` only links your case to one the system already derived.

## 5. File the case

```text
memex_case_submit(
  title=..., trigger=..., situation=..., actions=[...],
  outcome="success|failure|mixed", lesson=...,
  case_of=<id-if-found>,
)
```

The plugin auto-injects ambient tags (git / session / project / model). The system derives or updates the procedure from this case in the background.

## 6. Report

State what happened in one or two lines: the case you filed (title + outcome, and whether it linked to an existing procedure), **or** that the content held no reusable how-to so nothing was filed (and what you'd suggest instead).
