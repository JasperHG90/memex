---
epic = "auth"
priority = 20
summary = """
Let a client send the id_token as its bearer credential (opt-in
`oidc.credential: id_token`), so OIDC human login works against providers that
sign the id_token and issue an opaque access_token (HashiCorp Vault). Plus:
close all three silent-403 paths with diagnostics, name the real blocker in the
docs, and reject at config load the combinations that cannot work.
"""
---
# Ticket: oidc-id-token-credential

## 1. Title

Accept the id_token as the bearer credential for OIDC providers that sign it
(HashiCorp Vault), so human login stops failing with an unexplained `403`.

## 2. Size / Effort

**M.** One opt-in config field with two validators, its wiring through the
client token cache (4 functions in one module), three diagnostic log lines on
the server, docs, and a CI step per newly-gated suite. No schema, no migration,
no change to how a token is verified. Effort is driven by the failure modes,
not the happy path: the cache-compatibility guard, the id_token's own expiry,
and the three distinct ways a request can silently become a `403`.

## 3. Triggered by

[Issue #277](https://github.com/JasperHG90/memex/issues/277): every `memex auth
login` against HashiCorp Vault's OIDC provider ends in `403`. Vault returns an
opaque batch token as `access_token` (it only authorizes Vault's own
`userinfo` endpoint) and puts the signed, JWKS-verifiable JWT in `id_token`.
memex sends the `access_token`. The credential memex accepts is the one Vault
does not sign; the one Vault signs is the one memex never sends.

## 4. Context

**The client always sends the access token.** `_resolve_bearer` returns
`f'{cache.token_type} {cache.access_token}'` for the interactive grant
(`packages/common/src/memex_common/auth_client.py:220`, again at `:228` after a
concurrent refresh and `:234` after its own). `TokenCache` has no id_token
field at all (`packages/common/src/memex_common/auth_client.py:40-53`), and
`token_cache_from_response` reads only `data['access_token']`
(`packages/common/src/memex_common/auth_client.py:125`). The id_token is parsed
exactly once, unverified, for a display string: `_subject_from_id_token`
(`packages/cli/src/memex_cli/auth.py:137`), used at
`packages/cli/src/memex_cli/auth.py:245` and
`packages/cli/src/memex_cli/auth.py:285`.

**The server verifies an id_token unchanged.** `OidcVerifier.verify` selects a
provider on the unverified `iss` claim
(`packages/core/src/memex_core/server/oidc.py:161-163`) and validates
`iss`/`aud`/`exp` against config
(`packages/core/src/memex_core/server/oidc.py:198-202`). Nothing in the
verifier distinguishes an id_token from an access token. Confirmed empirically
during plan review: synthetic RS256 id_tokens shaped like Vault's (`iss` = the
provider URL, `aud` = the client id, plus `nonce`/`at_hash`/`azp`) produced a
full `AuthContext` against a synthetic JWKS, with a scalar `aud` and with a
list `aud`, and with zero server changes. So the fix is entirely on the client:
which token it sends.

**Three paths turn a bearer into a `403`, and all three are silent.** Probed
against the real verifier with a root-logger handler capturing at DEBUG:

```
--- vault-opaque                  -> None; log records = 0
--- bogus-jwt-unknown-iss         -> None; log records = 0
--- verified-but-no-matching-rule -> None; log records = 0
```

1. `_unverified_parts` raises on the unpack, and the `except` at
   `packages/core/src/memex_core/server/oidc.py:158` returns `None` with no log.
   This is the opaque-token case from issue #277.
2. The unknown-issuer branch at
   `packages/core/src/memex_core/server/oidc.py:163` returns `None` with no log.
3. `_claims_to_context` returns `None` at
   `packages/core/src/memex_core/server/oidc.py:96-97` when the verified token
   matches no grant rule and the provider sets no `default_policy`.
   `authenticate_request` (`packages/core/src/memex_core/server/auth.py:134-136`)
   turns that into `AuthFailure('invalid')`, a `403`.

Path 3 is the one a Vault operator following this ticket will actually hit,
because a Vault id_token carries `groups` only when the operator assigns a
scope template AND the client requests that scope (see P5). Only a bad `aud`
logs anything today (`OIDC token rejected for issuer ...`,
`packages/core/src/memex_core/server/oidc.py:209`), so every other rejection is
indistinguishable from a misconfiguration in the server log.

**The docs point the wrong way.** `docs/how-to/configuring-server/oidc.md:5`
frames opaque access tokens as a "(Google, GitHub)" problem, which reads as
"use a real OIDC provider instead". Vault is a real OIDC provider: discovery,
JWKS, RS256, auth-code with PKCE. The same "signed JWT access tokens only"
claim appears in four more places that all go stale with this change:
`docs/explanation/how-memex-works/oidc-authentication.md:55`,
`packages/core/src/memex_core/server/oidc.py:8-9` (module docstring),
`packages/common/src/memex_common/config.py:1469-1471`
(`OidcProviderConfig` docstring), and
`docs/reference/configuration-options.md:287`.

## 5. Non-goals / out of scope

- **No change to how a token is verified.** The signature and claims logic in
  `OidcVerifier.verify` is untouched; the only server edits are three log lines.
- **No `userinfo`-endpoint support.** Exchanging an opaque access token at the
  provider's `userinfo` endpoint is a different (network-per-request) design.
  Not this ticket.
- **No new default.** `credential` defaults to `access_token`; every existing
  deployment's behavior is byte-identical.
- **No id_token support for the service-account or keyless grants.**
  `client_credentials` and `jwt_profile` responses carry no id_token;
  `token_file` and `token_env` read a raw token from the runtime. Rejected at
  config load, not silently ignored.
- **No token-cache migration.** The cache is disposable local state
  (`memex auth login` re-mints it); new fields default so an old file loads.
- **No `azp` check and no scope enforcement on the server.** Accepting an
  id_token as a bearer is deliberately a plain JWT acceptance; the security
  consequence is documented (R13), not mitigated in code. Changing that is a
  separate design.
- **No widening of the `just test` recipe.** See section 11, Q2.
- **No general test-isolation sweep.** Section 8 fixes exactly the four
  pre-existing failures in the two suites this ticket newly gates on CI, and
  nothing else.

## 6. Requirements & restrictions

- **R1. Opt-in, defaulted off.** A `credential` field of type
  `Literal['access_token', 'id_token']` on `OidcClientConfig`
  (`packages/common/src/memex_common/config.py:1597`), default `access_token`.
  Existing configs unchanged.
- **R2. Reject the impossible combinations at config load**, matching the
  existing per-grant validator style
  (`packages/common/src/memex_common/config.py:1678`, which raises `ValueError`
  per grant). Two rules: `credential='id_token'` is valid only with
  `grant='interactive'`; and it requires `openid` in `scopes`, since without
  that scope the provider returns no id_token at all and every login would fail
  at runtime instead. `scopes` is operator-overridable
  (`packages/common/src/memex_common/config.py:1625-1628`), so the default
  containing `openid` does not make the check redundant.
- **R3. A cached token must never be reused across a credential switch.**
  Mirror the existing mode guard
  (`packages/common/src/memex_common/auth_client.py:215`), where a cache whose
  mode does not match falls back rather than sending the wrong token. The cache
  records which credential minted it; a mismatch falls back.
- **R4. Expiry must never outlast either token.** Set
  `expires_at = min(now + expires_in, id_token_exp)` when sending the id_token.
  Taking the id_token's `exp` alone can stretch the refresh interval to a full
  id_token lifetime (operator-set, commonly hours), during which an unused
  refresh token can pass a provider's idle timeout and kill the session; taking
  `expires_in` alone can send an already-expired id_token
  (`packages/common/src/memex_common/auth_client.py:122` feeds `is_fresh`,
  `packages/common/src/memex_common/auth_client.py:55`, consumed at
  `packages/common/src/memex_common/auth_client.py:219`). `min` has neither
  failure.
- **R5. Fail loudly on a provider that stops returning a usable id_token.**
  Never silently fall back to sending the access token, which reproduces the
  exact `403` this ticket fixes. This binds three cases: no `id_token` in the
  response; an `id_token` whose `exp` cannot be read (raise, do not fall back
  to `expires_in`, which is the bug R4 exists to fix); and a refresh response
  that drops the id_token. The caller of `_refresh_token` already catches
  `ValueError` and warns
  (`packages/common/src/memex_common/auth_client.py:231-233`); the CLI login
  path does not, so `login` must catch it too
  (`packages/cli/src/memex_cli/auth.py:88`).
- **R6. Never log token material, and never log an unverified value raw.**
  `_read_workload_token` states the first half ("The token contents are never
  logged", `packages/common/src/memex_common/auth_client.py:250`). New
  diagnostics log structure, issuer, and claim NAMES only, never claim values.
  Second half, new here: the unmatched `iss` in R12's second diagnostic comes
  from an UNVERIFIED, pre-auth, attacker-controlled payload. Every log in that
  module today prints only the CONFIGURED `provider.issuer` or an authlib
  exception (`packages/core/src/memex_core/server/oidc.py:172`,
  `packages/core/src/memex_core/server/oidc.py:192`,
  `packages/core/src/memex_core/server/oidc.py:209`,
  `packages/core/src/memex_core/server/oidc.py:213`), so this would be the
  first attacker-controlled value in a memex auth log. It MUST be truncated to
  a bounded length and `%r`-quoted, so a newline or terminal escape in the
  claim cannot forge or corrupt a log line.
- **R7. Every code change ships with a test**
  (`.claude/rules/python-testing.md:1`, constraint `all-code-needs-tests`).
- **R8. Docs must match new behavior**: a config-schema change with stale docs
  fails the enabled `documentation` review pass (`.loop/config.json:24`). All
  five stale-prose locations in section 4 are in scope.
- **R9. Style**: single quotes, line length 100, ruff plus mypy strict, Python
  3.12 or newer, all I/O async (`CLAUDE.md:1`, "Code style").
- **R10. Docs prose must pass the slop scan**
  (`.claude/rules/slop-scan-for-docs.md:1`): 80-char wrap, no em dashes, no
  tier-1 slop, American spellings, evidence-backed claims only.
- **R11. `TokenCache.bearer_token` must not be able to return the wrong
  token.** Add a `model_validator` requiring `id_token` to be set when
  `credential == 'id_token'`. The idiomatic one-liner
  `self.id_token or self.access_token` is FORBIDDEN: it is exactly the silent
  fallback R5 prohibits, in one line.
- **R12. All three silent `403` paths must log a distinguishable reason**, so
  an operator can tell an opaque token from an unknown issuer from a token that
  authenticated but matched no grant rule (section 4). This is issue #277's
  resolution 2, and path 3 is the one this ticket's own users will hit.
- **R13. The security tradeoff must be stated concretely enough for a doc
  reviewer to check.** Two facts: (a) an id_token accepted as a bearer is
  replayable by any party that receives it, since it carries no audience
  binding to the memex API and `verify` applies no `azp` check and no scope
  restriction; (b) the client id placed in the server's `audience`
  (`packages/common/src/memex_common/config.py:1481`) must not be shared with
  another relying party.

## 7. Code surface

**`packages/common/src/memex_common/config.py`**

- `OidcClientConfig` (`packages/common/src/memex_common/config.py:1597`) - add
  the `credential` field after `grant`
  (`packages/common/src/memex_common/config.py:1634`).
- `validate_grant_requirements`
  (`packages/common/src/memex_common/config.py:1678`) - add both R2 rules.
- Class docstring (`packages/common/src/memex_common/config.py:1598-1614`) -
  one line on the id_token mode.
- `OidcProviderConfig` docstring
  (`packages/common/src/memex_common/config.py:1469-1471`) - stale "signed JWT
  access tokens" claim (R8).

**`packages/common/src/memex_common/auth_client.py`**

- `TokenCache` (`packages/common/src/memex_common/auth_client.py:40`) - add
  `id_token: str | None = None` and `credential: str = 'access_token'`; add the
  R11 `model_validator`; add a `bearer_token` property returning the token the
  credential names.
- `token_cache_from_response`
  (`packages/common/src/memex_common/auth_client.py:114`) - accept
  `credential`; store the id_token; when the credential is the id_token,
  require it in the response and compute `expires_at` per R4.
- New module-private helper - unverified base64url read of a JWT `exp` claim,
  returning `None` when absent or unparseable. The server verifies; this only
  schedules the refresh. Its `None` is a hard error at the call site (R5), not
  a fallback.
- `_refresh_token` (`packages/common/src/memex_common/auth_client.py:137`) -
  pass the configured credential through to `token_cache_from_response`
  (`packages/common/src/memex_common/auth_client.py:165`).
- `_resolve_bearer` (`packages/common/src/memex_common/auth_client.py:204`) -
  add the credential mismatch to the fall-back guard
  (`packages/common/src/memex_common/auth_client.py:215`) and to the post-lock
  re-check (`packages/common/src/memex_common/auth_client.py:227`); send the
  cache's `bearer_token` at
  `packages/common/src/memex_common/auth_client.py:220`,
  `packages/common/src/memex_common/auth_client.py:228`, and
  `packages/common/src/memex_common/auth_client.py:234`.
- `acquire_service_token`
  (`packages/common/src/memex_common/auth_client.py:346`) - leaves the
  credential at its `access_token` default (service grants never see an
  id_token; R2 makes the combination unreachable, so no branch is added here).

**`packages/core/src/memex_core/server/oidc.py`**

- Module docstring (`packages/core/src/memex_core/server/oidc.py:8-9`) - stale
  "signed JWT access tokens" claim (R8).
- `verify` (`packages/core/src/memex_core/server/oidc.py:157-159`) - log INFO
  when the bearer does not parse as a JWT, naming the segment count and
  pointing at the token-type requirement (R6: no token material).
- `verify` (`packages/core/src/memex_core/server/oidc.py:161-164`) - log INFO
  when no configured provider matches the token's `iss`, naming the unmatched
  issuer.
- `verify` (`packages/core/src/memex_core/server/oidc.py:216`) - log INFO when
  `_claims_to_context` returns `None`, naming the issuer and the claim NAMES
  present (R6, R12). Logging at the `verify` call site keeps
  `_claims_to_context`
  (`packages/core/src/memex_core/server/oidc.py:83`) a pure mapping function.

**`packages/cli/src/memex_cli/auth.py`**

- `_loopback_login` (`packages/cli/src/memex_cli/auth.py:244`) and
  `_device_login` (`packages/cli/src/memex_cli/auth.py:284`) - pass the
  configured credential into `token_cache_from_response`.
- `login` (`packages/cli/src/memex_cli/auth.py:88`) - catch `ValueError` so an
  R5 failure prints a login error instead of a traceback.

**Tests** (each file is the declared home for the section 8 tests):

- `packages/common/tests/test_config_oidc.py` - R1 and R2 config validation.
- `packages/common/tests/test_auth_client.py` - R3, R4, R5, R11 cache plus
  bearer resolution plus refresh.
- `packages/core/tests/unit/test_server_oidc.py` - the three diagnostics, and
  an end-to-end verify of a Vault-shaped id_token whose `aud` is the client id.
- `packages/cli/tests/test_auth_cmd.py` - login stores the id_token, and an R5
  failure exits cleanly.
- `packages/common/tests/test_config_structure.py` - pre-existing isolation
  failures blocking the CI step (section 8).
- `packages/cli/tests/test_process.py` - pre-existing isolation failure
  blocking the CI step (section 8).

**Docs**

- `docs/how-to/configuring-server/oidc.md` - reframe line 5 (token type, not
  vendor; name Vault); new "Providers that sign the id_token" section carrying
  the R13 security facts; troubleshooting entries at
  `docs/how-to/configuring-server/oidc.md:222` covering all three `403` paths.
- `docs/explanation/how-memex-works/oidc-authentication.md:55` - reframe.
- `docs/reference/configuration-options.md:94` - the `OidcClientConfig` table
  gains a `credential` row. Hand-maintained table, no generator.
- `docs/reference/configuration-options.md:287` - stale `OidcProviderConfig`
  prose (R8).

**CI**

- `.github/workflows/ci.yaml` - the `python-tests` job
  (`.github/workflows/ci.yaml:86`) runs `tests`, `packages/core/tests/unit`,
  and `packages/core/tests/integration`. `packages/common/tests` and
  `packages/cli/tests` run in no job, so three of this ticket's four test homes
  would never gate on CI. Add a step for each, mirroring the existing per-suite
  step style.

## 8. Tests & validation gates

Gates (from `.loop/config.json:2` and `justfile:61`, `justfile:65`):

1. `just test`, which runs `uv run pytest tests` (root E2E only).
2. `just prek`, which runs `uv run prek run -a` (ruff format, ruff lint, mypy
   strict).

**Gate gap, must be run explicitly:** `just test` collects only `tests/`, so
none of this ticket's test files run under either configured gate. Run and
report each suite SEPARATELY, the way CI invokes them: `uv run pytest
packages/common/tests`, then `uv run pytest packages/cli/tests`, then `uv run
pytest packages/core/tests/unit`. Combining them into one invocation causes
cross-suite state pollution: measured, that combined command fails one
procedural-repository source-pointer case under
`packages/core/tests/unit/services/`, which passes both in isolation and when
its own suite runs alone. That is an artifact of the combined command, not a
defect, and CI never hits it (CI invokes each directory as its own step).

**Measured baseline on this worktree, per suite:**

```
packages/common/tests      3 failed, 586 passed, 4 skipped
packages/cli/tests         1 failed, 578 passed
packages/core/tests/unit   3598 passed, 14 skipped      (green)
tests  (just test)         56 passed, 208 deselected    (green)
```

The four failures are pre-existing and environment-dependent, and all four sit
in the two suites this ticket newly adds to CI. Adding a CI step for a suite
that is red on any developer machine is not a deliverable, and
`.claude/rules/pre-existing-issues.md:1` forbids skipping them, so this ticket
fixes exactly these four and no others:

- `test_config_server_url_default`, `test_config_server_url_derived_from_custom_host_port`,
  and `test_default_model_override_propagates` in
  `packages/common/tests/test_config_structure.py` construct `MemexConfig()`
  and read the developer's REAL user config, violating
  `.claude/rules/python-testing.md:1` ("No test should read or write real user
  directories"). The loader already ships the escape hatch these tests need:
  `MEMEX_LOAD_GLOBAL_CONFIG` and `MEMEX_LOAD_LOCAL_CONFIG`, both documented
  in-code as "useful for tests"
  (`packages/common/src/memex_common/config.py:64`,
  `packages/common/src/memex_common/config.py:101`). Exactly ONE of the three
  (`test_default_model_override_propagates`) already sets the local hatch and
  misses the global one; the other two set neither. The failures differ too:
  two assert on `server_url` (`assert 'https://memex.lab.orangecluster.nl' ==
  'http://127.0.0.1:8000'`) and the third on the model
  (`assert 'gemini/gemini-3-flash-preview' == 'custom/model'`). Fix: set both
  hatches in each, inside the `patch.dict(os.environ, ...)` block, per
  `CLAUDE.md:1` ("Use `patch.dict(os.environ, ...)` for config tests").
- `TestCheckPortAvailable::test_available_port` in
  `packages/cli/tests/test_process.py:60` hardcodes port 59999 and fails
  whenever that port is in use, violating the same rule file's determinism
  requirement. Fix: bind an ephemeral port to discover a free one, close it,
  then assert. The sibling `test_occupied_port`
  (`packages/cli/tests/test_process.py:63`) already uses `bind(('127.0.0.1', 0))`;
  match it.

These four are expected to be green on a clean CI runner (no user config, free
ports), so the CI steps are safe to add either way; the fixes make the suites
green on a developer machine too.

Tests to add:

| # | Test | Home | Asserts |
|---|---|---|---|
| T1 | `credential` defaults to `access_token` | `test_config_oidc.py` | R1 |
| T2 | `credential='id_token'` with `grant='interactive'` loads | `test_config_oidc.py` | R1 |
| T3 | `credential='id_token'` with each non-interactive grant raises | `test_config_oidc.py` | R2 |
| T3b | `credential='id_token'` without `openid` in `scopes` raises | `test_config_oidc.py` | R2 |
| T4 | `expires_at` is `min(now + expires_in, id_token exp)`, both orderings | `test_auth_client.py` | R4 |
| T5 | An id_token credential with no id_token in the response raises | `test_auth_client.py` | R5 |
| T5b | An id_token whose `exp` is absent or unparseable raises, no `expires_in` fallback | `test_auth_client.py` | R5 |
| T6 | `_resolve_bearer` sends the id_token when configured | `test_auth_client.py` | R3 |
| T7 | A cache minted under the other credential is not reused (falls back to the API key), both directions | `test_auth_client.py` | R3 |
| T8 | Refresh preserves the credential; a refresh with no id_token warns and falls back | `test_auth_client.py` | R5 |
| T9 | Old cache JSON with no `credential` or `id_token` keys still loads | `test_auth_client.py` | back-compat |
| T9b | `TokenCache(credential='id_token')` with no id_token is rejected at construction | `test_auth_client.py` | R11 |
| T10 | An opaque bearer logs a JWT-shape diagnostic and no token material | `test_server_oidc.py` | R6, R12 |
| T11 | An unknown-issuer bearer logs the unmatched issuer | `test_server_oidc.py` | R6, R12 |
| T11c | An over-long `iss` carrying a newline is truncated and quoted in the log, so it cannot forge a log line | `test_server_oidc.py` | R6 |
| T11b | A verified token matching no grant rule logs the issuer and claim names, no claim values | `test_server_oidc.py` | R6, R12 |
| T12 | A Vault-shaped id_token whose `aud` is the client id verifies to an AuthContext | `test_server_oidc.py` | P2 |
| T13 | `memex auth login` caches the id_token under the id_token credential | `test_auth_cmd.py` | R1 |
| T13b | A login whose token response omits the id_token exits 1 with a message, not a traceback | `test_auth_cmd.py` | R5 |

Environment note: `pyproject.toml:39` pins `required-version = "~=0.11.0"`, and
`uv sync --all-extras` fails on macOS arm64 because the `gpu` extra pins
`onnxruntime-gpu` to a linux/win-only wheel
(`packages/core/pyproject.toml:76`). Sync with the named extras instead of
`--all-extras`.

## 9. Risk assessment

- **Blast radius: small and opt-in.** Every new client path sits behind the
  id_token credential, which no existing config sets. The default branch is the
  current code. The three server log lines are additive and change no control
  flow.
- **Highest risk: the cache guard (R3).** Getting it wrong means a cached
  access token is sent after a switch to the id_token credential (a confusing
  `403`, the status quo), or a cached id_token is sent to a provider expecting
  the access token. T7 pins both directions. The guard mirrors the proven mode
  guard at `packages/common/src/memex_common/auth_client.py:215`.
- **Second risk: expiry (R4).** Naming it precisely, because the intuitive
  reading is backwards: `expires_at` feeds `is_fresh`
  (`packages/common/src/memex_common/auth_client.py:55`), so an id_token whose
  `exp` is LATER than `expires_in` makes the client refresh LATER, not earlier,
  and an unused refresh token can pass a provider's idle timeout in the
  meantime. The opposite ordering sends an expired token. R4's `min` is the
  only choice with neither failure; T4 pins both orderings. Known accepted
  edge: an id_token whose whole TTL is shorter than the 60s refresh skew
  (`packages/common/src/memex_common/auth_client.py:32`) yields a cache that is
  never `is_fresh`, so every request refreshes. That degrades throughput on a
  pathological provider config rather than breaking correctness, and it is the
  same behavior today for an equally short `expires_in`. Not gated.
- **Third risk: a silent fallback re-introducing the bug (R5, R11).** Every
  place that could quietly substitute the access token for a missing id_token
  reproduces exactly the `403` this ticket fixes, and would do so on the opt-in
  path where the operator believes it is fixed. T5, T5b, T8, T9b pin the four
  places.
- **Security, stated as the two checkable facts of R13.** An id_token is
  audience-scoped to the client, not to the memex API. Once the operator adds
  the client id to `audience`
  (`packages/common/src/memex_common/config.py:1481`), `verify` accepts ANY JWT
  from that issuer bearing that `aud`, with no `azp` check and no scope
  restriction. So (a) such a token is replayable by any party that receives it,
  and id_tokens are routinely forwarded as identity proofs; (b) that client id
  must not be shared with another relying party. This is why the feature is
  opt-in and documented rather than defaulted, matching issue #277's own
  framing of resolution 3.
- **Reversibility: client-side total, server-side partial.** Removing the
  client field restores the previous behavior and the cache is disposable. But
  the operator's `audience` widening is server config
  (`packages/common/src/memex_common/config.py:1481`, `min_length=1`) and
  survives removal of the field; rolling back fully means narrowing `audience`
  too. The doc must say so.
- **Diagnostics are a pre-auth path.** All three new log lines sit before
  authentication, so an unauthenticated caller can drive them. They are INFO,
  matching the existing rejection log at
  `packages/core/src/memex_core/server/oidc.py:209`, and carry no token
  material and no claim values.

## 10. Subtickets

One loop iteration, ordered:

1. Config field and both validators (R1, R2) with T1, T2, T3, T3b.
2. `TokenCache`, its validator, `token_cache_from_response`, and the expiry
   helper (R4, R5, R11) with T4, T5, T5b, T9, T9b.
3. `_resolve_bearer` and `_refresh_token` guards (R3) with T6, T7, T8.
4. CLI login wiring and its error path (R5) with T13, T13b.
5. Server diagnostics for all three paths (R6, R12) with T10, T11, T11b, T12.
6. Pre-existing isolation fixes (section 8), then the CI steps.
7. Docs (R8, R10, R13), including all five stale-prose locations.

## 11. Open questions

- **Q1. Should the server also accept an explicit "this provider issues
  id_tokens" marker?** Recommendation: **no**. The server verifies `iss`, `aud`,
  signature, and `exp` identically either way, as the plan-review probe
  confirmed; an operator expresses the intent by setting
  `audience: ["<client-id>"]`. A server-side marker would be a second way to
  say the same thing. Revisit only if a provider needs different claim
  validation.
- **Q2. Widen `just test` to cover the package test suites?**
  Recommendation: **not in this ticket**, because it changes the gate for every
  in-flight ticket at once. This ticket adds the missing CI steps (so the tests
  gate on push) and runs the package suites explicitly. Flagged for a follow-up
  ticket.
- **Q3. Should `verify` enforce `azp` when the audience is a client id?**
  Recommendation: **no, document instead** (R13, and the non-goal above). An
  `azp` check would be a real mitigation but changes verification semantics for
  every provider, and no existing deployment has asked for it. Revisit if
  id_token acceptance sees real adoption.

## Amendments after review

- **Log level (§7, §9): INFO became WARNING.** The plan specified `logger.info`
  for the new diagnostics, matching the existing rejection log. The adversarial
  pass showed that defeats R12 and eval row 9 outright: `LoggingConfig.level`
  defaults to `WARNING`
  (`packages/common/src/memex_common/config.py:1796`), so an INFO diagnostic is
  invisible on a stock server and the troubleshooting doc's "read the log" step
  dead-ends. Every reason a bearer is REFUSED now logs at WARNING, including the
  pre-existing `OIDC token rejected for issuer` line (raised for parity) and the
  JWKS-fetch failure (which refuses every bearer). Pinned by
  `test_all_rejection_reasons_survive_the_default_log_level` and
  `test_jwks_fetch_failure_is_visible_at_the_default_log_level`.
- **id_token persistence (§7):** it is written to the cache only when it is the
  credential being sent. The plan implied storing it either way; that puts a
  second live credential on disk that no code path reads, contradicting §5's
  "byte-identical behavior". Eval row 2 was amended to match (a tightening).
- **Credential-mismatch fall-back (R3):** now logs a WARNING naming both
  credentials and pointing at `memex auth login`. The plan only required the
  fall-back, which would have left the operator with an unexplained 401.

## Premises / assumptions

- **P1. The client sends the access token and never the id_token.** VERIFIED -
  `packages/common/src/memex_common/auth_client.py:220` and
  `packages/common/src/memex_common/auth_client.py:234` return
  `cache.access_token`; `TokenCache`
  (`packages/common/src/memex_common/auth_client.py:40-53`) has no id_token
  field; the only id_token read in the codebase is the display-only
  `_subject_from_id_token` (`packages/cli/src/memex_cli/auth.py:137`, called at
  `packages/cli/src/memex_cli/auth.py:245` and
  `packages/cli/src/memex_cli/auth.py:285`). Plan review independently
  enumerated every other bearer-producing path (`_read_workload_token`,
  `_resolve_service_bearer`, `acquire_service_token`) and found no other module
  in `packages/*/src` that reads `TokenCache` or builds an `Authorization`
  header.
- **P2. A Vault id_token verifies against the existing server code once
  `audience` is the client id.** VERIFIED empirically during plan review and
  pinned by T12. probe: mint an RS256 JWT with `iss` = provider URL, `aud` =
  client id, plus `nonce`/`at_hash`/`azp`, serve a synthetic JWKS, call
  `OidcVerifier.verify` (read-only: in-memory keys, no network, no writes).
  Result: a full `AuthContext` for both a scalar and a list `aud`. Mechanism:
  `verify` selects on `iss`
  (`packages/core/src/memex_core/server/oidc.py:161-163`) and validates `aud`
  against the provider's audience list
  (`packages/core/src/memex_core/server/oidc.py:198-202`), and authlib's base
  `JWTClaims.validate` checks only iss/sub/aud/exp/nbf/iat/jti, so id_token-only
  claims are ignored.
- **P3. All three `403` paths are silent in the log today.** VERIFIED by probe;
  captured output is quoted in section 4. probe: construct an `OidcVerifier`
  over a synthetic provider, attach a root-logger handler at DEBUG, call
  `verify()` on an opaque token, an unknown-issuer JWT, and a valid token
  matching no grant rule (read-only: no network, no writes). All three returned
  `None` with zero log records.
- **P4. The `packages/common` and `packages/cli` test suites run in no CI
  job.** VERIFIED - `.github/workflows/ci.yaml:86-127` lists exactly `tests`
  (twice), `packages/core/tests/unit`, and `packages/core/tests/integration`;
  the only other pytest invocations are a collect-only MCP import gate
  (`.github/workflows/ci.yaml:146`), the hermes runner subdirectory
  (`.github/workflows/ci.yaml:217`), and hermes-plugin integration
  (`.github/workflows/ci.yaml:254`). The `llm-tests` job is `if: false`, and
  `.pre-commit-config.yaml:51` runs pytest only as a collect-only badge count.
- **P5. Vault returns an opaque access token and a signed JWT id_token, and
  puts `groups` behind a scope template.** UNCERTAIN - external, not
  reproducible here, since there is no Vault instance in this environment.
  source: issue #277, whose author ran it against Vault 2.0.3
  (https://github.com/JasperHG90/memex/issues/277). Two consequences the design
  must not depend on being right: the credential switch is provider-agnostic
  and T12 pins verification with a synthetic token, so P5 being wrong costs
  nothing there. But the `groups` half has a client-side consequence worth
  stating in the doc: if a Vault operator exposes `groups` through a scope
  template, they must also add that scope to `oidc.scopes`
  (`packages/common/src/memex_common/config.py:1625-1628`), or the token
  verifies and authorization still fails through path 3 of section 4. R12's
  third log line is what makes that diagnosable rather than silent.
- **P6. The token cache is disposable local state, so new fields need no
  migration.** VERIFIED - `load_token_cache`
  (`packages/common/src/memex_common/auth_client.py:64-75`) returns `None` and
  warns on a malformed file; `save_token_cache`
  (`packages/common/src/memex_common/auth_client.py:78-91`) truncates on write;
  `TokenCache` sets no `model_config`, so pydantic's default `extra='ignore'`
  applies and both new defaulted fields load from an old file (pinned by T9).
- **P7. The four pre-existing failures in the newly-gated suites are
  environment-dependent, not real defects.** VERIFIED by measurement, per-suite
  baseline quoted in section 8. probe: `uv run pytest <suite>` for each of the
  three suites separately (read-only). Three read the developer's real user
  config (`assert 'https://memex.lab.orangecluster.nl' ==
  'http://127.0.0.1:8000'`) and one binds a port already in use locally. A
  fifth failure appears only when the three suites share one pytest process and
  passes in isolation, so it is an artifact of the combined command and not
  something CI can hit.
