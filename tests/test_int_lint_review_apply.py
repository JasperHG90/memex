"""F7 — integration test for ``memex lint review --apply``.

Spins up a real Postgres via testcontainers (root ``conftest.py``), seeds
``MaintenanceProposal`` rows, and drives the CLI end-to-end. Three cases:

  1. ``--apply`` with verdicts ``a, s`` → first row resolved, second untouched.
  2. Audit-log invariant: a finding driven through ``memex lint resolve <id>``
     and a finding driven through ``memex lint review --apply`` (accept) must
     yield equivalent rows on the resolution-relevant columns.
  3. Dry-run: ``a, a`` without ``--apply`` → both rows still pending.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import UUID, uuid4

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from memex_cli import app
from memex_common.client import RemoteMemexAPI
from memex_core.server import app as server_app


runner = CliRunner()


@asynccontextmanager
async def _mock_api_context(*_args, **_kwargs):
    """Route CLI requests through the in-process FastAPI app via ASGITransport.

    Mirrors the pattern in ``tests/test_e2e_cli.py``.
    """
    from memex_core.server import lifespan

    async with lifespan(server_app):
        async with AsyncClient(
            transport=ASGITransport(app=server_app),
            base_url='http://test/api/v1/',
        ) as client:
            yield RemoteMemexAPI(client)


def _setup_env(postgres_container):
    dsn = postgres_container.get_connection_url()
    parsed = urlparse(dsn)
    os.environ['MEMEX_SERVER__META_STORE__TYPE'] = 'postgres'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__HOST'] = parsed.hostname or 'localhost'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PORT'] = str(parsed.port or 5432)
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__DATABASE'] = parsed.path.lstrip('/')
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__USER'] = parsed.username or 'test'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD'] = parsed.password or 'test'


_CLI_OVERRIDES = [
    '--set',
    'server_url=http://test',
    '--set',
    'server.meta_store.type=postgres',
    '--set',
    'server.meta_store.instance.host=localhost',
    '--set',
    'server.meta_store.instance.database=dummy',
    '--set',
    'server.meta_store.instance.user=dummy',
    '--set',
    'server.meta_store.instance.password=dummy',
    '--set',
    'server.memory.extraction.model.model=gemini/gemini-3-flash-preview',
]


def _seed_two_pending_findings(postgres_url: str) -> tuple[UUID, list[UUID]]:
    """Seed a fresh vault + 2 pending proposals via raw SQL. Returns (vault_id, [f1, f2])."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _seed() -> tuple[UUID, list[UUID]]:
        conn = await asyncpg.connect(dsn)
        try:
            vault_id = uuid4()
            vault_name = f'F7-int-{vault_id.hex[:8]}'
            await conn.execute(
                'INSERT INTO vaults (id, name, created_at) VALUES ($1, $2, NOW())',
                vault_id,
                vault_name,
            )
            ids: list[UUID] = []
            for rule in ('rule_review_a', 'rule_review_b'):
                fid = uuid4()
                await conn.execute(
                    'INSERT INTO maintenance_proposals '
                    '(id, vault_id, lint_type, target_type, target_id, rule_name, evidence, '
                    'suggested_action, status, source) '
                    "VALUES ($1, $2, 'quality', 'memory_unit', $3, $4, '{}'::jsonb, "
                    "'fix it', 'pending', 'rule')",
                    fid,
                    vault_id,
                    str(uuid4()),
                    rule,
                )
                ids.append(fid)
            return vault_id, ids
        finally:
            await conn.close()

    return asyncio.run(_seed())


