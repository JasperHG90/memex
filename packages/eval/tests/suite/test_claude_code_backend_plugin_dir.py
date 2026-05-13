"""ClaudeCodeBackend resolves the memex Claude Code plugin directory.

Plugin parity with Hermes (the suite's default) requires that the
``claude`` subprocess gets ``--plugin-dir <claude-code-plugin>`` so the
agent loads the same briefing skills (``/remember``, ``/recall``, etc.)
and tool-routing rules that ``memex-hermes-plugin`` injects under the
Hermes backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memex_eval.suite.agents import ClaudeCodeBackend, _resolve_suite_plugin_dir


class TestResolveSuitePluginDir:
    def test_repo_default_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In the monorepo, the resolver finds packages/claude-code-plugin/."""
        monkeypatch.delenv('MEMEX_CLAUDE_PLUGIN_DIR', raising=False)
        resolved = _resolve_suite_plugin_dir()
        assert resolved is not None
        assert resolved.name == 'claude-code-plugin'
        assert (resolved / '.claude-plugin' / 'plugin.json').is_file()

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """``MEMEX_CLAUDE_PLUGIN_DIR`` takes precedence over the repo default."""
        manifest_dir = tmp_path / 'custom-plugin' / '.claude-plugin'
        manifest_dir.mkdir(parents=True)
        (manifest_dir / 'plugin.json').write_text('{"name": "test"}')
        monkeypatch.setenv('MEMEX_CLAUDE_PLUGIN_DIR', str(tmp_path / 'custom-plugin'))
        resolved = _resolve_suite_plugin_dir()
        assert resolved == tmp_path / 'custom-plugin'

    def test_missing_plugin_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Both candidates miss the manifest → resolver degrades gracefully."""
        monkeypatch.setenv('MEMEX_CLAUDE_PLUGIN_DIR', str(tmp_path / 'does-not-exist'))
        # Block the repo-default fallback by pointing __file__ at a tree
        # without pyproject.toml. The function returns None instead of
        # raising, so the backend can fall back to plugin-less invocation.
        import memex_eval.suite.agents as agents_mod

        fake_file = tmp_path / 'fake' / 'agents.py'
        fake_file.parent.mkdir()
        fake_file.write_text('')
        monkeypatch.setattr(agents_mod, '__file__', str(fake_file))
        resolved = _resolve_suite_plugin_dir()
        assert resolved is None


class TestBackendPicksUpPluginDir:
    def test_default_construction_resolves_plugin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('MEMEX_CLAUDE_PLUGIN_DIR', raising=False)
        b = ClaudeCodeBackend()
        assert b.plugin_dir is not None
        assert (b.plugin_dir / '.claude-plugin' / 'plugin.json').is_file()

    def test_env_override_flows_into_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manifest_dir = tmp_path / 'override-plugin' / '.claude-plugin'
        manifest_dir.mkdir(parents=True)
        (manifest_dir / 'plugin.json').write_text('{"name": "override"}')
        monkeypatch.setenv('MEMEX_CLAUDE_PLUGIN_DIR', str(tmp_path / 'override-plugin'))
        b = ClaudeCodeBackend()
        assert b.plugin_dir == tmp_path / 'override-plugin'
