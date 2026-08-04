eval: oidc-id-token-credential

**Definition of Done:** a self-hoster whose OIDC provider signs the id_token
and issues an opaque access token (HashiCorp Vault) can complete `memex auth
login` and reach an authenticated endpoint by setting one opt-in field, every
existing deployment behaves byte-identically, no path can quietly substitute
the wrong token, and each of the three ways a bearer becomes a `403` leaves a
distinguishable line in the server log.

Scoring policy: every row is a deterministic assertion at a hard 100% bar,
except row 11 (docs), which is a model-with-rubric judgment because prose
quality is not machine-checkable. Rows 4 through 8 are the load-bearing
guardrails: each one, if it holds only partially, silently reproduces the exact
`403` from issue #277 on the path where the operator believes it is fixed.

These rows sit ABOVE the ticket's unit tests. Where a row names a mechanism it
is because the mechanism IS the observable outcome (a log line, a config
refusal), not to restate a `test_*` in code terms.

Fork-dependent notes:
- Ticket Q1 (no server-side "issues id_tokens" marker) and Q3 (no `azp`
  enforcement) are settled as "document, do not implement". Row 9 pins the
  documented consequence rather than a code check. Re-pin if either is
  reopened.
- Row 6's bar follows plan R4 (`min(now + expires_in, id_token exp)`). If the
  operator later prefers the id_token's `exp` alone, this row must be rewritten,
  not relaxed.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| A Vault-style provider can authenticate a human end to end | Client config `oidc: {issuer, client_id: 'memex-cli', grant: interactive, credential: id_token, scopes: [openid, ...]}`; a token response carrying an opaque `access_token` (`hvb.…`, not a JWT) and a signed RS256 `id_token` whose `iss` is the provider and `aud` is `memex-cli`; server provider configured with `audience: ['memex-cli']` and a matching grant rule | The cached credential's bearer is the id_token, the request carries `Authorization: Bearer <id_token>`, and `OidcVerifier.verify` returns an `AuthContext` with the granted policy. The opaque access token is never sent | Deterministic (unit test) | 100% |
| An existing deployment is unaffected by the feature existing | A client config with no `credential` key at all, and a token response with both an `access_token` and an `id_token` | The bearer sent is the `access_token`, exactly as before the change, and the id_token is not written to the token cache at all | Deterministic (unit test) | 100% |
| A config that cannot possibly work is refused at load, not at runtime | `credential: id_token` paired with (a) `grant: client_credentials`, (b) `grant: jwt_profile`, (c) `grant: token_file`, (d) `grant: token_env`, and (e) `grant: interactive` with `scopes` lacking `openid` | Each raises `ValueError` at config construction naming the offending combination. No case reaches a network call or a `403` | Deterministic (unit test) | 100% (all 5 cases) |
| **[GUARDRAIL — no silent substitution]** A missing id_token never degrades into sending the access token | A token response with `access_token` present and `id_token` absent, under `credential: id_token` — at first login AND at refresh | Login: raises, and `memex auth login` exits non-zero with a message, not a traceback. Refresh: warns and the bearer resolves to `None` (falling back to the API key), and the access token is NOT sent as a bearer | Deterministic (unit test) | 100% (both paths) |
| **[GUARDRAIL — no silent substitution]** A `TokenCache` cannot be constructed in a state where the wrong token would be sent | `TokenCache(credential='id_token', access_token='…', id_token=None)` | Rejected at construction. There is no reachable code path where the bearer property returns the access token while the credential says id_token | Deterministic (unit test) | 100% |
| **[GUARDRAIL — stale cache]** Flipping the credential setting never reuses the old cache | A fresh cached token minted under `credential: access_token` while config now says `id_token`, and the mirror case (cache minted under `id_token`, config now `access_token`) | Both fall back (no bearer returned, API key used) rather than sending the token the config did not ask for | Deterministic (unit test) | 100% (both directions) |
| **[GUARDRAIL — expiry]** The client never sends an expired token and never stretches its refresh interval | Two token responses under `credential: id_token`: (a) `expires_in` 3600 with an id_token `exp` at now+300, (b) `expires_in` 300 with an id_token `exp` at now+3600 | `expires_at` is the earlier of the two in BOTH cases (`min` semantics): now+300 in (a), now+300 in (b) | Deterministic (unit test) | 100% (both orderings) |
| **[GUARDRAIL — expiry]** An unreadable id_token expiry fails loudly instead of guessing | Under `credential: id_token`, an id_token with no `exp` claim, and one whose payload is not decodable | Raises. Does NOT fall back to `expires_in`, which would reintroduce the expired-token failure | Deterministic (unit test) | 100% (both cases) |
| An operator can tell the three `403` causes apart from the server log alone | Three requests to a protected endpoint: (a) an opaque bearer (`hvb.…`), (b) a well-formed JWT whose `iss` matches no configured provider, (c) a fully valid token that matches no grant rule on a provider with no `default_policy` | Each emits a log record, and the three records are distinguishable from each other and from the existing bad-`aud` message. (a) names the token's non-JWT shape, (b) names the unmatched issuer, (c) names the issuer and the claim NAMES present | Deterministic (unit test asserting on captured log records) | 100% (all 3 emit, all 3 distinguishable) |
| **[GUARDRAIL — log safety]** The new pre-auth diagnostics leak nothing and cannot forge a log line | (a) an opaque bearer whose body is a recognizable secret string, (b) a valid token whose `groups`/`email` values are recognizable strings, (c) a token whose unverified `iss` is 4KB long and contains `\n` plus an ANSI escape | No emitted record contains the token, any claim VALUE, or an un-quoted newline; the oversized `iss` is truncated and quoted so it occupies one bounded log line | Deterministic (unit test asserting on captured log records) | 100% (all 3 cases) |
| A reader arriving from issue #277 gets a correct answer, not the old misdirection | The OIDC how-to and explanation pages after the change | The pages (a) attribute the limitation to the token TYPE rather than to a vendor list, (b) name HashiCorp Vault explicitly and say its id_token is the signed one, (c) show the `credential: id_token` config with the matching server `audience: ['<client-id>']`, (d) state that such a token is replayable by any party receiving it and that the client id must not be shared with another relying party, and (e) state that rollback also requires narrowing the server `audience` | Model with rubric, 1-5 per criterion, judged against the ticket's R13 | 5/5 on (d) and (e); 4/5 or better on (a) to (c) |
| **[GUARDRAIL — no regression]** The change does not break the suites it newly puts under CI | `uv run pytest packages/common/tests`, then `packages/cli/tests`, then `packages/core/tests/unit`, each as its own invocation; plus `just test` and `just prek` | All five green, with the four pre-existing environment-dependent failures in `common`/`cli` fixed rather than skipped or xfailed | Deterministic (CI + local run) | 100% |

Post-review amendment (2026-08-04, after the adversarial pass): row 2 originally
read "the id_token is stored but never sent". The reviewer pointed out that
persisting it under the access_token credential writes a second live credential
to disk that no code path reads, contradicting this ticket's own "byte-identical
behavior" claim. The row now requires that it not be cached at all. This tightens
the bar; it does not relax it.

signed-off-by: claude-opus-5, under Jasper Ginn's explicit delegation this session ("create a loop ticket for it (+ evals and signoff -- i'm empowering you to do that)"; operator asleep, autonomous run) 2026-08-04
