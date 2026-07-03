# Secure Memex with an API key

The Memex server starts unauthenticated. Anyone who can reach the port can read or write every vault. This guide walks you through turning on API key authentication, scoping each key to a policy and a vault list, and confirming the lock is on.

## Prerequisites

- A running Memex server you can restart.
- A config file you can edit. By default the server reads `.memex.yaml` from the current directory or the path in `MEMEX_CONFIG_PATH`.
- Shell access to generate secrets and run `curl`.

## Procedure

### 1. Generate a key

Run this once per key you want to issue:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The string it prints is the shared secret. Treat it like a password — paste it into config or an env var, never into chat, logs, or commits. The generation recipe is the same one the config field suggests <code-ref path="packages/common/src/memex_common/config.py" lines="1338-1341" />.

### 2. Add the key to the server config

Auth lives under `server.auth` in your YAML <code-ref path="packages/common/src/memex_common/config.py" lines="2427-2430" />. The minimum to switch auth on is `enabled: true` plus one entry in `keys`:

```yaml
server:
  auth:
    enabled: true
    keys:
      - key: "paste-the-token-here"
        policy: admin
        description: "ops laptop"
```

Each key entry must declare a `policy`. The three built-in policies map to permission sets <code-ref path="packages/common/src/memex_common/config.py" lines="1319-1323" />:

| Policy | Reads | Writes | Deletes |
|:------|:-----:|:------:|:-------:|
| `reader` | yes | no | no |
| `writer` | yes | yes | no |
| `admin` | yes | yes | yes |

Pick the narrowest policy that lets the caller do its job. A Firefox extension that only saves pages needs `writer`. A read-only dashboard needs `reader`. Reserve `admin` for the human at the keyboard.

### 3. Scope the key to specific vaults (optional)

Leave `vault_ids` off and the key reaches every vault. Add it and the key sees only the vaults you list <code-ref path="packages/common/src/memex_common/config.py" lines="1343-1346" />:

```yaml
server:
  auth:
    enabled: true
    keys:
      - key: "writer-token"
        policy: writer
        vault_ids: ["work", "personal"]
        read_vault_ids: ["archive"]
        description: "daily capture"
```

`read_vault_ids` widens read access without granting writes — the effective read scope is `vault_ids ∪ read_vault_ids`, and write or delete checks ignore `read_vault_ids` entirely <code-ref path="packages/core/src/memex_core/server/auth.py" lines="232-277" />. Setting `read_vault_ids` without `vault_ids` is rejected at config load <code-ref path="packages/common/src/memex_common/config.py" lines="1363-1372" />.

### 4. Keep secrets out of the YAML file (optional)

Prefix any `key` value with `env:` to resolve it from an environment variable at startup <code-ref path="packages/common/src/memex_common/config.py" lines="1376-1389" />:

```yaml
server:
  auth:
    enabled: true
    keys:
      - key: "env:MEMEX_ADMIN_KEY"
        policy: admin
        description: "primary admin"
```

Then export the secret before launching the server:

```bash
export MEMEX_ADMIN_KEY="paste-the-token-here"
```

If the variable is unset, the server refuses to start with a clear error naming the missing variable.

### 5. Restart the server

Auth config is read at startup <code-ref path="packages/core/src/memex_core/server/auth.py" lines="89-120" />. Stop and start the server. Watch the logs for the confirmation line:

```
API key authentication enabled (1 key(s) configured, 3 exempt path(s)).
```

If you see `API key authentication is disabled.` instead, `enabled` is not `true` in the config the server actually loaded.

### 6. Send the key on every request

Clients pass the key in the `X-API-Key` header <code-ref path="packages/core/src/memex_core/server/auth.py" lines="152" />:

```bash
curl -H "X-API-Key: paste-the-token-here" http://localhost:8000/api/v1/vaults
```

For the CLI and MCP clients, set `MEMEX_API_KEY` in the environment or `api_key` at the top of `.memex.yaml` and both clients will attach the header for you.

## Verification

With the server running, confirm three things:

```bash
# 1. Health probe — always works, auth or no auth.
curl -s http://localhost:8000/api/v1/health

# 2. Protected endpoint without a key — should fail.
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/v1/vaults

# 3. Same endpoint with the right key — should succeed.
curl -s -o /dev/null -w "%{http_code}\n" \
     -H "X-API-Key: paste-the-token-here" \
     http://localhost:8000/api/v1/vaults
```

You want `200`, `401`, `200`. The middleware returns `401` when the header is missing and `403` when the header is present but does not match a configured key <code-ref path="packages/core/src/memex_core/server/auth.py" lines="152-174" />.

## Troubleshooting

**`401 Missing API key.` on every request.** The header is not reaching the server. Print the request with `curl -v` and confirm `X-API-Key:` appears in the outgoing headers. If you are calling through a proxy, check the proxy is not stripping custom headers.

**`403 Invalid API key.` with a key you just set.** The server is comparing against a different config than the one you edited. Verify the path in `MEMEX_CONFIG_PATH` (or the file in the working directory the server was launched from), then restart. Keys are compared byte-for-byte with constant-time comparison <code-ref path="packages/core/src/memex_core/server/auth.py" lines="52-71" /> — a trailing newline or stray space will fail.

**`403 Access denied to vault <id>.`** The key has `vault_ids` set and the request targets a vault outside that list. For read calls, also confirm whether the vault belongs in `read_vault_ids` instead. Write or delete requests against a `read_vault_ids` vault are denied by design <code-ref path="packages/core/src/memex_core/server/auth.py" lines="232-277" />.

**An endpoint answers without a key.** Check `server.auth.exempt_paths`. By default the server bypasses auth on `/api/v1/health`, `/api/v1/ready`, and `/api/v1/metrics` <code-ref path="packages/common/src/memex_common/config.py" lines="1406-1409" />. Path matching is exact-string, not prefix — `/api/v1/healthz` is not exempt. Trim or extend the list to match your operations needs.

**Auth says enabled but no keys are configured.** The startup log shows a warning and every authenticated request returns `403` <code-ref path="packages/core/src/memex_core/server/auth.py" lines="107-111" />. Add at least one entry under `keys` and restart.

**Old `api_keys` field rejected at startup.** The flat `api_keys` shape was removed; each key now carries its own policy. The error message names the new layout <code-ref path="packages/common/src/memex_common/config.py" lines="1421-1432" />.

## See also

- [Tutorial: Getting started](../../tutorials/getting-started.md)
- [How-to: Configure Memex](./default-model.md)
- [Reference: server configuration](../../reference/configuration-options.md)
- [Explanation: architecture overview](../../explanation/how-memex-works/high-level-architecture.md)
