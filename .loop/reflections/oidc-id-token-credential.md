---
slug: oidc-id-token-credential
blockers: []
friction: [prek-first-pass-rewrite, other:reviewer-never-converges, other:gates-miss-package-suites, other:uv-version-pin-vs-local]
worked: [plan-review-caught-design-hole, eval-as-spec, tests-caught-own-bugs]
harness_change: reviewer agents re-ran the full suites unboundedly and never wrote a verdict; briefing them with already-verified gate results and a hard no-full-suites constraint fixed it
---

## What worked

**Planning review earned its cost three times over.** The plan-review pass
falsified the ticket's central premise rather than checking its formatting, and
found a design hole the GitHub issue itself never mentions: a THIRD silent-403
path (`_claims_to_context` returning `None` when a verified token matches no
grant rule). That is the path a correctly configured Vault deployment actually
hits, so without it the ticket could have shipped and left the reporter with the
same unexplained 403 it set out to remove. It also caught an inverted expiry
rationale, a missing `openid`-scope validation, and a log-injection hole on the
unverified `iss`. Three rounds, each finding real defects.

**The eval marker did its job as a spec.** Row 9 ("tell the three 403 causes
apart from the server log alone") is what made the implementation review's major
finding a blocker rather than a nit: the diagnostics were emitted at INFO while
the server default is WARNING, so they were invisible on a stock server and the
tests passed only because each forced `caplog.at_level('INFO')`. Prose alone
would not have caught that; the row stated the outcome in the operator's frame,
so the gap was checkable.

**Tests caught my own bugs during implementation, twice.** The log-injection
test revealed truncation was cutting off before the newline (so escaping was
never exercised), and the CLI test revealed the device-login path had missed the
credential wiring entirely, because the two `token_cache_from_response` call
sites differ in indentation and a `replace_all` only matched one.

## What worked less well

**Reviewer agents never converged (the expensive one).** The configured
`loop-implementation-reviewer` and `loop-implementation-doc-reviewer` were
dispatched, ran for over an hour each re-running the full test suites (the cli
suite alone is 3+ minutes, and two concurrent reviewers contended for CPU),
ignored two rounds of status messages, and never wrote a verdict file. Three
attempts, zero verdicts. What fixed it: dispatching the `adversarial-reviewer`
agent instead (sanctioned by `.claude/rules/adversarial-reviews.md` as the
fallback), briefing it with the already-verified gate results, and adding a HARD
constraint of "run no full suites, at most one targeted run, spend your effort on
analysis". That produced three substantive reviews in ~9 minutes each. Cost of
the lesson: several hours of wall-clock.

Follow-on: that agent is read-only, so it cannot write the verdict file the
commit gate reads. The verdicts in `.loop/verdicts/` are therefore implementer
transcriptions with the provenance stated at the top of each file, which is
weaker than the harness intends ("the verdict on disk is the reviewer's own
words"). Worth a harness fix: either give the review agents a bounded gate
budget, or let a read-only reviewer emit its verdict through a tool the driver
persists verbatim.

**The configured gates do not cover the code most tickets touch.** `just test`
runs `uv run pytest tests` (root E2E only), so none of this ticket's four test
files ran under either gate. Worse, `packages/common/tests` and
`packages/cli/tests` ran in NO CI job either, so they would not have gated on
push. Fixed here for those two suites, but `just test` itself is still narrower
than the tree; deliberately left to a follow-up ticket (plan Q2) because widening
it changes the gate for every in-flight ticket at once.

**`prek` fails its first pass whenever ruff-format rewrites.** Every stamp cycle
costs two runs: one that reformats and exits non-zero, one that passes. Known
pattern, just tax.

**The `uv` version pin fights the local install.** `pyproject.toml` pins
`required-version = "~=0.11.0"`; the machine has 0.10.8 via Homebrew and 0.9.2
via pipx, so every bare `uv` command fails until a 0.11.x is put on PATH. Also
`uv sync --all-extras` is a trap here: the `gpu` extra pins `onnxruntime-gpu`,
which has no macOS arm64 wheel, and the failed resolve DELETES the existing
`.venv` before erroring. Sync with named extras instead.

**Worktree isolation is enforced, not advisory.** The pre-commit gate refuses
commits on the primary checkout while a ticket is in flight. Good guardrail, but
I learned it by hitting it after already staging work in the main tree. The
gate's command parser also rejects a multi-line `-m` string (it splits on
newlines and `shlex` chokes on the unterminated quote), so a commit body needs
repeated single-line `-m` flags or `-F`.