def _read_status_row(postgres_url: str, finding_id: UUID) -> dict[str, object]:
    """Return ``{status, resolved_by}`` for a given finding (resolved_at is non-deterministic)."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _read() -> dict[str, object]:
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                'SELECT status, resolved_by, resolved_at IS NOT NULL AS has_resolved_at '
                'FROM maintenance_proposals WHERE id = $1',
                finding_id,
            )
            assert row is not None
            return dict(row)
        finally:
            await conn.close()

    return asyncio.run(_read())


@pytest.mark.integration
def test_review_apply_resolves_accepted_and_leaves_skipped(postgres_container, postgres_url):
    """``a\\ns\\n`` with ``--apply`` flips one finding to resolved and leaves the other pending.

    The CLI surfaces findings in ``created_at DESC`` order; rather than guessing which
    rule lands first, we just assert exactly one row is resolved and the other still pending.
    """
    _setup_env(postgres_container)
    vault_id, [f1, f2] = _seed_two_pending_findings(postgres_url)

    with patch('memex_cli.lint.get_api_context', _mock_api_context):
        result = runner.invoke(
            app,
            [
                *_CLI_OVERRIDES,
                'lint',
                'review',
                '--vault',
                str(vault_id),
                '--apply',
            ],
            input='a\ns\n',
        )

    assert result.exit_code == 0, f'CLI failed: {result.stdout}'

    rows = [_read_status_row(postgres_url, fid) for fid in (f1, f2)]
    statuses = sorted(str(r['status']).rsplit('.', 1)[-1] for r in rows)
    assert statuses == ['pending', 'resolved'], (
        f'expected one pending + one resolved, got {statuses}. stdout: {result.stdout}'
    )
    resolved_rows = [r for r in rows if str(r['status']).endswith('resolved')]
    pending_rows = [r for r in rows if str(r['status']).endswith('pending')]
    assert resolved_rows[0]['has_resolved_at'] is True
    assert pending_rows[0]['has_resolved_at'] is False


@pytest.mark.integration
def test_review_apply_audit_log_invariant_matches_direct_resolve(postgres_container, postgres_url):
    """A finding resolved via ``lint resolve <id>`` and one resolved via ``lint review --apply``
    must end up with the same shape on the audit-log-relevant columns.

    Concretely: both rows must have ``status=resolved``, ``resolved_at IS NOT NULL``,
    and the same ``resolved_by`` value (NULL in both cases — the route does not
    pass ``actor`` through to ``LintService.set_status``).
    """
    _setup_env(postgres_container)
    vault_id, [direct_id, review_id] = _seed_two_pending_findings(postgres_url)

    with patch('memex_cli.lint.get_api_context', _mock_api_context):
        direct_result = runner.invoke(
            app,
            [*_CLI_OVERRIDES, 'lint', 'resolve', str(direct_id)],
        )
        assert direct_result.exit_code == 0, f'direct resolve failed: {direct_result.stdout}'

        review_result = runner.invoke(
            app,
            [
                *_CLI_OVERRIDES,
                'lint',
                'review',
                '--vault',
                str(vault_id),
                '--apply',
            ],
            input='a\nq\n',
        )
        assert review_result.exit_code == 0, f'review --apply failed: {review_result.stdout}'

    direct_row = _read_status_row(postgres_url, direct_id)
    review_row = _read_status_row(postgres_url, review_id)

    assert str(direct_row['status']).endswith('resolved')
    assert str(review_row['status']).endswith('resolved')
    assert direct_row['has_resolved_at'] is True
    assert review_row['has_resolved_at'] is True
    assert direct_row['resolved_by'] == review_row['resolved_by']


@pytest.mark.integration
def test_review_dry_run_does_not_mutate(postgres_container, postgres_url):
    """``a\\na\\n`` WITHOUT ``--apply`` collects verdicts but leaves both rows pending."""
    _setup_env(postgres_container)
    vault_id, [f1, f2] = _seed_two_pending_findings(postgres_url)

    with patch('memex_cli.lint.get_api_context', _mock_api_context):
        result = runner.invoke(
            app,
            [
                *_CLI_OVERRIDES,
                'lint',
                'review',
                '--vault',
                str(vault_id),
            ],
            input='a\na\n',
        )

    assert result.exit_code == 0, f'CLI failed: {result.stdout}'

    after_f1 = _read_status_row(postgres_url, f1)
    after_f2 = _read_status_row(postgres_url, f2)
    assert str(after_f1['status']).endswith('pending'), after_f1
    assert str(after_f2['status']).endswith('pending'), after_f2
    assert after_f1['has_resolved_at'] is False
    assert after_f2['has_resolved_at'] is False
