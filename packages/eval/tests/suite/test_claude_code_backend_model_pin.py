"""ClaudeCodeBackend pins the Claude model for reproducibility.

Without a pin the eval result depends on the user's local ``claude`` CLI
configuration, which makes cross-machine comparison impossible. Default
is ``claude-sonnet-4-6`` (matching longmemeval); ``MEMEX_EVAL_CLAUDE_MODEL``
overrides per shell.
"""

from __future__ import annotations

import pytest

from memex_eval.suite.agents import ClaudeCodeBackend


class TestModelPin:
    def test_default_is_sonnet_4_6(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('MEMEX_EVAL_CLAUDE_MODEL', raising=False)
        assert ClaudeCodeBackend().model == 'claude-sonnet-4-6'

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('MEMEX_EVAL_CLAUDE_MODEL', 'claude-opus-4-7')
        assert ClaudeCodeBackend().model == 'claude-opus-4-7'

    def test_empty_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('MEMEX_EVAL_CLAUDE_MODEL', '')
        assert ClaudeCodeBackend().model == 'claude-sonnet-4-6'
