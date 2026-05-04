"""Unit tests for longmemeval_answer_direct (note-only / memory-only modes)."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_eval.external.longmemeval_answer_direct import (
    ABSTENTION_MARKER,
    DirectMode,
    _format_memory_results,
    _format_note_results,
    _is_abstention,
    answer_direct,
)
from memex_eval.external.longmemeval_common import (
    LongMemEvalCategory,
    LongMemEvalQuestion,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def question() -> LongMemEvalQuestion:
    return LongMemEvalQuestion(
        question_id='q_test',
        category=LongMemEvalCategory.SINGLE_SESSION_USER,
        is_abstention=False,
        question_text='What is the capital of France?',
        answer='Paris',
        answer_session_ids=['sess_1'],
        question_date=dt.datetime(2023, 5, 30, 12, 0, 0, tzinfo=dt.timezone.utc),
        sessions=[],
    )


@pytest.fixture
def mock_lm() -> MagicMock:
    lm = MagicMock()
    lm.copy = MagicMock(return_value=lm)
    lm.history = []
    return lm


def _mock_memory_unit(text: str, note_title: str = 'note') -> MagicMock:
    u = MagicMock()
    u.id = uuid4()
    u.text = text
    u.note_id = str(uuid4())
    u.note_title = note_title
    return u


def _mock_note_result(title: str, description: str) -> MagicMock:
    n = MagicMock()
    n.note_id = uuid4()
    n.title = title
    n.description = description
    n.summaries = []
    return n


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def test_format_memory_results_renders_numbered_block() -> None:
    units = [
        _mock_memory_unit('First fact', note_title='NoteA'),
        _mock_memory_unit('Second fact', note_title='NoteB'),
    ]
    out = _format_memory_results(units)
    assert '[1] (NoteA) First fact' in out
    assert '[2] (NoteB) Second fact' in out


def test_format_note_results_includes_description_and_summaries() -> None:
    n = MagicMock()
    n.note_id = uuid4()
    n.title = 'Paris Travel Log'
    n.description = 'Trip to Paris'
    n.summaries = ['Visited the Eiffel Tower', {'summary': 'Ate croissants'}]
    out = _format_note_results([n])
    assert '[1] (Paris Travel Log)' in out
    assert 'Trip to Paris' in out
    assert 'Visited the Eiffel Tower' in out
    assert 'Ate croissants' in out


def test_format_note_results_handles_missing_description() -> None:
    n = MagicMock()
    n.note_id = uuid4()
    n.title = 'Empty'
    n.description = ''
    n.summaries = []
    out = _format_note_results([n])
    assert '(no description)' in out


# ---------------------------------------------------------------------------
# Abstention detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'answer, expected',
    [
        ('', True),
        ('   ', True),
        (ABSTENTION_MARKER, True),
        ('I do not know based on the available memory.', True),
        ('i do not know', True),
        ("I don't know for certain", True),
        ('Paris', False),
        ('The capital is Paris.', False),
    ],
)
def test_is_abstention_classifies_correctly(answer: str, expected: bool) -> None:
    assert _is_abstention(answer) is expected


# ---------------------------------------------------------------------------
# answer_direct: memory-only mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_try_success_no_retry_memory_only(question, mock_lm) -> None:
    """When first attempt returns a confident answer, do not call the tool a second time."""
    with (
        patch('memex_eval.external.longmemeval_answer_direct.RemoteMemexAPI') as mock_api_cls,
        patch(
            'memex_eval.external.longmemeval_answer_direct._synthesize',
            new_callable=AsyncMock,
            return_value=('Paris', 100, 5),
        ) as mock_synth,
        patch('memex_eval.external.longmemeval_answer_direct.httpx.AsyncClient') as mock_client_cls,
    ):
        mock_api = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        mock_api.search = AsyncMock(return_value=[_mock_memory_unit('France capital is Paris')])
        mock_api_cls.return_value = mock_api

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await answer_direct(
            question=question,
            mode=DirectMode.MEMORY_ONLY,
            vault_name='test-vault',
            server_url='http://test',
            answer_lm=mock_lm,
        )

    assert result['answer'] == 'Paris'
    assert mock_api.search.await_count == 1, 'should NOT retry when first answer is confident'
    assert mock_synth.await_count == 1
    assert len(result['tool_calls']) == 1
    assert result['tool_calls'][0]['input']['expand_query'] is False
    assert result['tool_calls'][0]['name'] == 'memex_memory_search'


@pytest.mark.asyncio
async def test_abstention_triggers_expand_retry_memory_only(question, mock_lm) -> None:
    """First abstention → retry with expand_query=True."""
    with (
        patch('memex_eval.external.longmemeval_answer_direct.RemoteMemexAPI') as mock_api_cls,
        patch(
            'memex_eval.external.longmemeval_answer_direct._synthesize',
            new_callable=AsyncMock,
            side_effect=[(ABSTENTION_MARKER, 50, 10), ('Paris', 120, 8)],
        ) as mock_synth,
        patch('memex_eval.external.longmemeval_answer_direct.httpx.AsyncClient') as mock_client_cls,
    ):
        mock_api = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        mock_api.search = AsyncMock(
            side_effect=[
                [_mock_memory_unit('irrelevant data')],
                [_mock_memory_unit('Paris is the capital')],
            ]
        )
        mock_api_cls.return_value = mock_api

        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await answer_direct(
            question=question,
            mode=DirectMode.MEMORY_ONLY,
            vault_name='test-vault',
            server_url='http://test',
            answer_lm=mock_lm,
        )

    assert result['answer'] == 'Paris'
    assert mock_api.search.await_count == 2, 'second call should fire on abstention'
    assert mock_synth.await_count == 2
    # First call has expand_query=False, second has True
    assert mock_api.search.await_args_list[0].kwargs['expand_query'] is False
    assert mock_api.search.await_args_list[1].kwargs['expand_query'] is True
    assert len(result['tool_calls']) == 2


@pytest.mark.asyncio
async def test_double_abstention_returns_final_abstention(question, mock_lm) -> None:
    """If both attempts abstain, final answer is the abstention marker."""
    with (
        patch('memex_eval.external.longmemeval_answer_direct.RemoteMemexAPI') as mock_api_cls,
        patch(
            'memex_eval.external.longmemeval_answer_direct._synthesize',
            new_callable=AsyncMock,
            return_value=(ABSTENTION_MARKER, 30, 5),
        ),
        patch('memex_eval.external.longmemeval_answer_direct.httpx.AsyncClient') as mock_client_cls,
    ):
        mock_api = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        mock_api.search = AsyncMock(return_value=[_mock_memory_unit('irrelevant')])
        mock_api_cls.return_value = mock_api
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await answer_direct(
            question=question,
            mode=DirectMode.MEMORY_ONLY,
            vault_name='test-vault',
            server_url='http://test',
            answer_lm=mock_lm,
        )

    assert result['answer'] == ABSTENTION_MARKER
    assert mock_api.search.await_count == 2


@pytest.mark.asyncio
async def test_token_accumulation_across_retries(question, mock_lm) -> None:
    """Tokens from both attempts accumulate in the result."""
    with (
        patch('memex_eval.external.longmemeval_answer_direct.RemoteMemexAPI') as mock_api_cls,
        patch(
            'memex_eval.external.longmemeval_answer_direct._synthesize',
            new_callable=AsyncMock,
            side_effect=[(ABSTENTION_MARKER, 50, 10), ('Paris', 120, 8)],
        ),
        patch('memex_eval.external.longmemeval_answer_direct.httpx.AsyncClient') as mock_client_cls,
    ):
        mock_api = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        mock_api.search = AsyncMock(return_value=[_mock_memory_unit('text')])
        mock_api_cls.return_value = mock_api
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await answer_direct(
            question=question,
            mode=DirectMode.MEMORY_ONLY,
            vault_name='test-vault',
            server_url='http://test',
            answer_lm=mock_lm,
        )

    # Output tokens accumulate across both synthesis calls
    assert result['tokens']['output'] == 10 + 8
    # Input tokens = synthesis input + retrieval content tokens (both attempts)
    assert result['tokens']['input'] >= 50 + 120  # at least synthesis inputs


# ---------------------------------------------------------------------------
# answer_direct: note-only mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_only_calls_search_notes(question, mock_lm) -> None:
    """note-only mode should call api.search_notes, NOT api.search."""
    with (
        patch('memex_eval.external.longmemeval_answer_direct.RemoteMemexAPI') as mock_api_cls,
        patch(
            'memex_eval.external.longmemeval_answer_direct._synthesize',
            new_callable=AsyncMock,
            return_value=('Paris', 80, 5),
        ),
        patch('memex_eval.external.longmemeval_answer_direct.httpx.AsyncClient') as mock_client_cls,
    ):
        mock_api = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        mock_api.search = AsyncMock(return_value=[])
        mock_api.search_notes = AsyncMock(
            return_value=[_mock_note_result('France', 'Paris is the capital of France')]
        )
        mock_api_cls.return_value = mock_api
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await answer_direct(
            question=question,
            mode=DirectMode.NOTE_ONLY,
            vault_name='test-vault',
            server_url='http://test',
            answer_lm=mock_lm,
        )

    assert mock_api.search_notes.await_count == 1
    assert mock_api.search.await_count == 0
    assert result['tool_calls'][0]['name'] == 'memex_note_search'
    assert result['answer'] == 'Paris'


@pytest.mark.asyncio
async def test_limit_passed_through(question, mock_lm) -> None:
    """Default limit of 10 is passed to the tool."""
    with (
        patch('memex_eval.external.longmemeval_answer_direct.RemoteMemexAPI') as mock_api_cls,
        patch(
            'memex_eval.external.longmemeval_answer_direct._synthesize',
            new_callable=AsyncMock,
            return_value=('Paris', 80, 5),
        ),
        patch('memex_eval.external.longmemeval_answer_direct.httpx.AsyncClient') as mock_client_cls,
    ):
        mock_api = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        mock_api.search = AsyncMock(return_value=[_mock_memory_unit('x')])
        mock_api_cls.return_value = mock_api
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        await answer_direct(
            question=question,
            mode=DirectMode.MEMORY_ONLY,
            vault_name='test-vault',
            server_url='http://test',
            answer_lm=mock_lm,
        )

    call = mock_api.search.await_args
    assert call is not None
    assert call.kwargs['limit'] == 10


@pytest.mark.asyncio
async def test_reference_date_passed_through(question, mock_lm) -> None:
    """question.question_date is passed as reference_date for temporal resolution."""
    with (
        patch('memex_eval.external.longmemeval_answer_direct.RemoteMemexAPI') as mock_api_cls,
        patch(
            'memex_eval.external.longmemeval_answer_direct._synthesize',
            new_callable=AsyncMock,
            return_value=('Paris', 80, 5),
        ),
        patch('memex_eval.external.longmemeval_answer_direct.httpx.AsyncClient') as mock_client_cls,
    ):
        mock_api = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        mock_api.search = AsyncMock(return_value=[_mock_memory_unit('x')])
        mock_api_cls.return_value = mock_api
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        await answer_direct(
            question=question,
            mode=DirectMode.MEMORY_ONLY,
            vault_name='test-vault',
            server_url='http://test',
            answer_lm=mock_lm,
        )

    call = mock_api.search.await_args
    assert call is not None
    assert call.kwargs['reference_date'] == question.question_date


@pytest.mark.asyncio
async def test_empty_results_produce_abstention(question, mock_lm) -> None:
    """If retrieval returns zero results, we abstain without calling the LLM for that attempt."""
    with (
        patch('memex_eval.external.longmemeval_answer_direct.RemoteMemexAPI') as mock_api_cls,
        patch(
            'memex_eval.external.longmemeval_answer_direct._synthesize',
            new_callable=AsyncMock,
            return_value=(ABSTENTION_MARKER, 0, 0),
        ) as mock_synth,
        patch('memex_eval.external.longmemeval_answer_direct.httpx.AsyncClient') as mock_client_cls,
    ):
        mock_api = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        mock_api.search = AsyncMock(return_value=[])
        mock_api_cls.return_value = mock_api
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await answer_direct(
            question=question,
            mode=DirectMode.MEMORY_ONLY,
            vault_name='test-vault',
            server_url='http://test',
            answer_lm=mock_lm,
        )

    # Synthesis should NOT be called when there is no context at all
    assert mock_synth.await_count == 0
    assert result['answer'] == ABSTENTION_MARKER
    # But retrieval IS called twice (retry on empty)
    assert mock_api.search.await_count == 2
