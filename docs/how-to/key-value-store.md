# Use the key-value store

Memex's key-value store is where preferences, conventions, and procedures live — things you want every future session to remember without re-reading a note. Vaults isolate your notes; the KV store does not. A preference you save once is available across every project.

This guide covers picking a namespace, writing a value, reading it back, and listing what's stored. The `procedure:` namespace has its own short section at the end because it behaves a little differently.

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
| A learned procedure | `procedure:<verb>:<tag>` | `procedure:deploy:staging` |

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

Values are stored as strings. For structured values, encode JSON yourself. The latest write is the active value; for the `procedure:` namespace, older versions stay in a capped history (see the section below). For other namespaces, the write is a plain upsert.

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

## The `procedure:` namespace

Procedures are short steps you've learned and want to reuse — *"to deploy to staging, run X then Y"*, *"to debug the auth service, tail logs at Z"*. They live under `procedure:` with two extra rules:

- **Last writer wins.** Overwriting a procedure is the normal update path; Memex keeps the prior versions in capped history.
- **Naming convention.** The shape `procedure:<verb>:<context-tag>` keeps procedures findable when you search later. `procedure:deploy:staging` and `procedure:debug:auth_service` are clearer than `procedure:1`.

Set one the same way as any KV entry. The key must match `procedure:<verb>:<context-tag>` where each segment is lowercase letters, digits, hyphens, or underscores:

```bash
memex kv put "procedure:deploy:staging" "1) git push origin staging  2) await CI green  3) flip the flag"
```

When you search procedures, prefer the semantic search — you remember what you wanted to *do*, not the exact key you chose:

```bash
memex kv search "how do I deploy"
# → procedure:deploy:staging = 1) git push origin staging...  (score 0.94)
```

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

**Two writes to the same key produced unexpected results.** The store is last-writer-wins. If two scripts race to write the same key, only the later write is active (the earlier write stays in history). For preferences that need a combined value, write to two different keys and combine them on read.

**I don't remember whether I used `user:` or `project:`.** Run `memex kv search "<rough description>"` — the semantic search will find the entry across all namespaces.

## See also

- [Tutorial: getting started](../tutorial/getting-started.md)
- [How-to: organise with vaults](vaults.md)
- [Reference: MCP tools](../reference/mcp-tools.md)
- [Explanation: memory types](../explanation/how-memex-works/memory-types.md)
