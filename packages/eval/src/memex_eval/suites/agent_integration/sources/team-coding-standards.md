---
tags: [conventions, standards, engineering]
description: Team coding standards — conventions and their proposers.
---

# Team Coding Standards

Living document. Last reviewed November 2025.

## Adopted conventions

### Ruff line-length 100

**Proposed by:** Sarah Chen during the Q1 2025 style review (January).
**Rationale:** Default 88 produced too many awkward breaks in our domain
code (long signal names, qualified imports). 100 strikes a balance
without going to the 120 that diff tools handle poorly.

### Type-stub-first policy for new modules

**Proposed by:** Sarah Chen during the post-incident retrospective in
March 2025.
**Rationale:** Type stubs written before implementation catch interface
mistakes early. The March incident traced back to a function whose
parameter order was reversed silently. Stubs-first would have caught it
at PR review.

### Test layout convention

Every test file lives in `tests/<tier>/test_<module>.py` matching the
production module path. The `<tier>` segment is one of `unit`,
`integration`, `e2e`.

### Conventional commit format

Every commit message starts with `<type>(<scope>): <subject>` where
`<type>` is one of: feat, fix, chore, docs, refactor, test, style, perf.

### Branch naming

Branches follow `<type>/<short-slug>` (e.g. `feat/add-cache-layer`,
`fix/redis-pool-exhaustion`).
