"""Lint auto-learning loop — Layer 2: telemetry rollups.

Reads ``maintenance_proposals`` and aggregates resolved rows into
``lint_rule_telemetry``. Every later layer of the loop (threshold
calibration, DSPy compile, auto-solve) reads this table to decide
whether it has enough labelled data to act. Today's deliverable is
read-only observability: ``memex lint stats`` renders the rollup so an
operator can see which rules are signal and which are noise.

Verdict classification:

* ``accept``  — ``status='resolved'`` and ``evidence.resolution.followup.action``
  is set and the action_id is NOT ``no_op``. The operator picked a canned
  remediation; the rule's signal was useful.
* ``no_op``   — ``status='resolved'`` and ``followup.action = 'no_op'``.
  The operator reviewed but chose not to mutate state.
* ``dismiss`` — ``status='dismissed'``. Operator judged the rule wrong.
* ``legacy``  — ``status='resolved'`` but no ``resolution.followup`` block.
  Pre-cockpit rows; cannot be classified accept vs no_op. Counted
  separately so operators can see how much history is unlabelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text

from memex_core.services.base import BaseService


@dataclass(frozen=True)
class LintRuleTelemetryDTO:
    """Read-side projection of ``lint_rule_telemetry``."""

    rule_name: str
    vault_id: UUID | None
    window_start: datetime
    window_end: datetime
    accept_count: int
    no_op_count: int
    dismiss_count: int
    legacy_count: int
    median_surprise: float | None
    median_time_to_resolve_seconds: int | None
    refreshed_at: datetime

    @property
    def total_count(self) -> int:
        return self.accept_count + self.no_op_count + self.dismiss_count + self.legacy_count

    @property
    def labelled_count(self) -> int:
        """Verdicts the loop CAN learn from — accept / no_op / dismiss."""
        return self.accept_count + self.no_op_count + self.dismiss_count

    @property
    def accept_rate(self) -> float | None:
        """Fraction of labelled verdicts where a canned action ran.

        Excludes legacy rows from the denominator — those aren't labelled,
        so they shouldn't dilute the rate.
        """
        if self.labelled_count == 0:
            return None
        return self.accept_count / self.labelled_count


@dataclass(frozen=True)
class RefreshResult:
    """Summary returned by ``refresh_telemetry``."""

    rows_written: int
    rules_seen: int
    proposals_aggregated: int
    window_start: datetime
    window_end: datetime
    vault_id: UUID | None
    warnings: list[str] = field(default_factory=list)


# Resolves a single row of `maintenance_proposals` into one of
# {accept, no_op, dismiss, legacy} for the verdict counters. Centralised so
# the unit test can exercise the classification logic against synthetic
# dicts without a Postgres roundtrip.
def classify_verdict(row: dict[str, Any]) -> str:
    status = row.get('status')
    if status == 'dismissed':
        return 'dismiss'
    if status != 'resolved':
        # Pending rows aren't aggregated; defensive return otherwise.
        return 'legacy'
    evidence = row.get('evidence') or {}
    if not isinstance(evidence, dict):
        return 'legacy'
    resolution = evidence.get('resolution')
    if not isinstance(resolution, dict):
        return 'legacy'
    followup = resolution.get('followup')
    if not isinstance(followup, dict):
        return 'legacy'
    action = followup.get('action')
    if not action:
        return 'legacy'
    if action == 'no_op':
        return 'no_op'
    return 'accept'


# Raw aggregate query: returns the list of resolved proposals for a window
# with enough fields to classify each verdict and compute medians. We do
# the classification client-side rather than in SQL — JSON path queries
# across versions of Postgres + asyncpg get hairy, and the row counts
# involved here (single-vault windows) are well under 10k typically.
_FETCH_SQL = """
    SELECT
        rule_name,
        vault_id::text AS vault_id,
        status,
        evidence,
        created_at,
        resolved_at,
        (evidence ->> 'surprise_score')::float AS surprise_score
    FROM maintenance_proposals
    WHERE created_at >= :window_start
      AND created_at < :window_end
      AND status IN ('resolved', 'dismissed')
