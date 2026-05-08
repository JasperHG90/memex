"""Targeted tests for runner helpers fixed by adversarial review."""

from __future__ import annotations

from types import SimpleNamespace

from memex_eval.suite.runner import _extract_judge_revision


class TestJudgeRevisionExtractor:
    def test_returns_response_model_when_present(self) -> None:
        lm = SimpleNamespace(history=[{'response': {'model': 'gemini-2.5-pro-001'}}])
        assert _extract_judge_revision(lm) == 'gemini-2.5-pro-001'

    def test_returns_top_level_model_when_no_response_block(self) -> None:
        lm = SimpleNamespace(history=[{'model': 'gemini-2.5-flash'}])
        assert _extract_judge_revision(lm) == 'gemini-2.5-flash'

    def test_returns_none_on_empty_history(self) -> None:
        lm = SimpleNamespace(history=[])
        assert _extract_judge_revision(lm) is None

    def test_returns_none_on_malformed_entries(self) -> None:
        lm = SimpleNamespace(history=['not a dict'])
        assert _extract_judge_revision(lm) is None

    def test_returns_none_when_no_history_attr(self) -> None:
        lm = SimpleNamespace()
        assert _extract_judge_revision(lm) is None
