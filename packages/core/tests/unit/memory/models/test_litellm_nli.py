"""Tests for the LiteLLM NLI classifier adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from memex_common.config import LitellmNLIBackend
from memex_core.memory.models.backends.litellm_nli import (
    LiteLLMNLI,
    _parse_nli_response,
)
from memex_core.memory.models.protocols import NLIClassifierModel


class TestParseNLIResponse:
    def test_parse_clean_json(self) -> None:
        result = _parse_nli_response('{"entailment": 0.85, "neutral": 0.10, "contradiction": 0.05}')
        assert abs(result['entailment'] - 0.85) < 0.01
        assert abs(result['neutral'] - 0.10) < 0.01
        assert abs(result['contradiction'] - 0.05) < 0.01

    def test_parse_with_markdown_fences(self) -> None:
        content = '```json\n{"entailment": 0.8, "neutral": 0.15, "contradiction": 0.05}\n```'
        result = _parse_nli_response(content)
        assert 'entailment' in result

    def test_parse_with_surrounding_text(self) -> None:
        content = (
            'Here is the classification:\n'
            '{"entailment": 0.9, "neutral": 0.08, "contradiction": 0.02}\n'
            'Hope that helps!'
        )
        result = _parse_nli_response(content)
        assert abs(result['entailment'] - 0.9) < 0.01

    def test_normalises_probabilities(self) -> None:
        result = _parse_nli_response('{"entailment": 0.8, "neutral": 0.1, "contradiction": 0.1}')
        total = sum(result.values())
        assert abs(total - 1.0) < 0.001

    def test_raises_on_empty(self) -> None:
        with pytest.raises(ValueError, match='unparseable'):
            _parse_nli_response('')

    def test_raises_on_missing_keys(self) -> None:
        with pytest.raises(ValueError, match='missing keys'):
            _parse_nli_response('{"entailment": 0.5, "neutral": 0.5}')

    def test_raises_on_non_dict(self) -> None:
        with pytest.raises(ValueError, match='non-dict'):
            _parse_nli_response('"hello"')

    def test_raises_on_zero_total(self) -> None:
        with pytest.raises(ValueError, match='expected > 0'):
            _parse_nli_response('{"entailment": 0.0, "neutral": 0.0, "contradiction": 0.0}')

    def test_case_insensitive_keys(self) -> None:
        result = _parse_nli_response('{"Entailment": 0.5, "Neutral": 0.3, "Contradiction": 0.2}')
        assert 'entailment' in result
        assert 'neutral' in result
        assert 'contradiction' in result

    def test_extra_keys_ignored(self) -> None:
        result = _parse_nli_response(
            '{"entailment": 0.6, "neutral": 0.2, "contradiction": 0.2, "extra": 999}'
        )
        assert set(result.keys()) == {'entailment', 'neutral', 'contradiction'}

    def test_plain_code_fence(self) -> None:
        content = '```\n{"entailment": 0.7, "neutral": 0.2, "contradiction": 0.1}\n```'
        result = _parse_nli_response(content)
        assert abs(result['entailment'] - 0.7) < 0.01


class TestLiteLLMNLI:
    def test_satisfies_protocol(self) -> None:
        config = LitellmNLIBackend(model='openai/gpt-4o-mini')
        classifier = LiteLLMNLI(config)
        assert isinstance(classifier, NLIClassifierModel)

    def test_model_version(self) -> None:
        config = LitellmNLIBackend(model='openai/gpt-4o-mini')
        classifier = LiteLLMNLI(config)
        assert classifier.model_version == 'litellm:openai/gpt-4o-mini'

    @pytest.mark.asyncio
    async def test_classify_returns_probabilities(self) -> None:
        config = LitellmNLIBackend(model='openai/gpt-4o-mini')
        classifier = LiteLLMNLI(config)

        mock_message = AsyncMock()
        mock_message.content = '{"entailment": 0.85, "neutral": 0.10, "contradiction": 0.05}'
        mock_choice = AsyncMock()
        mock_choice.message = mock_message
        mock_response = AsyncMock()
        mock_response.choices = [mock_choice]

        with patch(
            'memex_core.memory.models.backends.litellm_nli.litellm.acompletion',
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classifier.classify(
                'FastAPI is a framework', 'FastAPI is not a framework'
            )

        assert abs(result['contradiction'] - 0.05) < 0.01
        assert abs(result['entailment'] - 0.85) < 0.01
        assert abs(result['neutral'] - 0.10) < 0.01

    @pytest.mark.asyncio
    async def test_classify_passes_kwargs(self) -> None:
        config = LitellmNLIBackend(
            model='ollama/llama3',
            api_base='http://localhost:11434',
            api_key='sk-test',
        )
        classifier = LiteLLMNLI(config)

        mock_message = AsyncMock()
        mock_message.content = '{"entailment": 0.1, "neutral": 0.2, "contradiction": 0.7}'
        mock_choice = AsyncMock()
        mock_choice.message = mock_message
        mock_response = AsyncMock()
        mock_response.choices = [mock_choice]

        with patch(
            'memex_core.memory.models.backends.litellm_nli.litellm.acompletion',
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_acompletion:
            await classifier.classify('premise', 'hypothesis')

        call_kwargs = mock_acompletion.call_args[1]
        assert call_kwargs['api_base'] == 'http://localhost:11434/'
        assert call_kwargs['api_key'] == 'sk-test'
        assert call_kwargs['temperature'] == 0.0
