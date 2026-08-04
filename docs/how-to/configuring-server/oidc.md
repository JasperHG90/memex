# Authenticate with an OIDC provider

Memex accepts bearer tokens from your own OIDC provider alongside API keys. A verified JWT is mapped onto the same policy and vault scoping model that API keys use, so you can let people sign in through Entra ID, Zitadel, Keycloak, or Auth0 instead of sharing static secrets. This guide turns on OIDC on the server, maps token claims to policies, and logs a client in.

The bearer memex verifies must be a **signed JWT**, because the server checks it locally against the provider's JWKS. That is a requirement about the token, not about the vendor. Most providers sign the access token and work with no extra setup (Entra ID v2, Zitadel, Keycloak, Auth0). Some issue an opaque access token but do sign the id_token: HashiCorp Vault and Google both work this way. Those work too, once the client is told to send the id_token instead: see [Providers that sign the id_token](#providers-that-sign-the-id_token). A provider that issues no signed JWT at all, such as a GitHub OAuth app, cannot be verified on this path, and those callers keep using API keys.

The server setup below is the same for everyone. Only the way a client obtains a token differs, so follow the section that fits your caller:

- **A person at a terminal** signs in with a browser. Do the server setup, then [Log in as a person](#log-in-as-a-person-cli-and-mcp).
- **A service account** (a cron job, a headless MCP deployment) authenticates with no browser. Do the server setup, then [Service accounts](#service-accounts-non-interactive).

For the concepts behind this (why the token carries identity and the config carries only policy), see [How OIDC authentication works](../../explanation/how-memex-works/oidc-authentication.md).

## Prerequisites

- A running Memex server you can restart.
- An OIDC provider with a registered application (a `client_id`, and a `client_secret` only if it is a confidential client).
- The provider's issuer URL, for example `https://login.example.com/realms/memex`. The server discovers the JWKS from `<issuer>/.well-known/openid-configuration`.

## Turn on OIDC (server)

OIDC providers live under `server.auth.oidc`, next to `keys` <code-ref path="packages/common/src/memex_common/config.py" lines="1552-1585" />. Setting `enabled: true` switches on authentication; a request may then present either an API key or a bearer token.

```yaml
server:
  auth:
    enabled: true
    oidc:
      - issuer: "https://login.example.com/realms/memex"
        audience: ["memex-api"]
        grant_rules:
          - claim: "groups"
            value: "memex-admins"
            policy: admin
          - claim: "groups"
            value: "memex-writers"
            policy: writer
```

Each provider is selected by matching the token's `iss` claim, then verified: signature against the JWKS, `aud` against `audience`, `iss` against `issuer`, and expiry with a small clock-skew allowance <code-ref path="packages/core/src/memex_core/server/oidc.py" lines="191-256" />. The allowed signing algorithms default to `RS256` and `ES256` and are enforced against the token header, so an `alg: none` token is rejected.

### Map claims to a policy

A `grant_rule` matches when the named claim carries the given value: membership when the claim is a list (such as `groups` or `roles`), equality when it is a scalar (such as `email`). The first matching rule sets the request's policy and vault scope <code-ref path="packages/common/src/memex_common/config.py" lines="1399-1436" />. The three policies map to the same permission sets as API keys:

| Policy | Reads | Writes | Deletes |
|:------|:-----:|:------:|:-------:|
| `reader` | yes | no | no |
| `writer` | yes | yes | no |
| `admin` | yes | yes | yes |

Scope a grant to specific vaults the same way you scope a key:

```yaml
grant_rules:
  - claim: "groups"
    value: "team-a"
    policy: writer
    vault_ids: ["work"]
    read_vault_ids: ["archive"]
```

`read_vault_ids` widens read access without granting writes, and requires `vault_ids` to be set. If you want every verified token to get a baseline policy when no rule matches, set `default_policy`; leave it unset and an unmatched token is rejected. A provider must define at least one `grant_rule` or a `default_policy`, otherwise every token it issues is refused.

### Restart and confirm

Auth config is read at startup. Restart the server and watch the logs:

```
OIDC bearer-token authentication enabled (1 provider(s)).
```

That confirmation is logged at `INFO`, below the default level of `WARNING`, so set `server.logging.level: INFO` to see it. Refusal reasons are logged at `WARNING` and show up either way.

The non-localhost bind guard keys off `enabled`, so an OIDC-only server (providers set, `keys` empty) still binds. If Hermes or the browser extension also point at that server, keep at least one API key for them, since the login flow covers the CLI and MCP only.

## Log in as a person (CLI and MCP)

Point the client at the same provider under the top-level `oidc` block <code-ref path="packages/common/src/memex_common/config.py" lines="2970-2977" />:

```yaml
oidc:
  issuer: "https://login.example.com/realms/memex"
  client_id: "memex-cli"
```

Then log in:

```bash
memex auth login            # opens a browser (Authorization Code + PKCE)
memex auth login --device   # headless: shows a code and a URL to visit
```

The token is cached at `<user_config_dir>/memex/token.json` with `0600` permissions. From then on the CLI and MCP send `Authorization: Bearer` and refresh the token as it nears expiry; if OIDC is not configured or no token is cached, they fall back to `X-API-Key`. Check or clear the session:

```bash
memex auth status   # shows the identity and expiry
memex auth logout   # deletes the cached token
```

## Providers that sign the id_token

HashiCorp Vault's OIDC provider returns an **opaque batch token** as the `access_token`, which only authorizes Vault's own `userinfo` endpoint, and puts the signed, JWKS-verifiable JWT in the `id_token`. Sending the access token to memex therefore always fails: it is not a JWT, so there is nothing to verify. Set `credential` to make the client send the id_token instead.

```yaml
oidc:
  issuer: "https://vault.example.com/v1/identity/oidc/provider/memex"
  client_id: "memex-cli"
  credential: "id_token"
  scopes: ["openid", "offline_access", "groups"]
```

Setting `scopes` replaces the default list rather than adding to it, so spell out everything you need. `openid` is required and is enforced at config load. `offline_access` is what gets you a refresh token. Drop it and the bearer stops being sent at the first expiry: the client falls back to the API key if one is configured, and otherwise sends no credential at all, until you run `memex auth login` again. The last entry is whatever scope your provider maps to the claim you grant on.

An id_token's `aud` is the **client id**, not a separate API identifier, so the server's `audience` must list the client id:

```yaml
server:
  auth:
    enabled: true
    oidc:
      - issuer: "https://vault.example.com/v1/identity/oidc/provider/memex"
        audience: ["memex-cli"]        # the client_id, because that is the id_token's aud
        grant_rules:
          - claim: "groups"
            value: "memex-admins"
            policy: admin
```

Nothing else changes. The server verifies signature, `iss`, `aud`, and `exp` exactly as it does for an access token, then applies grant rules the same way.

After changing `credential`, run `memex auth login` again. A cached token is never reused across the switch, because its bearer is the token the new setting does not send; the client says so at `WARNING` and falls back to the API key until you log in again.

Two constraints are enforced at config load rather than left to fail as a `403`: `credential: id_token` requires `grant: interactive` (service-account and keyless grants never receive an id_token), and it requires the `openid` scope (without it the provider returns no id_token at all).

**Make sure the claim you grant on is actually in the token.** On Vault, `groups` reaches the id_token only when a scope template provides it *and* the client requests that scope, which is why `groups` is in the `scopes` list above. A token that verifies but carries no matching claim authenticates and then fails authorization, which is still a `403`. The server logs that case explicitly, so check the log before assuming the token itself is wrong.

### What you are accepting

An id_token is audience-scoped to the **client**, not to the memex API, so accepting one as a bearer credential is a real tradeoff and is opt-in for that reason:

- **The token is replayable by anything that receives it.** Once `audience` contains the client id, memex accepts any JWT from that issuer bearing that `aud`. It applies no `azp` check and no scope restriction. Since id_tokens are routinely passed around as identity proofs, treat one as equivalent to a bearer credential for memex.
- **Do not share that client id with another relying party.** Any service that can obtain an id_token for `memex-cli` can call memex as that user. Register a client used only for memex.
- **Rolling back has two halves.** Removing `credential` from the client config restores the previous behavior, and the cached token is disposable. But the server's widened `audience` is separate config and survives that change, so narrow it too.

## Service accounts (non-interactive)

A machine caller (a scheduled job, a headless MCP deployment) authenticates without a browser. Set `grant` on the client `oidc` block to a service-account grant; tokens are then acquired and re-acquired automatically, with no `memex auth login` step.

**Zitadel key file (JWT profile, recommended).** Download the service account's key JSON from Zitadel, then:

```yaml
oidc:
  issuer: "https://your-instance.zitadel.cloud"
  client_id: "memex-svc"
  grant: "jwt_profile"
  key_file: "/etc/memex/zitadel-key.json"
  scopes:
    - "openid"
    - "urn:zitadel:iam:org:project:id:<project-id>:aud"   # adds your API as audience
```

The client signs a short-lived assertion with the key and exchanges it at the token endpoint (RFC 7523 `jwt-bearer`).

**Client secret (client credentials).**

```yaml
oidc:
  issuer: "https://your-instance.zitadel.cloud"
  client_id: "memex-svc"
  grant: "client_credentials"
  client_secret: "env:MEMEX_OIDC_CLIENT_SECRET"
  scopes: ["openid", "urn:zitadel:iam:org:project:id:<project-id>:aud"]
```

On the **server**, map a claim the service token actually carries. Machine tokens have no `groups`/`email`; key rules on `roles` (Zitadel project roles), or pin the machine identity by `sub`:

```yaml
server:
  auth:
    oidc:
      - issuer: "https://your-instance.zitadel.cloud"
        audience: ["<project-id>"]
        grant_rules:
          - claim: "sub"                 # the service user's id
            value: "<service-user-id>"
            policy: writer
            vault_ids: ["ingest"]
```

Because rule matching does membership on list claims and equality on scalars, map on a `roles` array or a `sub` string, not a space-delimited `scope` string. `memex auth status` reports service-account mode.

## Keyless workloads (Nomad Workload Identity)

The `jwt_profile` and `client_credentials` grants above still put a long-lived secret (a key file or a client secret) on the workload host. A keyless grant removes it entirely: the workload presents an **ambient token its runtime already issues**, and the client just reads it. On Nomad that token is a **Workload Identity** JWT, delivered to the task and auto-rotated.

**Client** (`grant: token_file`): point the client at the file the runtime writes.

```yaml
oidc:
  issuer: "https://nomad.example.internal"   # your Nomad's OIDC issuer
  grant: "token_file"
  token_file: "/secrets/nomad_token.jwt"
```

The token is read fresh on every request (no cache, no secret stored). `grant: token_env` reads it from an environment variable instead, for runtimes that inject it there. `client_id` is not needed for these grants.

**Nomad job** (`identity` stanza; Nomad signs the JWT, sets the audience, and writes it to the task):

```hcl
job "hermes" {
  group "app" {
    task "hermes" {
      identity {
        name = "memex"
        aud  = ["memex"]
        file = true          # writes the token to secrets/nomad_token.jwt
        ttl  = "1h"          # Nomad re-renders before expiry
      }
      # ...
    }
  }
}
```

**Server** (config only) trusts Nomad as one more provider and pins the job identity:

```yaml
server:
  auth:
    enabled: true
    oidc:
      - issuer: "https://nomad.example.internal"
        audience: ["memex"]
        grant_rules:
          - claim: "nomad_job_id"     # Nomad WI tokens carry job/task identity
            value: "hermes"
            policy: writer
            vault_ids: ["hermes"]
```

The memex server must be able to reach Nomad's OIDC discovery + JWKS endpoints to verify these tokens.

## Verification

With the server running and a token in hand:

```bash
# 1. Protected endpoint with a valid bearer token — should succeed.
curl -s -o /dev/null -w "%{http_code}\n" \
     -H "Authorization: Bearer $TOKEN" \
     http://localhost:8000/api/v1/vaults

# 2. A tampered or expired token — should fail.
curl -s -o /dev/null -w "%{http_code}\n" \
     -H "Authorization: Bearer not-a-real-token" \
     http://localhost:8000/api/v1/vaults

# 3. An existing API key on the same server — still works.
curl -s -o /dev/null -w "%{http_code}\n" \
     -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/vaults
```

You want `200`, `403`, `200`. A request with no credential at all returns `401`.

## Troubleshooting

**Every bearer token returns `403`.** Read the server log first: every reason a bearer is refused is logged at `WARNING`, so it is already visible at the default log level. Each reason names itself, so you do not have to guess.

| Log line | Cause | Fix |
|:---|:---|:---|
| `not a parseable JWT` | The bearer is an opaque token, so there is nothing to verify. | If the provider signs the id_token instead, set `credential: id_token` on the client. If all its tokens are opaque, use an API key. |
| `no configured provider matches issuer` | The token's `iss` matches no configured `issuer`. Provider selection is by `iss`, so no provider is even tried. | Copy the issuer from the log into `server.auth.oidc[].issuer`, exactly. |
| `OIDC token rejected for issuer ...` | Signature, `aud`, or `exp` failed. The message names which. | Usually `aud`: add the audience the token carries. For an id_token that is the client id. |
| `matched no grant_rule` | The token verified but authorizes nothing. | Add a rule for one of the claim names in the log, or set a `default_policy`. If the claim you expected is missing, the provider is not issuing it: check the scope you requested. |
| `OIDC JWKS fetch failed for issuer ...` | The server cannot reach the provider's discovery or JWKS endpoint, so it can verify nothing. | Check DNS, egress rules, and the certificate from the memex server's own network location, since your workstation may reach the provider when the server cannot. |
| `OIDC token verification error for issuer ...` | The provider's key material could not be used, for example an unusable JWKS entry or a `kid` that resolves to a malformed key. | Fetch the provider's JWKS by hand and check the entry for the `kid` in the token header. |

Provider selection and the first two refusal paths are at <code-ref path="packages/core/src/memex_core/server/oidc.py" lines="166-206" />.

**No log line at all for a failing request.** The credential never reached the OIDC path. A request carrying `X-API-Key` is resolved as a key and never tried as a bearer, and a server with `enabled: true` but no `oidc` providers rejects every bearer outright.

**`memex auth login` says no provider is configured.** Set the top-level `oidc.issuer` and `oidc.client_id` in the client config the CLI loads.

**Login works but requests still send the API key.** The cached token is for a different issuer than the configured one, or it expired with no refresh token. Run `memex auth status`, then `memex auth login` again.

## See also

- [Explanation: How OIDC authentication works](../../explanation/how-memex-works/oidc-authentication.md)
- [Reference: AuthConfig and OIDC config](../../reference/configuration-options.md#authconfig)
- [How-to: Secure Memex with an API key](./api-key.md)
- [Explanation: architecture overview](../../explanation/how-memex-works/high-level-architecture.md)