"""


_UPSERT_SQL = """
    INSERT INTO lint_rule_telemetry (
        rule_name, vault_id, window_start, window_end,
        accept_count, no_op_count, dismiss_count, legacy_count,
        median_surprise, median_time_to_resolve_seconds, refreshed_at
    )
    VALUES (
        :rule_name, CAST(:vault_id AS uuid), :window_start, :window_end,
        :accept_count, :no_op_count, :dismiss_count, :legacy_count,
        :median_surprise, :median_time_to_resolve_seconds, now()
    )
    ON CONFLICT (rule_name, vault_id, window_start) DO UPDATE SET
        window_end = EXCLUDED.window_end,
        accept_count = EXCLUDED.accept_count,
        no_op_count = EXCLUDED.no_op_count,
        dismiss_count = EXCLUDED.dismiss_count,
        legacy_count = EXCLUDED.legacy_count,
        median_surprise = EXCLUDED.median_surprise,
        median_time_to_resolve_seconds = EXCLUDED.median_time_to_resolve_seconds,
        refreshed_at = now()
"""


# Variant of the upsert for the global rollup row (vault_id IS NULL). The
# composite PK includes vault_id, and Postgres treats NULL as distinct
# from NULL for uniqueness — so a plain ON CONFLICT clause does not
# match the existing global row. We DELETE the prior global row first
# inside the same transaction.
_DELETE_GLOBAL_SQL = """
    DELETE FROM lint_rule_telemetry
    WHERE rule_name = :rule_name
      AND vault_id IS NULL
      AND window_start = :window_start
"""


_INSERT_GLOBAL_SQL = """
    INSERT INTO lint_rule_telemetry (
        rule_name, vault_id, window_start, window_end,
        accept_count, no_op_count, dismiss_count, legacy_count,
        median_surprise, median_time_to_resolve_seconds, refreshed_at
    )
    VALUES (
        :rule_name, NULL, :window_start, :window_end,
        :accept_count, :no_op_count, :dismiss_count, :legacy_count,
        :median_surprise, :median_time_to_resolve_seconds, now()
    )
"""


_SELECT_SQL = """
    SELECT
        rule_name,
        vault_id::text AS vault_id,
        window_start,
        window_end,
        accept_count,
        no_op_count,
        dismiss_count,
        legacy_count,
        median_surprise,
        median_time_to_resolve_seconds,
        refreshed_at
    FROM lint_rule_telemetry
    WHERE (:rule_name::text IS NULL OR rule_name = :rule_name)
      AND (
        :vault_id::uuid IS NULL
          AND :include_global = TRUE
          AND vault_id IS NULL
        OR :vault_id::uuid IS NOT NULL
          AND vault_id = CAST(:vault_id AS uuid)
        OR :vault_id::uuid IS NULL
          AND :include_global = FALSE
      )
    ORDER BY window_end DESC, rule_name ASC
