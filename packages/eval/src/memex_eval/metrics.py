"""Scoring, aggregation, and result models for benchmark runs."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum


class CheckStatus(str, Enum):
    PASS = 'pass'
    FAIL = 'fail'
    SKIP = 'skip'
    ERROR = 'error'


@dataclass
class CheckResult:
    """Result of a single ground-truth check."""

    name: str
    group: str
    status: CheckStatus
    description: str
    query: str
    expected: str | list[str]
    actual: str = ''
    reasoning: str = ''
    duration_ms: float = 0.0


@dataclass
class GroupResult:
    """Aggregated results for a scenario group."""

    name: str
    description: str
    checks: list[CheckResult] = field(default_factory=list)
    ingest_duration_ms: float = 0.0
    reflection_duration_ms: float = 0.0

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.SKIP)

    @property
    def errored(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.ERROR)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def pass_rate(self) -> float:
        runnable = self.total - self.skipped
        if runnable == 0:
            return 0.0
        return self.passed / runnable


@dataclass
class BenchmarkResult:
    """Full benchmark run result."""

    groups: list[GroupResult] = field(default_factory=list)
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    finished_at: dt.datetime | None = None
    vault_name: str = ''

    @property
    def total_passed(self) -> int:
        return sum(g.passed for g in self.groups)

    @property
    def total_failed(self) -> int:
        return sum(g.failed for g in self.groups)

    @property
    def total_skipped(self) -> int:
        return sum(g.skipped for g in self.groups)

    @property
    def total_errored(self) -> int:
        return sum(g.errored for g in self.groups)

    @property
    def total_checks(self) -> int:
        return sum(g.total for g in self.groups)

    @property
    def overall_pass_rate(self) -> float:
        runnable = self.total_checks - self.total_skipped
        if runnable == 0:
            return 0.0
        return self.total_passed / runnable

    @property
    def duration_ms(self) -> float:
        if not self.finished_at:
            return 0.0
        delta = self.finished_at - self.started_at
        return delta.total_seconds() * 1000

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            'started_at': self.started_at.isoformat(),
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'duration_ms': self.duration_ms,
            'vault_name': self.vault_name,
            'summary': {
                'total': self.total_checks,
                'passed': self.total_passed,
                'failed': self.total_failed,
                'skipped': self.total_skipped,
                'errored': self.total_errored,
                'pass_rate': round(self.overall_pass_rate, 4),
            },
            'groups': [
                {
                    'name': g.name,
                    'description': g.description,
                    'ingest_duration_ms': g.ingest_duration_ms,
                    'reflection_duration_ms': g.reflection_duration_ms,
                    'passed': g.passed,
                    'failed': g.failed,
                    'skipped': g.skipped,
                    'errored': g.errored,
                    'pass_rate': round(g.pass_rate, 4),
                    'checks': [
                        {
                            'name': c.name,
                            'status': c.status.value,
                            'description': c.description,
                            'query': c.query,
                            'expected': c.expected,
                            'actual': c.actual,
                            'reasoning': c.reasoning,
                            'duration_ms': c.duration_ms,
                        }
                        for c in g.checks
                    ],
                }
                for g in self.groups
            ],
        }
