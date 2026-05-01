# ADR-006: F20 Pins py-fsrs 4.1.2 (FSRS-5), Diverging From RFC-007's FSRS-4.5 Reference

## Status

Accepted

## Context

F20 (Revisitation) implements spaced-repetition scheduling for memory units using the FSRS family of algorithms. The original RFC-007 referenced FSRS-4.5, the latest published variant at the time of writing. Implementation work began against the `py-fsrs` PyPI package, which had since released version 4.1.2.

A direct cross-check between the live `py-fsrs` 4.1.2 source and the published FSRS-5 paper (verified 2026-05-01, captured in `POC-F20 paper-cross-check.md`) confirmed that py-fsrs 4.1.2 implements FSRS-5, not FSRS-4.5. The package version number does NOT match the algorithm version number — a documentation footgun.

Continuing under the assumption that py-fsrs implements FSRS-4.5 would mean every reviewer reading the code against RFC-007 would see parameters and update rules that do not match the cited algorithm, with no obvious signal that the code is correct against a DIFFERENT version of FSRS.

## Decision

F20 pins `py-fsrs == 4.1.2` and treats it as the FSRS-5 implementation. The original RFC-007 reference to FSRS-4.5 is superseded by this ADR. Any future upgrade of py-fsrs (to 4.2+, 5.0, or beyond) MUST trigger a re-validation against the FSRS paper that the new version actually implements, and an update or successor to this ADR.

The version-to-algorithm mapping is captured here so it survives package upgrades, contributor turnover, and RFC drift.

Implemented in `packages/core/src/memex_core/memory/revisit.py`. Cross-check evidence in `POC-F20 paper-cross-check.md`.

## Consequences

**Positive:**
- The algorithm in production matches the algorithm cited in code review and audit.
- A clear trigger exists for re-validation when py-fsrs upgrades.
- FSRS-5 is the more recent and better-calibrated algorithm; we benefit from the upgrade.

**Negative:**
- RFC-007 and this ADR disagree on a fact-of-the-matter (which FSRS variant is in use). Resolved by RFC-007 explicitly deferring to this ADR going forward.
- py-fsrs version numbers do not track algorithm versions — every upgrade requires a manual paper cross-check, not just a CHANGELOG read.
- If py-fsrs 4.1.3 is released as a bug-fix and silently changes algorithm behavior, our pin protects us, but a contributor who bumps the pin without reading this ADR could land a quiet regression. Mitigated by a comment at the import site referencing this ADR.

## Alternatives Considered

- **Downgrade to a py-fsrs version that implements FSRS-4.5** — rejected: older, less-calibrated algorithm, and no clear long-term maintenance story.
- **Reimplement FSRS-4.5 in-house to match RFC-007** — rejected: high maintenance cost, reinventing a well-tested library.
- **Update RFC-007 in place** — rejected: RFC history is part of the audit trail; an ADR is the correct mechanism for a decision that supersedes an earlier RFC reference.
