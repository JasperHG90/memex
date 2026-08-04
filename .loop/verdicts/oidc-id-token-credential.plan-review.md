---
verdict: pass
plan: 4d65730db998e73f607dc709a5acf4e03e2974c2c386020d16bf17090717f7dc
---

# Plan review: oidc-id-token-credential (re-review, revision 3)

## Deterministic floor

`loopctl verify-plan oidc-id-token-credential` -> `valid`, seven warnings, no
hard fail. The seven are the same branch-precision citations I resolved by hand
in the prior two passes (`verify` at `oidc.py:157-159`, `:161-164`, `:216`;
`authenticate_request` at `auth.py:134-136`; `_loopback_login` `auth.py:244`,
`_device_login` `:284`, `login` `:88`). Each cites a branch inside a function
rather than the function's `def` line, and each resolves to what the plan
claims. Plan fingerprint on disk matches the briefing:
`4d65730db998e73f607dc709a5acf4e03e2974c2c386020d16bf17090717f7dc`.

## Premise verdict

**SOUND.** Both required fixes are resolved with real edits that survive
independent measurement, the two unprompted additions are accurate, and the
one thing I asked for that was NOT applied turns out, on fresh evidence
gathered this pass, to be a recommendation rather than a requirement.

## Prior required fixes: resolved or not

**Fix 1 (bound and escape the attacker-controlled issuer). RESOLVED in
substance, with one clause not applied.**

R6 is now "Never log token material, and never log an unverified value raw"
(plan `:165-178`). Its second half names the exact provenance (an UNVERIFIED,
pre-auth, attacker-controlled payload), states the mandate ("MUST be truncated
to a bounded length and `%r`-quoted"), and gives the reason (a newline or
terminal escape cannot forge or corrupt a log line). I re-checked its supporting
claim exhaustively rather than accepting it: `grep -n "logger\."
packages/core/src/memex_core/server/oidc.py` returns exactly five statements,
`:172`, `:192`, `:209`, `:213` (all printing the CONFIGURED `provider.issuer`
plus an authlib exception) and `:289` (a startup line with a provider count).
R6's "this would be the first attacker-controlled value in a memex auth log" is
therefore exact, with no sixth log statement to contradict it.

The author bound it with a dedicated test row instead of amending T11, which is
the better of the two: T11c at plan `:391` pins "an over-long `iss` carrying a
newline is truncated and quoted in the log, so it cannot forge a log line",
homed in `packages/core/tests/unit/test_server_oidc.py`. I checked it is
buildable rather than assuming: `test_unknown_issuer_rejected_without_network`
(`packages/core/tests/unit/test_server_oidc.py:123-129`) already drives exactly
this branch with `_mint(key, _valid_claims(iss='https://evil.example'))` under
`@respx.mock` with NO routes mocked, because the unknown-issuer branch at
`packages/core/src/memex_core/server/oidc.py:163-164` returns before any JWKS
fetch. Swapping the `iss` for a 500-char string containing `\n` is a one-line
change to a proven helper. The row is deterministic, offline, and cheap.

**The clause not applied:** I also asked that section 9 stop calling all three
new lines equivalent to the existing rejection log at `oidc.py:209`, on the
ground that path 1's line fires on a single-byte bearer while `:209` requires a
well-formed JWT bearing a configured issuer. Section 9's last bullet is
unchanged. I re-attacked my own point this pass instead of restating it, and
the evidence weakens it: `audit_access_log`
(`packages/core/src/memex_core/server/__init__.py:310-336`) already writes one
audit record for EVERY non-skipped request, including a 403'd one, so a
bad-bearer flood already costs one log record per request today and the new INFO
line is a constant factor on an existing per-request write, not a new
amplification channel. The dangerous half of my original finding was the
attacker-controlled CONTENT, and that is now carried by R6, the binding
section, which is stronger placement than the risk narrative. I record the
residual as a recommendation below rather than holding the plan for it.

