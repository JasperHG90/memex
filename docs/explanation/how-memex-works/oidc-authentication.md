# How OIDC authentication works

With OIDC, Memex stops storing who you are and keeps only what you may do. Your identity provider (Entra ID, Zitadel, Keycloak, Auth0) proves your identity and signs that proof into a token. Memex verifies the signature, reads the claims, and maps them to a policy. There is no user table, no password, and no identity in the Memex config. This page explains that split and why it is the whole design.

## Context

The API-key model answers "who are you" and "what may you do" with a single secret. A key is a bearer credential: whoever holds it is the reader, writer, or admin the config says it is. That is simple and it works for one operator or a handful of scripts. It does not scale to a team, because every person shares keys, key rotation is manual, and the audit trail records a key prefix rather than a name.

OIDC splits the two questions. Authentication (who are you) moves to a provider your organization already runs. Authorization (what may you do) stays in Memex. The bridge between them is a signed token.

## The trust chain

An OIDC access token is a JWT: three base64 segments carrying a header, a set of claims, and a signature. The claims are facts the provider asserts about the caller, for example:

```json
{
  "iss": "https://login.example.com/realms/memex",
  "sub": "8f3c-user-uuid",
  "email": "alice@example.com",
  "groups": ["memex-writers"],
  "aud": "memex-api",
  "exp": 1721500000
}
```

When a request arrives with `Authorization: Bearer <jwt>`, Memex does four things:

1. **Select the provider.** It reads the unverified `iss` claim and finds the configured provider whose `issuer` matches. This read is not yet trusted; it only picks which keys to verify against.
2. **Verify the signature.** It fetches the provider's public keys (JWKS, discovered once and cached) and checks that the provider signed this exact token. This is the load-bearing step. A valid signature is what lets Memex trust the `groups` and `email` values inside.
3. **Check the standard claims.** The audience (`aud`) must match, the issuer (`iss`) must match the selected provider, and the token must not be expired (with a small clock-skew allowance). Only asymmetric signing algorithms are accepted, so a forged `alg: none` token is rejected.
4. **Map claims to a policy.** The provider's grant rules turn a claim value into a `reader`/`writer`/`admin` policy and an optional vault scope. The first matching rule wins.

The result is an `AuthContext`: the same authorization object an API key produces. From that point on, every permission check in Memex is identical whether the caller presented a key or a token.

## Why the config holds no identity

A Memex OIDC config lists trusted issuers and rules like "the `memex-writers` group gets the `writer` policy." It never lists people. That is deliberate. The token says who you are and which groups you are in; your provider is the source of truth and signs that assertion. The Memex config says only what those groups are allowed to do. So Memex never looks anyone up. It reads cryptographically signed claims and applies a policy. Add a person to the `memex-writers` group in your provider and they can write; remove them and they cannot, with no change to Memex.

This also means a verified token behaves like a short-lived, self-describing API key that the provider mints on demand. Everything downstream (vault scoping, the audit actor, admin gating) treats it the same way, which is why the two schemes coexist on one server without special cases.

## Human identity and machine identity

The same trust chain serves both audiences. What differs is how the caller obtains a token, not how Memex verifies it.

- **A person** runs `memex auth login`. The CLI performs the Authorization Code flow with PKCE (or a device flow when there is no browser), the provider authenticates the human, and the resulting token carries their `sub` and `email`. The audit trail records the person.
- **A service account** carries no browser and no human. It authenticates with the client-credentials grant (a client secret) or the JWT-profile grant (a signed assertion from a downloaded key file, the way Zitadel recommends). The CLI and MCP acquire and refresh these tokens automatically, with no login step. The token's identity is the machine's `sub` or `client_id`.

Both paths end at the same verifier and the same `AuthContext`. The only practical difference on the server is which claim you map: human tokens usually carry `groups` or `email`, while machine tokens carry `roles` or just a `sub`, so a service account's grant rule keys on one of those instead.

## What Memex deliberately does not do

- **It does not issue tokens.** Memex is a resource server, not an authorization server. It has no `/authorize` or `/token` endpoint of its own; your provider issues tokens and Memex only verifies them.
- **It does not persist users.** Identity is ephemeral, present only for the life of a request. The audit log records a subject string, not a row in a user table.
- **It does not accept opaque tokens.** Verification is local, against the provider's JWKS, so a provider whose access tokens are opaque strings rather than signed JWTs (Google, GitHub) cannot be used on this path. Those callers keep using API keys.

## See also

- [How-to: Authenticate with an OIDC provider](../../how-to/configuring-server/oidc.md)
- [Reference: AuthConfig and OIDC config](../../reference/configuration-options.md#authconfig)
- [How-to: Secure Memex with an API key](../../how-to/configuring-server/api-key.md)
