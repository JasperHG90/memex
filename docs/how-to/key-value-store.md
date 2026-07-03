# Use the key-value store

Memex's key-value store is where preferences, conventions, and settings live — things you want every future session to remember without re-reading a note. Vaults isolate your notes; the KV store does not. A preference you save once is available across every project.

This guide covers picking a namespace, writing a value, reading it back, and listing what's stored. The KV store holds one static binding per key — a preference, a setting, a convention. It does not hold how-to procedures: those live on the procedural plane, a separate store with its own tools. See [Submit cases and derive procedures](submit-cases.md) when you want to save a reusable workflow.

## Prerequisites

- A running Memex server. Check with `memex server status`.
- The `memex` CLI on your PATH, or an MCP-enabled client (Claude Code, Hermes) connected to the server.

## Step 1 — Pick a namespace

KV keys are namespaced. The prefix tells Memex (and your future self) what scope the value belongs to. Pick one before you write.

| What the value is about | Namespace | Example key |
|---|---|---|
| You, across all your work | `user:` | `user:editor` |
| One project or repo | `project:<id>:` | `project:github.com/acme/api:lang:python` |
| Every project you touch (a company-wide standard) | `global:` | `global:lang:python:min_version` |
| One app you use | `app:<app-id>:` | `app:claude-code:theme` |

The four prefixes above (`global:`, `user:`, `project:`, `app:`) are the only namespaces a KV key may start with. A key that begins with anything else is rejected at write time. <code-ref path="packages/common/src/memex_common/kv_utils.py" lines="14" /> <code-ref path="packages/core/src/memex_core/services/kv.py" lines="58-67" />

The most common mistake is reaching for `user:` for everything. If the value is tied to a specific project or app, use `project:` or `app:` — `user:` is for things that follow *you*, not your work.

Project IDs are the normalised git remote URL (e.g. `github.com/acme/api`) or, for non-git work, the project's directory path. The Claude Code plugin derives this on session start and uses it to pick the right vault for the project; with the CLI, you pass the project ID yourself.

## Step 2 — Put a value

From the CLI, both key and value are positional — key first, value second:

```bash
memex kv put "user:editor" "neovim"
memex kv put "project:github.com/acme/api:lang:python" "3.10"
memex kv put "global:lang:python:min_version" "3.12"
```

From an MCP client (Claude Code, Hermes, Claude Desktop), the tool takes named arguments:

```
memex_kv_put(key="user:editor", value="neovim")
```

Values are stored as strings. For structured values, encode JSON yourself. Every write is a plain upsert: the latest write to a key is the active value, and it overwrites whatever was there before. KV keeps no version history. <code-ref path="packages/core/src/memex_core/services/kv.py" lines="83-124" />

Attach a TTL — in seconds — when the value should expire on its own:

```bash
memex kv put "app:claude-code:current_branch" "release/v1.0" --ttl 86400  # 24h
```

## Step 3 — Read a value back

Exact-key lookup:

```bash
memex kv get "user:editor"
# → neovim
```

If you don't remember the exact key, search by query. KV entries carry an embedding, so semantic matches surface even when the wording differs:

```bash
memex kv search "what editor do I use"
# → user:editor = neovim  (score 0.91)
```

## Step 4 — List keys in a namespace

To see every key in one namespace, use `--namespace/-n`:

```bash
memex kv list -n "project"
```

To narrow further with a glob pattern, use `--pattern/-p`:

```bash
memex kv list -p "project:github.com/acme/api:*"
# → project:github.com/acme/api:lang:python = 3.10
# → project:github.com/acme/api:style:indent = 7
# → project:github.com/acme/api:test_runner = pytest
```

`--namespace` is repeatable: `-n user -n global` lists both. `--pattern` accepts standard `*` globs.

## What does not belong in KV

KV is for one static binding per key. A multi-step workflow you want to recall and reuse — *"to deploy to staging, run X then Y"*, *"the release steps"* — is not a KV value. It belongs on the procedural plane, which derives reusable procedures from the cases you submit. Recall one with `memex procedure search`; record one with `memex case submit`. See [Submit cases and derive procedures](submit-cases.md).

There is no `procedure:` KV namespace. A key containing the word `procedure` is just a plain key with no special handling.

## Verification

After setting a value, read it back:

```bash
memex kv put "user:test_key" "hello"
memex kv get "user:test_key"
# → user:test_key = hello
```

If the read returns what you wrote, the store is wired up.

## Troubleshooting

**"Key not found" but I just wrote it.** Check the namespace prefix. `user:editor` and `editor` are different keys; Memex does not silently add a namespace for you.

**A value I wrote yesterday is gone.** Look for a TTL on the original write — `memex kv get <key>` prints an `Expires:` line if one was set. If the value had a TTL, it has expired. Re-write without `--ttl` for a persistent entry.

**A project value is showing up under the wrong project.** The `project:` prefix needs the project identifier you intended. The Claude Code plugin derives it on session start from your git remote (or directory path for non-git projects); the CLI does not — pass the project ID yourself.

**Two writes to the same key produced unexpected results.** The store is last-writer-wins. If two scripts race to write the same key, only the later write survives — the earlier value is overwritten, not kept. For preferences that need a combined value, write to two different keys and combine them on read.

**I don't remember whether I used `user:` or `project:`.** Run `memex kv search "<rough description>"` — the semantic search will find the entry across all namespaces.

## See also

- [Tutorial: getting started](../tutorial/getting-started.md)
- [How-to: organise with vaults](vaults.md)
- [Reference: MCP tools](../reference/mcp-tools.md)
- [Explanation: memory types](../explanation/how-memex-works/memory-types.md)