**Fix 2 (section 8's account of the three config failures). RESOLVED and now
ACCURATE.** Verified against the source, not the prose:

- `test_config_server_url_default`
  (`packages/common/tests/test_config_structure.py:42-46`): `patch.dict(
  os.environ, {}, clear=False)`, neither hatch.
- `test_config_server_url_derived_from_custom_host_port` (`:54-58`): same,
  neither hatch.
- `test_default_model_override_propagates` (`:76-82`):
  `patch.dict(os.environ, {'MEMEX_LOAD_LOCAL_CONFIG': 'false'})`, local hatch
  only.

The plan's "Exactly ONE of the three (`test_default_model_override_propagates`)
already sets the local hatch and misses the global one; the other two set
neither" is now correct. And the failure split is correct, measured today:

```
E   AssertionError: assert 'https://meme...ngecluster.nl' == 'http://127.0.0.1:8000'
E   AssertionError: assert 'https://meme...ngecluster.nl' == 'http://10.0.0.1:9000'
E   AssertionError: assert 'gemini/gemin...flash-preview' == 'custom/model'
```

Two on `server_url`, one on the model, with the model message quoted verbatim in
the plan. The plan's parenthetical exemplar for the server_url pair shows the
first one's expected side only; both share the same leaked left-hand value and
the same one-line fix, so an implementer is not misled. That is a finer grain
than what I required and I do not hold the plan for it.

## Per assumption

### P1. The client sends the access token and never the id_token. HOLDS

`packages/common/src/memex_common/auth_client.py:220`, `:228`, `:234` all
return `cache.access_token`; `TokenCache` (`:40-53`) has no id_token field;
`token_cache_from_response` (`:127`) reads only `data['access_token']`.

### P2. A Vault-shaped id_token verifies against existing server code. HOLDS

Proved by probe in the prior pass (synthetic RS256 tokens with scalar and list
`aud` both produced a full `AuthContext`). The mechanism is unchanged on disk:
`verify` selects on `iss` (`packages/core/src/memex_core/server/oidc.py:161-164`)
and validates only `iss`/`aud`/`exp` (`:198-202`).

### P3. All three 403 paths are silent today. HOLDS

Re-confirmed structurally by the exhaustive logger grep above: the three `return
None` sites (`packages/core/src/memex_core/server/oidc.py:159`, `:164`, and
`:216` via `_claims_to_context`'s `:96-97`) have no log statement between them
and the return.

### P4. `packages/common` and `packages/cli` run in no CI job. HOLDS

Unchanged from the prior pass (`.github/workflows/ci.yaml:86-127`).

### P5. Vault's token shapes and its `groups` scope template. UNCERTAIN

Correctly self-labeled and correctly fenced. No Vault instance here.

### P6. The token cache is disposable, no migration. HOLDS

`load_token_cache` (`packages/common/src/memex_common/auth_client.py:64-75`)
warns and returns `None` on a malformed file; `save_token_cache` opens with
`O_TRUNC` (`:85`); `TokenCache` sets no `model_config`, so pydantic's default
`extra='ignore'` loads an old file.

### P7. The four pre-existing failures are environment-dependent. HOLDS

Re-measured this pass, and the plan's quoted baseline reproduces exactly:

```
packages/common/tests   3 failed, 586 passed, 4 skipped   (plan: same)
packages/cli/tests      1 failed, 578 passed              (plan: same)
```

`packages/cli/tests/test_process.py:60-61` still hardcodes port 59999 and its
sibling at `:63-72` still models the `bind(('127.0.0.1', 0))` fix the plan
prescribes.

### P8 (new material). The skew note in section 9 is accurate. HOLDS

The unprompted accepted-edge note (plan `:421-425`) is right on every count I
could check:

- Born non-fresh: `is_fresh` is `time.time() < (self.expires_at - skew)` with
  `skew` defaulting to `_EXPIRY_SKEW_SECONDS`
  (`packages/common/src/memex_common/auth_client.py:55-56`), so a TTL under 60s
  is never fresh.
- "Every request refreshes": `_resolve_bearer` falls through `:219` to the lock
  at `:224`, re-checks at `:227`, and calls `_refresh_token` at `:230`, which is
  a discovery GET (`:141`) plus a token POST (`:155`), neither cached.
- "Degrades throughput rather than breaking correctness": `:234` returns the
  bearer from the refreshed cache unconditionally, so the request still
  succeeds.
- "Same behavior today for an equally short `expires_in`": today
  `expires_at = time.time() + expires_in` (`:129`), so an `expires_in` under 60
  is likewise born non-fresh. The parity claim is true, which is what makes
  "not gated" a defensible accept rather than a deferral.

This is the right response to my prior recommendation: the author chose to
accept the edge explicitly with a parity argument instead of adding a guard,
and the argument checks out.

Anchor nit: the note cites `packages/common/src/memex_common/auth_client.py:32`
for the 60s skew; `:32` is the explanatory comment and the constant
`_EXPIRY_SKEW_SECONDS = 60.0` is at `:33`. One line off, lands on the comment
that defines the concept.

### P9 (implicit, new material). R6's truncate-and-quote is implementable without a new failure mode. UNCERTAIN

The only thing in the new material I could not settle. The ORDER of the two
operations R6 mandates is not specified, and it matters:

- `repr(iss)[:200]` is total over every JSON type.
- `repr(iss[:200])` raises `TypeError` when `iss` is absent or non-string.

That second spelling is reachable pre-auth: `payload.get('iss')` returns `None`
for a JWT with no `iss` claim, and the module's own guard
(`packages/core/src/memex_core/server/oidc.py:162`,
`provider = self._providers.get(issuer) if isinstance(issuer, str) else None`)
routes a non-string `iss` into the very branch the new log line sits in. An
exception there is not a 403: `authenticate_request` does not wrap `verify`
(`packages/core/src/memex_core/server/auth.py:107-136`), and both that
function's docstring ("This function never raises HTTP errors ... would ...
surface as a 500") and the in-module comment at `oidc.py:166-168` say a raise
out of `verify` becomes a 500.

I record this as UNCERTAIN and recommended, not required, for three reasons:
`%r`-quoted names `repr`, whose natural spelling is the total one; the module
already models the `isinstance` convention at `:162`; and the plan states
nothing false about it. One clause in R6 closes it for good.

### P10 (implicit). Section 9's diagnostics bullet. HOLDS as written, imprecise at the margin

"They are INFO, matching the existing rejection log at `oidc.py:209`" is true of
the LEVEL (`:209` is `logger.info`). "Carry no token material and no claim
values" is loose for path 2, which logs the `iss` claim value, though R6 lists
"issuer" as its own permitted category alongside claim NAMES, so the binding
requirement is self-consistent. Recommended cleanup, below.

## Most dangerous assumption

**P5, the Vault half.** Unchanged and still correctly defused: it is labeled
UNCERTAIN rather than asserted, T12 pins the verification path with a synthetic
token so the design does not rest on it, and R12's third diagnostic converts the
residual `groups` failure from a silent 403 into a named log line. Nothing in
this revision moved that risk.

## Required fixes

None. Both prior required fixes are resolved, the two unprompted additions
verify clean, and the single clause I asked for that was not applied is
downgraded on fresh evidence (the per-request `audit_access_log` at
`packages/core/src/memex_core/server/__init__.py:310-336`), not waived.

## Recommended, not required

1. **Pin the repr-then-truncate order in R6** (P9): "computed as
   `repr(iss)[:N]`, never `repr(iss[:N])`, so an absent or non-string `iss`
   cannot raise out of `verify` into a 500". Evidence: `payload.get('iss')` can
   be `None`, `oidc.py:162` routes a non-string `iss` into the same branch, and
   `oidc.py:166-168` plus `auth.py:107-120` establish that a raise out of
   `verify` is a 500 rather than a 403. Optionally extend T11c's row to a
   missing-`iss` bearer.
2. **Section 9's diagnostics bullet** (P10): say the path-2 line carries the
   issuer, truncated and quoted per R6, rather than "no claim values", and drop
   the blanket equivalence with `oidc.py:209` (path 1 fires on any non-JWT
   bearer, `:209` needs a well-formed JWT bearing a configured issuer). Both are
   prose accuracy in the risk narrative; R6 and T11c already bind the behavior.
3. **Anchor nit** (P8): the 60s skew constant is
   `packages/common/src/memex_common/auth_client.py:33`, not `:32`. Same for
   R4's `:122`, which reads `expires_in`; the assignment that feeds `is_fresh`
   is `:129`.
4. The three items carried over from the prior pass and still open, all
   cosmetic or operational: `MEMEX_LOAD_LOCAL_CONFIG` does not disable
   `MEMEX_CONFIG_PATH` (`packages/common/src/memex_common/config.py:88-101`);
   R5's refresh-drops-id_token consequence as a user-visible outcome;
   `docs/reference/configuration-options.md:109` as the home for the two new
   grant-validation rules; and naming the `markers:` input for the two new CI
   steps (`.github/workflows/ci.yaml:96-127`).

## Contract hygiene

- **Code surface with resolved anchors: clean.** Every cited `path:line` I
  opened this pass resolves to what the plan claims. The two anchor nits above
  are one-line-off citations to an adjacent comment or read, not wrong targets.
- **Gates discovered, not assumed: clean.** `just test` and `just prek` match
  `.loop/config.json`, the gate gap is stated, the run-them-separately
  instruction is justified by measurement, and the per-suite baseline reproduces
  exactly today.
- **Non-goals explicit: clean.** Eight, including the two fences that do real
  work (no general test-isolation sweep, no `just test` widening).
- **Tests homed: clean.** All 19 rows carry a home; all six home files exist on
  disk, and I confirmed the three server-diagnostic rows attach to existing test
  bodies (`test_server_oidc.py:123`, `:187`, `:237`).
- **Forks surfaced: clean.** Q1, Q2, Q3 each carry a recommendation.