"""


class LintLearningService(BaseService):
    """Read + refresh the lint telemetry rollups.

    Refresh is idempotent: re-running with the same window writes the
    same row contents. Vault-scoped refreshes write per-vault rows AND
    the global (vault_id NULL) row for the same rule keys.
    """

    DEFAULT_WINDOW_DAYS = 30

    async def refresh_telemetry(
        self,
        *,
        vault_id: UUID | None = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> RefreshResult:
        """Aggregate resolved proposals into ``lint_rule_telemetry`` rows.

        Args:
            vault_id: when supplied, the rollup is scoped to this vault
                AND the global cross-vault row is also refreshed. When
                None, only the global row is refreshed (across all
                vaults' resolved findings).
            window_days: rolling window length. Defaults to 30 days.
            now: window end. Defaults to ``now(UTC)``; primarily for tests.
        """
        if window_days <= 0:
            raise ValueError('window_days must be positive')
        end = now or datetime.now(timezone.utc)
        start = end - timedelta(days=window_days)

        async with self.metastore.session() as session:
            result = await session.execute(
                text(_FETCH_SQL),
                {'window_start': start, 'window_end': end},
            )
            rows = [dict(row) for row in result.mappings().all()]

        # Two rollups in lock-step: per-vault (if vault_id supplied) and
        # global. The per-vault uses ON CONFLICT (rule, vault, window) DO
        # UPDATE; the global uses DELETE + INSERT because NULL-vault PK
        # is "distinct from NULL" in Postgres.
        per_vault: dict[str, _Bucket] = {}
        global_buckets: dict[str, _Bucket] = {}
        for row in rows:
            rule = row.get('rule_name')
            if not rule:
                continue
            verdict = classify_verdict(row)
            ttr = _resolve_ttr(row)
            row_vault = row.get('vault_id')
            global_buckets.setdefault(rule, _Bucket()).observe(
                verdict, row.get('surprise_score'), ttr
            )
            if vault_id is not None and row_vault == str(vault_id):
                per_vault.setdefault(rule, _Bucket()).observe(
                    verdict, row.get('surprise_score'), ttr
                )

        rules_seen = set(global_buckets.keys()) | set(per_vault.keys())
        rows_written = 0

        async with self.metastore.session() as session:
            for rule, bucket in per_vault.items():
                await session.execute(
                    text(_UPSERT_SQL),
                    bucket.upsert_params(
                        rule_name=rule,
                        vault_id=str(vault_id) if vault_id is not None else None,
                        window_start=start,
                        window_end=end,
                    ),
                )
                rows_written += 1
            for rule, bucket in global_buckets.items():
                await session.execute(
                    text(_DELETE_GLOBAL_SQL),
                    {'rule_name': rule, 'window_start': start},
                )
                await session.execute(
                    text(_INSERT_GLOBAL_SQL),
                    bucket.upsert_params(
                        rule_name=rule,
                        vault_id=None,
                        window_start=start,
                        window_end=end,
                    ),
                )
                rows_written += 1
            await session.commit()

        return RefreshResult(
            rows_written=rows_written,
            rules_seen=len(rules_seen),
            proposals_aggregated=len(rows),
            window_start=start,
            window_end=end,
            vault_id=vault_id,
        )

    async def get_telemetry(
        self,
        *,
        rule_name: str | None = None,
        vault_id: UUID | None = None,
        include_global: bool = True,
    ) -> list[LintRuleTelemetryDTO]:
        """Read rollup rows.

        Args:
            rule_name: optional filter; when None, every rule is returned.
            vault_id: optional vault scope. When supplied, returns rows for
                that vault. When None and ``include_global=True``, returns
                only the global rollup. When None and ``include_global=False``,
                returns every per-vault row (across all vaults).
        """
        async with self.metastore.session() as session:
            result = await session.execute(
                text(_SELECT_SQL),
                {
                    'rule_name': rule_name,
                    'vault_id': str(vault_id) if vault_id is not None else None,
                    'include_global': include_global,
                },
            )
            rows = result.mappings().all()
        return [_dto_from_row(dict(r)) for r in rows]


@dataclass
class _Bucket:
    """In-memory accumulator for one (rule, vault) rollup."""

    accept: int = 0
    no_op: int = 0
    dismiss: int = 0
    legacy: int = 0
    surprises: list[float] = field(default_factory=list)
    ttr_seconds: list[int] = field(default_factory=list)

    def observe(self, verdict: str, surprise: float | None, ttr: int | None) -> None:
        if verdict == 'accept':
            self.accept += 1
        elif verdict == 'no_op':
            self.no_op += 1
        elif verdict == 'dismiss':
            self.dismiss += 1
        else:
            self.legacy += 1
        if surprise is not None:
            self.surprises.append(float(surprise))
        if ttr is not None and ttr >= 0:
            self.ttr_seconds.append(ttr)

    def upsert_params(
        self,
        *,
        rule_name: str,
        vault_id: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        return {
            'rule_name': rule_name,
            'vault_id': vault_id,
            'window_start': window_start,
            'window_end': window_end,
            'accept_count': self.accept,
            'no_op_count': self.no_op,
            'dismiss_count': self.dismiss,
            'legacy_count': self.legacy,
            'median_surprise': _median(self.surprises),
            'median_time_to_resolve_seconds': _median_int(self.ttr_seconds),
        }


def _resolve_ttr(row: dict[str, Any]) -> int | None:
    created = row.get('created_at')
    resolved = row.get('resolved_at')
    if not created or not resolved:
        return None
    try:
        delta = resolved - created
    except TypeError:
        return None
    return int(delta.total_seconds())


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _median_int(values: list[int]) -> int | None:
    m = _median([float(v) for v in values])
    if m is None:
        return None
    return int(m)


def _dto_from_row(row: dict[str, Any]) -> LintRuleTelemetryDTO:
    vault_raw = row.get('vault_id')
    return LintRuleTelemetryDTO(
        rule_name=row['rule_name'],
        vault_id=UUID(vault_raw) if vault_raw else None,
        window_start=row['window_start'],
        window_end=row['window_end'],
        accept_count=int(row.get('accept_count') or 0),
        no_op_count=int(row.get('no_op_count') or 0),
        dismiss_count=int(row.get('dismiss_count') or 0),
        legacy_count=int(row.get('legacy_count') or 0),
        median_surprise=row.get('median_surprise'),
        median_time_to_resolve_seconds=row.get('median_time_to_resolve_seconds'),
        refreshed_at=row['refreshed_at'],
    )
