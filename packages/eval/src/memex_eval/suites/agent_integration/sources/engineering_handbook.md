---
tags: [handbook, engineering, conventions]
description: Engineering handbook — testing, deployment, style, security.
publish_date: 2025-01-15
---

# Engineering Handbook

Comprehensive reference for engineering practices at Acme Corp. Maintained by
the platform team. Read end-to-end during onboarding; consult sections as
needed afterwards.

## Testing

### Test types

We run three test tiers:

- **Unit tests** — under `tests/unit/`, no Postgres, no network, no
  filesystem. Each unit test must run in under 100ms.
- **Integration tests** — under `tests/integration/`, real Postgres via
  testcontainers, no network calls to third-party services.
- **End-to-end tests** — under `tests/e2e/`, real Postgres, real LLM API
  calls (gated behind `LLM_API_KEY`).

### Naming convention

Test files are named `test_<module>.py`. Test functions use the
**`test_` prefix**. Pytest's collection respects this convention.

### Pytest markers

- `@pytest.mark.slow` — skipped by default; opt in with `--run-slow`.
- `@pytest.mark.llm` — requires LLM API key; skipped in CI without one.
- `@pytest.mark.benchmark` — performance benchmarks; reported separately.

### Coverage gate

CI fails under 80% line coverage on changed files.

## Deployment

### Environments

We run three environments: **dev**, **staging**, **prod**. Each has its own
Kubernetes cluster, its own database, its own object store.

### Release cadence

- **dev** auto-deploys every commit to main.
- **staging** auto-deploys daily at 02:00 UTC.
- **prod** deploys on-demand via a manual GitHub Action with approval gate.

### Rollback policy

Every deployment ships with a one-click rollback to the previous version.
SREs may roll back without engineering approval if alerts fire within
30 minutes of deploy.

### Database migrations

Migrations run as a separate Kubernetes job, gated by an approval step.
Forward-only migrations are required. Down-migrations are tested locally
but never run in prod (we forward-fix).

### Configuration management

All config lives in version-controlled `values-<env>.yaml` files. Secrets
go in HashiCorp Vault, referenced by name. Never check secrets into git.

## Style

### Python

- **Formatter:** `ruff format` (configured in `pyproject.toml`).
- **Line length:** 100 characters.
- **Quotes:** single quotes (`'foo'`) except in docstrings.
- **Imports:** `ruff check --select I` enforces ordering.
- **Type hints:** required on every function signature; `mypy --strict`
  in CI.

### TypeScript

- **Formatter:** `prettier` with default config.
- **Lint:** `eslint` with `@typescript-eslint/recommended`.
- **No `any`:** PR review will reject explicit `any` without inline
  justification.

### Git

- Branch names: `<type>/<short-slug>` (e.g. `feat/add-cache-layer`).
- Commits: conventional commits (feat, fix, chore, docs, refactor, test).
- PR titles match the merge commit; PR body links the related ticket.

## Security

### Secret handling

- Never commit secrets. CI runs `git-secrets` in pre-commit.
- API keys live in Vault. Code retrieves them via the platform SDK.
- Database credentials rotate every 90 days; rotation is automated.

### Vulnerability response

- Critical CVEs (CVSS 9+) get a hotfix within 24 hours.
- High CVEs (CVSS 7-8) get a fix in the next scheduled release.
- Medium and below get triaged in the weekly review.

### Access control

- Production database access is read-only for engineers by default.
- Write access requires a one-time approval per session, logged.
- The `prod-admin` role is restricted to four named SREs.

### Logging

- No PII in logs. Filter at the SDK layer.
- Authentication failures are logged; the payload is not.
- Logs ship to the central platform with a 90-day retention.

## Observability

### Metrics

Every service emits Prometheus metrics. Required metrics:
- `requests_total{status}` — request count by HTTP status.
- `request_duration_seconds{handler}` — latency histogram.
- `errors_total{type}` — error count by category.

### Tracing

OpenTelemetry instrumentation is mandatory. Every request gets a trace ID
that propagates through the call graph.

### Dashboards

Each service owns one Grafana dashboard. The dashboard lives in the
service repository as a JSON file, deployed by the platform.
