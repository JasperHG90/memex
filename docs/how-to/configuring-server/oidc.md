# Authenticate with an OIDC provider

Memex accepts bearer tokens from your own OIDC provider alongside API keys. A verified JWT access token is mapped onto the same policy and vault scoping model that API keys use, so you can let people sign in through Entra ID, Zitadel, Keycloak, or Auth0 instead of sharing static secrets. This guide turns on OIDC on the server, maps token claims to policies, and logs a client in.

Only providers that issue **signed JWT access tokens** work here, because the server verifies tokens locally against the provider's JWKS. Providers with opaque access tokens (Google, GitHub) are not supported on this path; those callers keep using API keys.

## Prerequisites

- A running Memex server you can restart.
- An OIDC provider with a registered application (a `client_id`, and a `client_secret` only if it is a confidential client).
- The provider's issuer URL, for example `https://login.example.com/realms/memex`. The server discovers the JWKS from `<issuer>/.well-known/openid-configuration`.

## Turn on OIDC (server)

OIDC providers live under `server.auth.oidc`, next to `keys` <code-ref path="packages/common/src/memex_common/config.py" lines="1521-1527" />. Setting `enabled: true` switches on authentication; a request may then present either an API key or a bearer token.

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

Each provider is selected by matching the token's `iss` claim, then verified: signature against the JWKS, `aud` against `audience`, `iss` against `issuer`, and expiry with a small clock-skew allowance <code-ref path="packages/core/src/memex_core/server/oidc.py" lines="141-182" />. The allowed signing algorithms default to `RS256` and `ES256` and are enforced against the token header, so an `alg: none` token is rejected.

### Map claims to a policy

A `grant_rule` matches when the named claim carries the given value: membership when the claim is a list (such as `groups` or `roles`), equality when it is a scalar (such as `email`). The first matching rule sets the request's policy and vault scope <code-ref path="packages/common/src/memex_common/config.py" lines="1399-1435" />. The three policies map to the same permission sets as API keys:

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

The non-localhost bind guard keys off `enabled`, so an OIDC-only server (providers set, `keys` empty) still binds. If Hermes or the browser extension also point at that server, keep at least one API key for them, since the login flow covers the CLI and MCP only.

## Log in (CLI and MCP)

Point the client at the same provider under the top-level `oidc` block <code-ref path="packages/common/src/memex_common/config.py" lines="2803-2809" />:

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

**Every bearer token returns `403`.** The token did not verify. Confirm the `iss` claim exactly matches a configured `issuer`, the `aud` claim contains one of the configured `audience` values, and the token is a JWT access token (not an opaque token or an id token). Provider selection is by `iss`, so a mismatch there means no provider is even tried <code-ref path="packages/core/src/memex_core/server/oidc.py" lines="141-156" />.

**A valid token authenticates but is denied.** It matched no `grant_rule` and there is no `default_policy`, so it authenticated without an authorization. Add a rule for its claims or set a `default_policy`.

**`memex auth login` says no provider is configured.** Set the top-level `oidc.issuer` and `oidc.client_id` in the client config the CLI loads.

**Login works but requests still send the API key.** The cached token is for a different issuer than the configured one, or it expired with no refresh token. Run `memex auth status`, then `memex auth login` again.

## See also

- [How-to: Secure Memex with an API key](./api-key.md)
- [Reference: server configuration](../../reference/configuration-options.md)
- [Explanation: architecture overview](../../explanation/how-memex-works/high-level-architecture.md)
