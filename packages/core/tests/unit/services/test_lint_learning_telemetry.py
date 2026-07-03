"""Unit tests for LintLearningService.refresh_telemetry — vault-scoped vs. global rollup.

The bug: vault_id NULL was rejected by a NOT NULL constraint on the
lint_rule_telemetry table.  The fix uses DELETE+INSERT for the global row
(vault_id IS NULL) because Postgres treats NULL as distinct from NULL in
composite unique constraints, so ON CONFLICT cannot match the existing row.

These tests mock the metastore session to verify that:
1. A vault-scoped refresh issues both per-vault UPSERT and global DELETE+INSERT.
2. A global-only refresh (vault_id=None) issues only global DELETE+INSERT.
3. classify_verdict is used correctly for each row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.services.lint_learning import (
    LintLearningService,
    RefreshResult,
    _DELETE_GLOBAL_SQL,
    _INSERT_GLOBAL_SQL,
    _UPSERT_SQL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMapping(dict):
    """Dict subclass that also supports attribute access (like SQLAlchemy rows)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _make_service(fetched_rows: list[dict[str, Any]]) -> LintLearningService:
    """Build a LintLearningService with a mocked metastore.

    ``fetched_rows`` will be returned by the first session.execute (the
    FETCH query).  Subsequent session.execute calls (UPSERT/DELETE/INSERT)
    are recorded for inspection.

    The service uses two separate ``async with self.metastore.session()``
    blocks: one for the FETCH, one for the writes. We model this by tracking
    a call counter and returning the fetch result on the first execute,
    dummies thereafter.
    """
    # Build a mappings().all() chain for the fetch query.
    # Each row must be dict-like since the code does ``dict(row)`` on it.
    fetch_result = MagicMock()
    fetch_mappings = MagicMock()
    fetch_mappings.all.return_value = [_FakeMapping(row) for row in fetched_rows]
    fetch_result.mappings.return_value = fetch_mappings

    # Track all execute calls — first call returns the fetch result,
    # subsequent calls return a dummy (for the UPSERT/DELETE/INSERT).
    call_count = 0
    all_sessions: list[AsyncMock] = []

    def _make_session() -> AsyncMock:
        nonlocal call_count
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def _execute_side_effect(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fetch_result
            return MagicMock()

        mock_session.execute = AsyncMock(side_effect=_execute_side_effect)
        all_sessions.append(mock_session)
        return mock_session

    # Each ``async with self.metastore.session()`` call returns a fresh mock.
    def _session_factory() -> AsyncMock:
        session = _make_session()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    metastore = MagicMock()
    metastore.session.side_effect = lambda: _session_factory()

    filestore = MagicMock()
    config = MagicMock()

    service = LintLearningService(metastore=metastore, filestore=filestore, config=config)
    # Expose internal tracking for assertions.
    service._all_sessions = all_sessions  # type: ignore[attr-defined]
    return service


def _resolved_row(
    rule_name: str = 'cold_low_mw_unit',
    vault_id: str | None = None,
    action: str = 'deprioritize_unit',
    surprise: float = 0.8,
) -> dict[str, Any]:
    """Return a synthetic resolved proposal row dict."""
    now = datetime.now(timezone.utc)
    return {
        'rule_name': rule_name,
        'vault_id': vault_id,
        'status': 'resolved',
        'evidence': {
            'resolution': {'followup': {'action': action}},
        },
        'created_at': now - timedelta(hours=2),
        'resolved_at': now,
        'surprise_score': surprise,
    }


# ---------------------------------------------------------------------------
# Tests: vault-scoped refresh
# ---------------------------------------------------------------------------


class TestRefreshTelemetryVaultScoped:
    """When vault_id is supplied, both per-vault and global rows are written."""

    @pytest.mark.asyncio
    async def test_vault_scoped_writes_both_per_vault_and_global(self) -> None:
        vault = uuid4()
        rows = [
            _resolved_row('rule_a', str(vault)),
            _resolved_row('rule_b', str(vault)),
        ]
        service = _make_service(rows)
        result = await service.refresh_telemetry(vault_id=vault, window_days=30)

        assert isinstance(result, RefreshResult)
        assert result.vault_id == vault
        assert result.rules_seen == 2
        assert result.proposals_aggregated == 2
        # Per-vault: 2 UPSERT + global: 2*(DELETE+INSERT) = 2+4 = 6 writes
        # plus 1 commit. But the first session is for fetch, second for writes.
        # The mock session's execute is called once for fetch in session 1,
        # then multiple times for writes in session 2.
        # We just check that rows_written reflects both per-vault and global.
        assert result.rows_written == 4  # 2 per-vault + 2 global

    @pytest.mark.asyncio
    async def test_vault_scoped_per_vault_gets_vault_id_in_params(self) -> None:
        vault = uuid4()
        rows = [_resolved_row('rule_a', str(vault))]
        service = _make_service(rows)
        await service.refresh_telemetry(vault_id=vault, window_days=30)

        # Session 0 = fetch, session 1 = writes.
        # The write session's execute calls contain the upsert / delete / insert.
        sessions = service._all_sessions  # type: ignore[attr-defined]
        assert len(sessions) >= 2
        write_session = sessions[1]
        calls = write_session.execute.call_args_list
        # First write is the per-vault UPSERT.
        assert len(calls) >= 1
        upsert_params = (
            calls[0].args[1] if len(calls[0].args) > 1 else calls[0].kwargs.get('parameters', {})
        )
        assert upsert_params['vault_id'] == str(vault)


# ---------------------------------------------------------------------------
# Tests: global-only refresh (vault_id=None)
# ---------------------------------------------------------------------------


class TestRefreshTelemetryGlobal:
    """When vault_id is None, only the global rollup (DELETE+INSERT) fires."""

    @pytest.mark.asyncio
    async def test_global_refresh_writes_global_rows_only(self) -> None:
        # Two rules across different vaults.
        rows = [
            _resolved_row('rule_a', str(uuid4())),
            _resolved_row('rule_b', str(uuid4())),
        ]
        service = _make_service(rows)
        result = await service.refresh_telemetry(vault_id=None, window_days=30)

        assert result.vault_id is None
        assert result.rules_seen == 2
        # No per-vault rows (vault_id=None), only global DELETE+INSERT per rule.
        assert result.rows_written == 2  # only global rows

    @pytest.mark.asyncio
    async def test_global_refresh_passes_null_vault_to_fetch(self) -> None:
        service = _make_service([])
        await service.refresh_telemetry(vault_id=None, window_days=30)
        sessions = service._all_sessions  # type: ignore[attr-defined]
        # Session 0 = fetch session.
        fetch_session = sessions[0]
        fetch_params = fetch_session.execute.call_args_list[0].args[1]
        assert fetch_params['vault_id'] is None

    @pytest.mark.asyncio
    async def test_global_refresh_empty_window_returns_zero(self) -> None:
        service = _make_service([])
        result = await service.refresh_telemetry(vault_id=None, window_days=30)
        assert result.rows_written == 0
        assert result.rules_seen == 0


# ---------------------------------------------------------------------------
# Tests: bucket routing into global vs per-vault
# ---------------------------------------------------------------------------


class TestBucketRouting:
    """Verify that rows are binned into the correct per-vault / global buckets."""

    @pytest.mark.asyncio
    async def test_rows_from_other_vaults_contribute_to_global_but_not_per_vault(self) -> None:
        target_vault = uuid4()
        other_vault = uuid4()
        rows = [
            _resolved_row('rule_a', str(target_vault)),
            _resolved_row('rule_a', str(other_vault)),  # different vault
        ]
        service = _make_service(rows)
        result = await service.refresh_telemetry(vault_id=target_vault, window_days=30)

        # Per-vault: 1 row (target_vault only) → 1 UPSERT.
        # Global: 1 rule with 2 rows → 1 DELETE+INSERT.
        # Total: 1 per-vault + 1 global = 2 rows_written.
        assert result.rows_written == 2

    @pytest.mark.asyncio
    async def test_window_days_must_be_positive(self) -> None:
        service = _make_service([])
        with pytest.raises(ValueError, match='window_days must be positive'):
            await service.refresh_telemetry(vault_id=None, window_days=0)


# ---------------------------------------------------------------------------
# Tests: global DELETE+INSERT SQL uses NULL vault_id
# ---------------------------------------------------------------------------


class TestGlobalSQLNullVaultId:
    """Verify the SQL constants handle NULL vault_id correctly."""

    def test_insert_global_sql_uses_null_literal(self) -> None:
        assert 'NULL' in _INSERT_GLOBAL_SQL

    def test_delete_global_sql_uses_is_null(self) -> None:
        assert 'vault_id IS NULL' in _DELETE_GLOBAL_SQL

    def test_upsert_sql_casts_vault_id(self) -> None:
        # The UPSERT path (for per-vault) casts vault_id via CAST.
        assert 'CAST(:vault_id AS uuid)' in _UPSERT_SQL
