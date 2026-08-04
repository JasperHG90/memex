---
verdict: pass
tree: 1f04b694bfab3c70307a7d25fdc1d1784ca32352
---

# Adversarial review: oidc-id-token-credential

## Provenance (read this first)

This file is a TRANSCRIPTION by the implementer, not a file the reviewer wrote.
The configured `loop-implementation-reviewer` agent was dispatched three times
in this session and never produced a verdict file (twice it looped re-running
the full test suites for over an hour without converging; once it was stopped
after a direct instruction to write the file went unanswered). The review was
therefore delegated to the `adversarial-reviewer` sub-agent, which the repo's
own `.claude/rules/adversarial-reviews.md` sanctions as the alternative
("a specialized agent if available, otherwise a general-purpose sub-agent
briefed to review skeptically"). That agent is READ-ONLY by construction, so it
cannot write here; its findings are transcribed below and its verdicts are
quoted, not paraphrased into a pass.

Three independent review rounds ran, each on a different tree:

| Round | Tree | Verdict | Findings |
|---|---|---|---|
| 1 | `51eaae2d` | ACCEPT WITH CHANGES | 6 (1 major) |
| 2 (post-fix) | `2e432595` | PASS | 4 minor residues |
| 3 (final confirmation) | `dd72e7b7` | PASS | 3 nits, all optional |

Delta between round 3's tree and this verdict's tree (`1f04b694`): exactly two
one-line prose edits, both of them nits round 3 itself raised and marked
optional. Nothing else changed, and no test or gate result moved. Verbatim:

1. `packages/common/src/memex_common/config.py` `OidcProviderConfig` docstring:
   Google was still named on the "keep using API keys" side, the one surface of
   four the earlier fix missed. Now grouped with Vault as a provider that signs
   the id_token, with a GitHub OAuth app as the shut-out example.
2. `docs/how-to/configuring-server/oidc.md`: "the session stops working at the
   first expiry" overstated the consequence of dropping `offline_access`; the
   client actually falls back to the API key when one is configured. Reworded.

Round 3's third nit (two prose semicolon splices) was deliberately NOT applied:
`.claude/rules/slop-scan-for-docs.md` classifies that category as low-confidence
and says to surface it for a human rather than auto-rewrite. Flagged here for
the operator.

## Verdict

**PASS.**

Round 3, verbatim: "all six confirmation items (A-F) landed correctly on tree
`dd72e7b7`; three nits, none blocking. Confidence: 93%."

## Round 1 findings and their fixes (all six resolved, confirmed in rounds 2-3)

1. **MAJOR - the three new diagnostics never appeared on a default server.**
   They were `logger.info`, but `LoggingConfig.level` defaults to `WARNING`
   (`packages/common/src/memex_common/config.py:1796`), so eval row 9 ("tell the
   three 403 causes apart from the server log alone") was not met: the tests
   passed only because each one forced `caplog.at_level('INFO', ...)`. The
   reviewer's scenario was exact: the issue-#277 reporter follows the new
   troubleshooting table, greps the log, finds nothing, and is where they
   started.
   FIX: every reason a bearer is REFUSED now logs at WARNING, including the
   pre-existing `OIDC token rejected for issuer` line (raised for parity) and,
   after round 2, the JWKS-fetch failure (which refuses every bearer).
   Pinned by `test_all_rejection_reasons_survive_the_default_log_level` and
   `test_jwks_fetch_failure_is_visible_at_the_default_log_level`, both of which
   capture at WARNING so an INFO regression fails them.
   Round 3 confirmed the level classification comment is now exactly true:
   "WARNING at 182/200/214/251/255/265 (all refusals), INFO only at 234
   (forced-refetch fallback, non-refusing) and 345 (startup)."

2. **Doc anchors did not match the heading slug.** `#providers-that-sign-the-id-token`
   vs the real `#providers-that-sign-the-id_token` (underscore is `\w`, so
   slugifiers preserve it; repo precedent `docs/reference/mcp-tools.md:280`).
   FIX: both links corrected. Confirmed in rounds 2 and 3.

3. **Unsupported "and so does Keycloak in some configurations" claim**, which
   also contradicted the same sentence listing Keycloak as an access-token
   signer. FIX: clause deleted.

4. **The credential-mismatch fall-back was silent.** Flipping `credential`
   without re-running `memex auth login` silently discarded the cache and, with
   no API key configured, sent no auth header at all.
   FIX: logs a WARNING naming both credentials and pointing at `memex auth
   login`. Round 2 verified the if/else restructure is "an exact De Morgan of
   the original" with no behavior change for the no-cache, wrong-issuer, or
   wrong-mode cases.

5. **The id_token was persisted even under `credential='access_token'`**,
   writing a second live credential to disk that no code path reads, which
   contradicted the ticket's own "byte-identical behavior" claim.
   FIX: persisted only when it is the credential being sent. Eval row 2 was
   amended with a dated, visible note; round 2 judged the amendment "honest...
   strictly stronger".

6. **The test-isolation fix was symptom-scoped**: one more bare `MemexConfig()`
   remained, and `MEMEX_CONFIG_PATH` was not cleared even though it
   short-circuits the local hatch.
   FIX: replaced with an autouse module fixture setting both hatches and
   deleting `MEMEX_CONFIG_PATH`. Round 2 verified it covers all 20 module-level
   tests and is load-bearing against this machine's real user config.

## Round 2 residues and their fixes (all four resolved)

- Troubleshooting still said "Read the server log at `INFO` first" after the
  level change. Reworded to say the reasons are at WARNING and visible by
  default.
- The comment claiming "every reason a bearer is REFUSED logs at WARNING" was
  false for the JWKS-fetch path, which also 403s every bearer. That call site
  was raised to WARNING, a troubleshooting row added, and the comment narrowed
  to name what actually stays at INFO.
- The plan still prescribed `logger.info`. An `## Amendments after review`
  section was added, matching the eval's amendment style.
- `.loop/*` harness state should not ride in the feature commit. Committed
  separately (see the two commits on this branch).

## Round 3 confirmation, verbatim highlights

- Anchors: "all four resolve correctly", with each `<code-ref>` range checked
  against the current files.
- YAML: "Loaded every ```yaml block in the how-to through
  `MemexConfig.model_validate`: 9/10 OK", the one failure being a pre-existing
  bare `grant_rules:` fragment this change never touched.
- No regression: no silent-fallback hole (`bearer_token` has no
  `or self.access_token`; the `model_validator` blocks the bad construction;
  refresh failure returns `None`, never the access token); `min(now +
  expires_in, id_exp)` pinned in both orderings; `repr(iss)[:N]` ordering
  intact with newline, ANSI, and 8KB-flood tests.
- "The tests are load-bearing rather than decorative: the WARNING-level tests
  would fail if the level were reverted to INFO, the expiry tests pin `min()`
  in both orderings (so neither one-sided bug passes), and
  `test_refresh_dropping_the_id_token_falls_back` pins the exact silent-
  substitution hole the feature exists to close."

## Gates (independently re-run by the reviewer where the constraint allowed)

Round 3 ran, itself, on the reviewed tree: the 5-file targeted OIDC suite
(156 passed), `ruff check` (all passed), `ruff format --check` (10 files already
formatted), the pinned `mypy` prek hook on both src and test file sets (Passed
twice), and `check-yaml` on the new CI steps (Passed).

Full-suite counts below were run by the implementer and were NOT independently
re-run in rounds 2-3 (the reviewers were explicitly constrained away from them
after round 1 spent over an hour re-running suites without converging). This is
the one place this verdict rests on author-reported evidence:

- `just test` -> 56 passed, 208 deselected
- `just prek` -> every hook Passed (ruff, ruff-format, mypy strict)
- `packages/common/tests` -> 612 passed, 4 skipped (638 after the final nit run)
- `packages/cli/tests` -> 581 passed
- `packages/core/tests/unit` -> 3608 passed, 14 skipped
- `loopctl verify` -> ok, stamp bound to this tree

## Acceptance

Eval marker rows 1-12 met. Row 9 ("tell the three 403 causes apart from the
server log alone") was the one genuinely NOT met before round 1's major finding
was fixed, and is the reason the log level changed.
