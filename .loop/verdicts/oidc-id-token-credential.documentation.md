---
verdict: pass
tree: 1f04b694bfab3c70307a7d25fdc1d1784ca32352
---

# Documentation review: oidc-id-token-credential

## Provenance (read this first)

Transcribed by the implementer, not written by the reviewer. The configured
`loop-implementation-doc-reviewer` agent was dispatched once, ran for over an
hour re-running full test suites without converging, did not answer two direct
status messages, and was stopped. The review was re-run with the
`adversarial-reviewer` sub-agent under a documentation-only brief, which is the
alternative `.claude/rules/adversarial-reviews.md` sanctions. That agent is
read-only, so it cannot write here; its verdicts are quoted, not paraphrased.

Two rounds ran:

| Round | Tree | Verdict |
|---|---|---|
| 1 | `bbe0b48c` | PASS WITH REQUIRED FIXES (2 major, 4 minor, 2 nits) |
| 2 (confirmation) | `dd72e7b7` | PASS (3 nits, none blocking) |

Delta between round 2's tree and this verdict's tree (`1f04b694`) is two
one-line prose edits, both nits round 2 itself raised as optional; they are
listed verbatim in the adversarial verdict alongside this file. Round 2's third
nit (two prose semicolon splices at `docs/how-to/configuring-server/oidc.md:113`
and `:132`) was deliberately NOT applied, because
`.claude/rules/slop-scan-for-docs.md` classifies that category low-confidence
and says to surface it for a human rather than auto-rewrite. Operator decision.

## Verdict

**PASS.**

## Round 1 required fixes (both majors, all minors resolved)

**MAJOR 1 - the reframed sentence was factually wrong about Google, and
contradicted a comment added in the same diff.** The docs said "a provider whose
tokens are all opaque (Google, GitHub) cannot be verified on this path at all",
while the new code comment said Google "sign[s] only the id_token". Both cannot
be true. The reviewer had it right: Google Identity issues an opaque `ya29.*`
access token AND an RS256-signed id_token whose `aud` is the client id, so
Google is exactly the shape this feature now supports; a GitHub OAuth app issues
no id_token at all and is the genuinely shut-out case.

This mattered more than a vendor-name slip: it reproduced, for a different
vendor, the exact misdirection issue #277 was filed about, inside the sentence
written to remove it. A Google-backed self-hoster would have been told to fall
back to API keys when `credential: id_token` would have worked.

FIX: Google moved to the signs-the-id_token group and GitHub kept as the
shut-out example, in all four surfaces that carry the split:
`docs/how-to/configuring-server/oidc.md:5`,
`docs/explanation/how-memex-works/oidc-authentication.md:55`, the code comment
in `packages/core/src/memex_core/server/oidc.py`, and (after round 2 caught the
fourth) the `OidcProviderConfig` docstring in
`packages/common/src/memex_common/config.py`.

**MAJOR 2 - three `<code-ref>` line anchors pointed at the wrong code, two
broken by this diff.** The change shifted `config.py` by +45 lines and
`server/oidc.py` by ~+41 inside `verify()`. The `:22` anchor landed mid-validator
instead of `class AuthConfig`; `:40` started mid-`__init__` and excluded both
the provider selection and the `claims_options` block it names; the `:275`
anchor had been hand-edited but recomputed wrong, stopping one line short of the
thing it named. A fourth (`:79`) was already stale at HEAD and drifted further.

FIX: all four re-resolved and verified by round 2 against the current files:
`config.py 1552-1585` (AuthConfig through its `oidc` field), `oidc.py 191-256`
(issuer selection through decode/validate), `config.py 2970-2977`
(`MemexConfig.oidc`), `oidc.py 166-206` (the first two refusal paths).

**Minor - the client YAML silently dropped `offline_access`.** `scopes` uses a
`default_factory`, so setting it replaces the default list wholesale; without
`offline_access` no refresh token is issued and the session degrades at the
first expiry, contradicting the same page's promise 11 lines above that clients
"refresh the token as it nears expiry".
FIX: `offline_access` added to the example, plus prose stating that setting
`scopes` replaces rather than extends the default, and what dropping it costs.

**Minor - the server snippet omitted `enabled: true`** while starting at the
`server:` root, so copy-pasting it over the canonical block would yield a server
with authentication switched off entirely: silently open, not merely broken.
FIX: `enabled: true` added.

**Minor - no instruction to re-run `memex auth login` after flipping
`credential`**, which is the most likely first-run stumble for the target user
(they have already logged in under `access_token`).
FIX: a paragraph added to the new section.

**Minor - "Restart and confirm" told operators to watch for a startup line the
server logs at INFO**, invisible at the default WARNING level. Pre-existing, but
newly self-contradicting once this change taught the reader that WARNING is the
visibility line.
FIX: the doc now says that line is at INFO and needs `server.logging.level:
INFO`, while refusal reasons show up either way.

**Nit - the troubleshooting table omitted the sixth refusal message**
(`OIDC token verification error`), despite the intro promising each reason names
itself. FIX: row added.

**Nit - four `X, not Y` constructions** in added prose. Reduced to one
load-bearing instance ("a requirement about the token, not about the vendor",
which is the thesis of the reframe).

## Round 2 confirmation, verbatim

- Anchors: "all four resolve correctly", each range checked against current
  files; the untouched `:44` anchor still resolves.
- YAML: "Loaded every ```yaml block in the how-to through
  `MemexConfig.model_validate`: 9/10 OK" - the only failure a pre-existing bare
  `grant_rules:` fragment this change never touched. Both new blocks validate.
- Log-string fidelity: "all six table row strings match real `logger.warning`
  format strings verbatim", and the default-level claim traced end to end
  through `LoggingConfig.level` -> `configure_logging` -> the `memex` logger.
- Scope-replacement prose verified accurate against the `default_factory`, and
  the `openid` enforcement confirmed real at `config.py:1727-1731`.

## Round 1 gates the reviewer verified itself

Config field accuracy (type, default, both validators) matched exactly; every
quoted log string exists in the source; the "visible at the default log level"
claim traced through `configure_logging`; the "No log line at all" paragraph
verified against `server/auth.py`; the security section verified against the
code (grep for `azp` and scope checks returns nothing, so the replay claim is
honest); the rollback claim verified (server `audience` survives removing the
client field); all five stale-claim sites updated with no sixth found; slop scan
on added lines clean on every mechanical category (0 em dashes, 0 ` -- `, 0
tier-1 slop, 0 smart quotes, 0 British spellings).

## Acceptance

Eval row 11 (docs, model-with-rubric) met: requirements (a) through (e) all
present, including the two R13 security facts (an id_token accepted as a bearer
is replayable; the client id must not be shared with another relying party) and
the two-sided rollback note.
