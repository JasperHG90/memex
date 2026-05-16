"""Tests for the AnswerBackend abstraction + registry."""

from __future__ import annotations

from uuid import uuid4

import pytest

from memex_eval.suite import (
    AgentAnswer,
    AnswerBackend,
    DirectApiBackend,
    Scenario,
    KeywordsPresent,
    get_backend,
    list_backends,
    register_backend,
)


def _scenario() -> Scenario:
    return Scenario(
        id='s1',
        description='d',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
    )


def test_built_in_backends_registered() -> None:
    backends = list_backends()
    assert 'api' in backends
    assert 'claude-code' in backends
    assert 'hermes' in backends


def test_get_backend_returns_instance() -> None:
    backend = get_backend('api')
    assert isinstance(backend, DirectApiBackend)


def test_get_backend_raises_on_unknown() -> None:
    with pytest.raises(KeyError):
        get_backend('nonexistent_backend')


def test_register_custom_backend() -> None:
    @register_backend('test_custom')
    class _CustomBackend(AnswerBackend):
        async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
            return AgentAnswer(answer_text='custom!', backend_name=self.name)

    backend = get_backend('test_custom')
    assert backend.name == 'test_custom'


def test_agent_answer_default_shape() -> None:
    ans = AgentAnswer()
    assert ans.answer_text is None
    assert ans.units == []
    assert ans.tool_calls == []
    assert ans.retrieved_unit_ids == []
    assert ans.duration_ms == 0.0


@pytest.mark.asyncio
async def test_direct_api_backend_dispatches_memory_search() -> None:
    """DirectApiBackend.answer() with a KeywordsPresent outcome calls api.search."""
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    api = SimpleNamespace()
    api.search = AsyncMock(return_value=[SimpleNamespace(id='u1', text='hello x world')])

    backend = DirectApiBackend()
    answer = await backend.answer(
        _scenario(),
        api=api,
        vault_id=uuid4(),
        server_url='http://localhost:8000',
    )
    assert len(answer.units) == 1
    assert answer.retrieved_unit_ids == ['u1']
    assert answer.error is None
    api.search.assert_called_once()


@pytest.mark.asyncio
async def test_direct_api_backend_routes_search_type_note() -> None:
    """When scenario.search_type='note', backend uses api.search_notes."""
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    from memex_eval.suite import KeywordsPresent, Scenario

    sc = Scenario(
        id='note_search',
        description='d',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        search_type='note',
    )
    api = SimpleNamespace()
    # NoteSearchResult uses ``note_id``, not ``id``; the backend reads
    # ``getattr(n, 'note_id', '')`` (agents.py). Test fixtures that
    # mock ``id`` produce empty retrieved_unit_ids silently — the
    # latent bug RankingBaselineRbo surfaced.
    api.search_notes = AsyncMock(return_value=[SimpleNamespace(note_id='n1', text='note body')])
    api.search = AsyncMock()  # should NOT be called

    backend = DirectApiBackend()
    answer = await backend.answer(sc, api=api, vault_id=uuid4(), server_url='http://localhost:8000')
    api.search_notes.assert_called_once()
    api.search.assert_not_called()
    assert answer.retrieved_unit_ids == ['n1']


@pytest.mark.asyncio
async def test_direct_api_backend_unwraps_lint_findings_dict() -> None:
    """lint_findings() returns a dict; backend extracts the 'findings' list."""
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    from memex_eval.suite import LintFindingPresent, Scenario

    sc = Scenario(
        id='lint_one',
        description='d',
        query='q',
        expected=LintFindingPresent(
            type='lint_finding_present', expected_rule_name='surprise_gate_llm'
        ),
    )
    api = SimpleNamespace()
    api.lint_findings = AsyncMock(
        return_value={'findings': [{'rule_name': 'surprise_gate_llm'}], 'total': 1}
    )

    backend = DirectApiBackend()
    answer = await backend.answer(sc, api=api, vault_id=uuid4(), server_url='http://localhost:8000')
    assert len(answer.lint_findings) == 1
    # The wrapper exposes dict keys as attrs so getattr() works in score().
    assert answer.lint_findings[0].rule_name == 'surprise_gate_llm'
    metrics = sc.expected.score(answer, sc)
    assert metrics == {'pass': 1.0}
