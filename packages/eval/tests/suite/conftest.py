"""Test fixtures for the suite-framework test module.

The outcome and setup-action registries are process-globals; tests that
register custom entries must not leak across runs. This autouse fixture
snapshots both registries before each test and restores them after, so
test ordering and re-imports stay clean.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from memex_eval.suite.agents import _BACKEND_REGISTRY
from memex_eval.suite.base import _OUTCOME_REGISTRY
from memex_eval.suite.setup_actions import _SETUP_ACTION_REGISTRY


@pytest.fixture(autouse=True)
def _isolate_suite_registries() -> Iterator[None]:
    outcome_snapshot = dict(_OUTCOME_REGISTRY)
    setup_snapshot = dict(_SETUP_ACTION_REGISTRY)
    backend_snapshot = dict(_BACKEND_REGISTRY)
    yield
    _OUTCOME_REGISTRY.clear()
    _OUTCOME_REGISTRY.update(outcome_snapshot)
    _SETUP_ACTION_REGISTRY.clear()
    _SETUP_ACTION_REGISTRY.update(setup_snapshot)
    _BACKEND_REGISTRY.clear()
    _BACKEND_REGISTRY.update(backend_snapshot)
