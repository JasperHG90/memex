# Create a system vault

System vaults are addressable, fully-extracted vaults that stay silent on the synthesis-and-discovery surfaces (search, list, briefing, reflection, vault-summary) by default. Use this guide when you need a vault that *receives* data on behalf of another component — an integration buffer, a session scratch space, an external ingest pipeline — and you don't want that data to pollute normal user searches.

The [explanation page](../explanation/system-vs-content-vaults.md) covers the *why*; this page covers the *how*.

## Prerequisites

- A running Memex server.
- The `memex` CLI on your PATH.
- Auth role that allows `vault create` (default admin or write scope).

## When to make a vault system vs content

The default is **content**; you only need `kind=system` when the vault is *infrastructure* rather than *user-facing content*. The current canonical example is `inbox`: a buffer that the triage-inbox skill reads from and routes into other vaults. The same shape fits a session-vault for a sub-agent, a procedural case-vault, a third-party integration buffer, or a test sink.

The rule of thumb:

- The vault's notes are *things you read about* → content.
- The vault's notes are *things another component consumes* → system.

If you're unsure, leave the vault as content. Flipping `kind` later requires a fresh vault — see [the immutability note](#immutability) below.

## Step 1 — Create the vault

```bash
memex vault create my-integration-buffer --kind system
```

Because `kind=system` is permanent, the CLI prompts before persisting: <code-ref path="packages/cli/src/memex_cli/vaults.py" lines="191-198" />

```
Vault kind 'system' is permanent and cannot be changed later. Continue? [y/n]:
```

Type `y` to proceed, `n` (or anything else) to abort. The CLI prints the new vault's UUID on success.

To skip the prompt in scripts, pass `--force` (or `-f`):

```bash
memex vault create my-integration-buffer --kind system --force
```

If you forget `--force` in a non-interactive shell, the command aborts with exit code 1.

## Step 2 — (Optional) Override the synthesis policy

By default, a system vault skips both reflection (`reflect: false`) and vault-summary regeneration (`summarize: false`). If your use case wants either of those — e.g., a system vault that holds durable evidence you'd like mental models built from — flip the flag at creation time:

```bash
memex vault create my-case-vault \
  --kind system \
  --reflect \
  --summarize
```

Use `--no-reflect` / `--no-summarize` to be explicit about turning them off (the default, but useful in scripts that derive the flags from configuration).

The same flags work on a content vault — `--no-summarize` on a personal scratchpad, for example, is a valid use case for a content vault that doesn't want a briefing entry.

## Step 3 — Use the vault

A system vault is a normal vault from the caller's perspective. You address it the same way you address a content vault — by name or UUID, on any tool:

```bash
# Write into it (content is the positional argument)
memex note add '{"event": "...", "ts": "..."}' \
  --vault my-integration-buffer \
  --title "Webhook payload 2026-06-08T12:00:00Z"

# Read from it directly
memex note search "webhook" --vault my-integration-buffer
```

From the CLI, you read a system vault by naming it with `--vault` — there is no CLI flag that folds every system vault into one search. Over HTTP and MCP, the `include_system_vaults=true` parameter does that: an **unconditional union** that adds every system vault to the resolved scope, regardless of whether you also passed a vault. <code-ref path="packages/core/src/memex_core/server/notes.py" lines="183" /> To read one specific system vault, name it (`--vault inbox`) and leave the union off. If you find yourself reaching for the union often, consider whether the vault is really a system vault.

## Step 4 — Verify visibility is what you expect

Three quick checks:

```bash
# 1. List without the flag — your system vault should NOT appear
memex vault list

# 2. List with the flag — it SHOULD appear
memex vault list --include-system

# 3. The Kind column reads 'system'
memex vault list --include-system
```

The `Kind` column was added in V11; older lists won't have it. Update the CLI (`uv sync`) if you see no column.

## Immutability

`kind` is set at creation and cannot be changed afterwards. There is no `memex vault set-kind` and there never will be — the harm comes from changing kind *after* state has accumulated (mental models get archived, vault summaries get cleared, the entity ranking shifts). If you need to flip a vault's kind:

1. Create a new vault with the right `kind`.
2. `memex note migrate` every note from the old vault to the new one.
3. Delete the old vault.

The migration moves notes, memory units, entity links, and reflection records. It does not copy the *synthesis* rows (mental models, vault summaries) — those are rebuilt by the scheduler on the new vault if its `policy` allows.

## Programmatic creation (HTTP)

The HTTP `POST /api/v1/vaults` endpoint accepts `kind` and `policy` in the request body: <code-ref path="packages/core/src/memex_core/server/vaults.py" lines="212-213" />

```bash
curl -X POST $MEMEX_SERVER_URL/api/v1/vaults \
  -H "Authorization: Bearer $MEMEX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-integration-buffer",
    "kind": "system",
    "policy": {"reflect": false, "summarize": false}
  }'
```

The endpoint rejects any `kind` other than `"content"` or `"system"`, and any unknown `policy` key (the typed model uses `extra='forbid'`), with HTTP 422. <code-ref path="packages/common/src/memex_common/schemas.py" lines="437-467" /> There is no MCP tool for vault creation — use the CLI or this endpoint.

## Troubleshooting

### The vault doesn't appear in `memex vault list`

That's the expected default behavior. Re-run with `--include-system`.

### Reflection/summary *does* run on my system vault

Check the vault's `policy`. There is no per-vault `show` command; list the vaults as JSON (with `--include-system`, so the system vault appears) and read the `policy` field on the entry:

```bash
memex vault list --include-system --json
```

If `policy.reflect` or `policy.summarize` is `true` when you expected `false`, you set the flag at creation — the `kind` doesn't override an explicit `policy` setting.

### I want to flip a vault's kind

You can't. [Create a new vault and migrate](#immutability).

## See also

- [Explanation: System vaults vs content vaults](../explanation/system-vs-content-vaults.md) — the contract in depth.
- [How-to: Organise work with vaults](vaults.md) — content vault creation and migration.
