"""Tests for the ``memex setup claude-code`` CLI command."""

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from memex_cli.setup_claude_code import (
    CLAUDE_MD_MARKER,
    _build_hooks_config,
    _load_hook_template,
    _load_template,
    _mcp_server_entry,
    _merge_settings_local,
    app,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helper to invoke the setup command (shorthand used by most tests)
# ---------------------------------------------------------------------------


def _invoke(*args: str, **kwargs):
    return runner.invoke(app, ['claude-code', *args], catch_exceptions=False, **kwargs)


# ===========================================================================
# Unit tests — _load_template
# ===========================================================================


class TestLoadTemplate:
    """Tests for the _load_template helper."""

    def test_loads_remember_skill(self):
        content = _load_template('remember_skill.md')
        assert isinstance(content, str)
        assert len(content) > 0

    def test_loads_recall_skill(self):
        content = _load_template('recall_skill.md')
        assert isinstance(content, str)
        assert len(content) > 0

    def test_loads_claude_md_section(self):
        content = _load_template('claude_md_section.md')
        assert isinstance(content, str)
        assert CLAUDE_MD_MARKER in content

    def test_missing_template_raises(self):
        with pytest.raises(FileNotFoundError):
            _load_template('nonexistent.md')


# ===========================================================================
# Unit tests — _mcp_server_entry
# ===========================================================================


class TestMcpServerEntry:
    """Tests for the _mcp_server_entry helper."""

    def test_structure(self):
        entry = _mcp_server_entry('my-vault')
        assert entry['type'] == 'stdio'
        assert entry['command'] == 'uv'
        assert entry['args'] == ['run', 'memex', 'mcp', 'run']
        assert entry['env']['MEMEX_SERVER__ACTIVE_VAULT'] == 'my-vault'

    def test_vault_name_propagated(self):
        for name in ('global', 'work', 'personal'):
            assert _mcp_server_entry(name)['env']['MEMEX_SERVER__ACTIVE_VAULT'] == name


# ===========================================================================
# Unit tests — template content validation
# ===========================================================================


class TestTemplateContent:
    """Validate that bundled templates contain the expected structure."""

    # --- remember skill ---------------------------------------------------

    def test_remember_has_valid_frontmatter(self):
        content = _load_template('remember_skill.md')
        fm = self._extract_frontmatter(content)
        assert fm['name'] == 'remember'
        assert 'description' in fm
        assert 'argument-hint' in fm

    def test_remember_references_add_note(self):
        content = _load_template('remember_skill.md')
        assert 'memex_add_note' in content

    def test_remember_references_background(self):
        content = _load_template('remember_skill.md')
        assert 'background' in content.lower()

    def test_remember_references_arguments(self):
        content = _load_template('remember_skill.md')
        assert '$ARGUMENTS' in content

    # --- recall skill -----------------------------------------------------

    def test_recall_has_valid_frontmatter(self):
        content = _load_template('recall_skill.md')
        fm = self._extract_frontmatter(content)
        assert fm['name'] == 'recall'
        assert 'description' in fm
        assert 'argument-hint' in fm

    def test_recall_references_search_tools(self):
        content = _load_template('recall_skill.md')
        assert 'memex_search' in content
        assert 'memex_note_search' in content
        assert 'memex_list_entities' in content

    def test_recall_references_arguments(self):
        content = _load_template('recall_skill.md')
        assert '$ARGUMENTS' in content

    # --- claude_md_section ------------------------------------------------

    def test_claude_md_section_contains_marker(self):
        content = _load_template('claude_md_section.md')
        assert CLAUDE_MD_MARKER in content

    def test_claude_md_section_documents_slash_commands(self):
        content = _load_template('claude_md_section.md')
        assert '/remember' in content
        assert '/recall' in content

    def test_claude_md_section_references_tools(self):
        content = _load_template('claude_md_section.md')
        assert 'memex_add_note' in content
        assert 'memex_search' in content

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _extract_frontmatter(content: str) -> dict:
        """Extract YAML frontmatter delimited by ``---``."""
        parts = content.split('---', 2)
        assert len(parts) >= 3, 'No YAML frontmatter found'
        return yaml.safe_load(parts[1])


# ===========================================================================
# CLI integration tests — file creation
# ===========================================================================


class TestSetupCreatesFiles:
    """Verify that the setup command creates all expected files."""

    def test_creates_all_files_in_empty_dir(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path))
        assert result.exit_code == 0, result.output

        # Skill files
        remember = tmp_path / '.claude' / 'skills' / 'remember' / 'SKILL.md'
        recall = tmp_path / '.claude' / 'skills' / 'recall' / 'SKILL.md'
        assert remember.exists()
        assert recall.exists()
        assert 'memex_add_note' in remember.read_text()
        assert 'memex_search' in recall.read_text()

        # .mcp.json
        mcp_json = tmp_path / '.mcp.json'
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text())
        assert 'memex' in data['mcpServers']
        assert data['mcpServers']['memex']['command'] == 'uv'

        # CLAUDE.md
        claude_md = tmp_path / 'CLAUDE.md'
        assert claude_md.exists()
        content = claude_md.read_text()
        assert CLAUDE_MD_MARKER in content
        assert 'memex_add_note' in content

    def test_skill_content_matches_template(self, tmp_path):
        """Generated skill files must match the packaged templates exactly."""
        _invoke('--project-dir', str(tmp_path))

        for skill_name, template_file in [
            ('remember', 'remember_skill.md'),
            ('recall', 'recall_skill.md'),
        ]:
            expected = _load_template(template_file)
            actual = (tmp_path / '.claude' / 'skills' / skill_name / 'SKILL.md').read_text()
            assert actual == expected

    def test_mcp_json_is_valid_json(self, tmp_path):
        _invoke('--project-dir', str(tmp_path))
        data = json.loads((tmp_path / '.mcp.json').read_text())
        memex = data['mcpServers']['memex']
        assert memex['type'] == 'stdio'
        assert memex['args'] == ['run', 'memex', 'mcp', 'run']

    def test_default_vault_is_global(self, tmp_path):
        """Without --vault, the default vault should be 'global'."""
        _invoke('--project-dir', str(tmp_path))
        data = json.loads((tmp_path / '.mcp.json').read_text())
        assert data['mcpServers']['memex']['env']['MEMEX_SERVER__ACTIVE_VAULT'] == 'global'


# ===========================================================================
# CLI integration tests — .mcp.json merge behaviour
# ===========================================================================


class TestMcpJsonMerge:
    """Verify that .mcp.json is merged, not clobbered."""

    def test_preserves_existing_servers(self, tmp_path):
        mcp_path = tmp_path / '.mcp.json'
        mcp_path.write_text(
            json.dumps(
                {
                    'mcpServers': {
                        'playwright': {
                            'type': 'stdio',
                            'command': 'npx',
                            'args': ['@playwright/mcp'],
                        },
                    }
                }
            )
        )

        _invoke('--project-dir', str(tmp_path))

        data = json.loads(mcp_path.read_text())
        assert 'playwright' in data['mcpServers']
        assert 'memex' in data['mcpServers']

    def test_overwrites_existing_memex_entry(self, tmp_path):
        """Running setup twice updates the memex entry (e.g. vault change)."""
        _invoke('--project-dir', str(tmp_path), '--vault', 'old-vault')
        _invoke('--project-dir', str(tmp_path), '--vault', 'new-vault', '--force')

        data = json.loads((tmp_path / '.mcp.json').read_text())
        assert data['mcpServers']['memex']['env']['MEMEX_SERVER__ACTIVE_VAULT'] == 'new-vault'

    def test_creates_mcp_json_from_scratch(self, tmp_path):
        """When no .mcp.json exists, one is created with the correct structure."""
        assert not (tmp_path / '.mcp.json').exists()

        _invoke('--project-dir', str(tmp_path))

        data = json.loads((tmp_path / '.mcp.json').read_text())
        assert 'mcpServers' in data
        assert 'memex' in data['mcpServers']

    def test_handles_empty_mcp_json(self, tmp_path):
        """An existing but empty .mcp.json (just ``{}``) is handled gracefully."""
        (tmp_path / '.mcp.json').write_text('{}')

        _invoke('--project-dir', str(tmp_path))

        data = json.loads((tmp_path / '.mcp.json').read_text())
        assert 'memex' in data['mcpServers']


# ===========================================================================
# CLI integration tests — CLAUDE.md behaviour
# ===========================================================================


class TestClaudeMd:
    """Verify CLAUDE.md append, idempotency, and --force/--no-claude-md flags."""

    def test_appends_to_existing(self, tmp_path):
        claude_md = tmp_path / 'CLAUDE.md'
        claude_md.write_text('# My Project\n\nExisting instructions.\n')

        _invoke('--project-dir', str(tmp_path))

        content = claude_md.read_text()
        assert content.startswith('# My Project')
        assert CLAUDE_MD_MARKER in content
        assert 'memex_add_note' in content

    def test_idempotent_without_force(self, tmp_path):
        """Second run without --force must not modify CLAUDE.md."""
        _invoke('--project-dir', str(tmp_path))
        first_content = (tmp_path / 'CLAUDE.md').read_text()

        _invoke('--project-dir', str(tmp_path))
        second_content = (tmp_path / 'CLAUDE.md').read_text()

        assert first_content == second_content
        assert second_content.count(CLAUDE_MD_MARKER) == 1

    def test_idempotent_with_force(self, tmp_path):
        """Second run WITH --force replaces the section but keeps exactly one marker."""
        _invoke('--project-dir', str(tmp_path))
        _invoke('--project-dir', str(tmp_path), '--force')

        content = (tmp_path / 'CLAUDE.md').read_text()
        assert content.count(CLAUDE_MD_MARKER) == 1

    def test_force_preserves_content_before_marker(self, tmp_path):
        """--force must keep everything before the Memex marker intact."""
        claude_md = tmp_path / 'CLAUDE.md'
        preamble = '# Project\n\nDo not delete me.\n'
        claude_md.write_text(preamble)

        _invoke('--project-dir', str(tmp_path))
        _invoke('--project-dir', str(tmp_path), '--force')

        content = claude_md.read_text()
        before_marker, _, _ = content.partition(CLAUDE_MD_MARKER)
        assert 'Do not delete me.' in before_marker

    def test_no_claude_md_skips_creation(self, tmp_path):
        _invoke('--project-dir', str(tmp_path), '--no-claude-md')
        assert not (tmp_path / 'CLAUDE.md').exists()

    def test_no_claude_md_skips_modification(self, tmp_path):
        """--no-claude-md must not touch an existing CLAUDE.md."""
        claude_md = tmp_path / 'CLAUDE.md'
        original = '# Untouched\n'
        claude_md.write_text(original)

        _invoke('--project-dir', str(tmp_path), '--no-claude-md')

        assert claude_md.read_text() == original


# ===========================================================================
# CLI integration tests — skill file --force / skip behaviour
# ===========================================================================


class TestSkillForce:
    """Verify --force and skip-if-exists logic for skill files."""

    def test_skips_existing_without_force(self, tmp_path):
        skill_dir = tmp_path / '.claude' / 'skills' / 'remember'
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / 'SKILL.md'
        skill_file.write_text('custom content')

        _invoke('--project-dir', str(tmp_path))

        assert skill_file.read_text() == 'custom content'

    def test_force_overwrites_existing(self, tmp_path):
        skill_dir = tmp_path / '.claude' / 'skills' / 'remember'
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / 'SKILL.md'
        skill_file.write_text('custom content')

        _invoke('--project-dir', str(tmp_path), '--force')

        assert 'memex_add_note' in skill_file.read_text()

    def test_force_overwrites_both_skills(self, tmp_path):
        for name in ('remember', 'recall'):
            d = tmp_path / '.claude' / 'skills' / name
            d.mkdir(parents=True)
            (d / 'SKILL.md').write_text('old')

        _invoke('--project-dir', str(tmp_path), '--force')

        assert (
            'memex_add_note'
            in (tmp_path / '.claude' / 'skills' / 'remember' / 'SKILL.md').read_text()
        )
        assert (
            'memex_search' in (tmp_path / '.claude' / 'skills' / 'recall' / 'SKILL.md').read_text()
        )

    def test_skips_one_overwrites_other(self, tmp_path):
        """Only the skill that already exists is skipped; the other is still created."""
        remember_dir = tmp_path / '.claude' / 'skills' / 'remember'
        remember_dir.mkdir(parents=True)
        (remember_dir / 'SKILL.md').write_text('custom remember')

        _invoke('--project-dir', str(tmp_path))

        assert (remember_dir / 'SKILL.md').read_text() == 'custom remember'
        recall_file = tmp_path / '.claude' / 'skills' / 'recall' / 'SKILL.md'
        assert recall_file.exists()
        assert 'memex_search' in recall_file.read_text()


# ===========================================================================
# CLI integration tests — --vault option
# ===========================================================================


class TestVaultOption:
    """Verify custom vault name propagates correctly."""

    def test_custom_vault_in_mcp_json(self, tmp_path):
        _invoke('--project-dir', str(tmp_path), '--vault', 'work')
        data = json.loads((tmp_path / '.mcp.json').read_text())
        assert data['mcpServers']['memex']['env']['MEMEX_SERVER__ACTIVE_VAULT'] == 'work'

    def test_vault_with_special_characters(self, tmp_path):
        _invoke('--project-dir', str(tmp_path), '--vault', 'my-project_v2')
        data = json.loads((tmp_path / '.mcp.json').read_text())
        assert data['mcpServers']['memex']['env']['MEMEX_SERVER__ACTIVE_VAULT'] == 'my-project_v2'


# ===========================================================================
# CLI integration tests — health check
# ===========================================================================


class TestHealthCheck:
    """Verify the server health-check behaviour."""

    def test_skips_health_check_when_no_url(self, tmp_path):
        """Without --server-url and no config, the check is skipped gracefully."""
        result = _invoke('--project-dir', str(tmp_path))
        assert result.exit_code == 0
        # Files should still be created
        assert (tmp_path / '.mcp.json').exists()

    def test_continues_on_unreachable_server(self, tmp_path):
        """An unreachable server produces a warning but does not abort."""
        result = _invoke(
            '--project-dir',
            str(tmp_path),
            '--server-url',
            'http://localhost:1',
        )
        assert result.exit_code == 0
        assert (tmp_path / '.mcp.json').exists()
        assert (tmp_path / '.claude' / 'skills' / 'remember' / 'SKILL.md').exists()

    def test_warns_when_vault_not_found(self, tmp_path):
        """When the server is reachable but the vault doesn't exist, warn the user."""

        @dataclass
        class FakeVault:
            name: str

        mock_api = AsyncMock()
        mock_api.list_vaults.return_value = [FakeVault(name='other-vault')]

        with patch(
            'memex_cli.setup_claude_code.httpx.AsyncClient',
        ) as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client_instance
            with patch(
                'memex_cli.setup_claude_code.RemoteMemexAPI',
                return_value=mock_api,
            ):
                result = _invoke(
                    '--project-dir',
                    str(tmp_path),
                    '--server-url',
                    'http://localhost:9999',
                    '--vault',
                    'missing-vault',
                )

        assert result.exit_code == 0
        assert 'missing-vault' in result.output or (tmp_path / '.mcp.json').exists()

    def test_succeeds_when_vault_found(self, tmp_path):
        """When the server is reachable and vault exists, show success."""

        @dataclass
        class FakeVault:
            name: str

        mock_api = AsyncMock()
        mock_api.list_vaults.return_value = [
            FakeVault(name='global'),
            FakeVault(name='work'),
        ]

        with patch(
            'memex_cli.setup_claude_code.httpx.AsyncClient',
        ) as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client_instance
            with patch(
                'memex_cli.setup_claude_code.RemoteMemexAPI',
                return_value=mock_api,
            ):
                result = _invoke(
                    '--project-dir',
                    str(tmp_path),
                    '--server-url',
                    'http://localhost:9999',
                    '--vault',
                    'work',
                )

        assert result.exit_code == 0
        assert (tmp_path / '.mcp.json').exists()


# ===========================================================================
# CLI integration tests — output messages
# ===========================================================================


class TestOutputMessages:
    """Verify human-readable CLI output for key scenarios."""

    def test_fresh_setup_shows_created(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path))
        assert 'Created' in result.output or 'created' in result.output.lower()

    def test_skip_message_for_existing_skills(self, tmp_path):
        skill_dir = tmp_path / '.claude' / 'skills' / 'remember'
        skill_dir.mkdir(parents=True)
        (skill_dir / 'SKILL.md').write_text('custom')

        result = _invoke('--project-dir', str(tmp_path))
        assert 'Skipping' in result.output or 'already exists' in result.output.lower()

    def test_skip_message_for_existing_claude_md_section(self, tmp_path):
        claude_md = tmp_path / 'CLAUDE.md'
        claude_md.write_text(f'# Project\n\n{CLAUDE_MD_MARKER}\nold section\n')

        result = _invoke('--project-dir', str(tmp_path))
        assert 'already present' in result.output.lower() or 'Skipping' in result.output

    def test_summary_panel_shown(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path))
        assert 'setup complete' in result.output.lower()
        assert '/remember' in result.output
        assert '/recall' in result.output

    def test_summary_shows_hooks_enabled(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path))
        assert 'Hooks: Enabled' in result.output
        assert 'SessionStart' in result.output

    def test_summary_shows_hooks_disabled(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path), '--no-hooks')
        assert 'Hooks: Disabled' in result.output

    def test_summary_shows_session_end_when_tracking(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path), '--with-session-tracking')
        assert 'SessionEnd' in result.output


# ===========================================================================
# Unit tests — hook template content
# ===========================================================================


class TestHookTemplateContent:
    """Validate that hook templates exist and contain expected markers."""

    def test_session_start_exists_and_has_shebang(self):
        content = _load_template('hooks/on_session_start.sh')
        assert content.startswith('#!/usr/bin/env bash')

    def test_session_start_references_memex_cli(self):
        content = _load_template('hooks/on_session_start.sh')
        assert 'memex' in content
        assert 'note recent' in content

    def test_session_start_checks_uv(self):
        content = _load_template('hooks/on_session_start.sh')
        assert 'command -v uv' in content

    def test_session_start_checks_jq(self):
        content = _load_template('hooks/on_session_start.sh')
        assert 'command -v jq' in content

    def test_pre_compact_exists_and_has_shebang(self):
        content = _load_template('hooks/on_pre_compact.sh')
        assert content.startswith('#!/usr/bin/env bash')

    def test_pre_compact_references_state_dir(self):
        content = _load_template('hooks/on_pre_compact.sh')
        assert 'compact_pending.json' in content

    def test_pre_compact_has_project_dir_placeholder(self):
        content = _load_template('hooks/on_pre_compact.sh')
        assert '__PROJECT_DIR__' in content

    def test_session_end_exists_and_has_shebang(self):
        content = _load_template('hooks/on_session_end.sh')
        assert content.startswith('#!/usr/bin/env bash')

    def test_session_end_references_memex_cli(self):
        content = _load_template('hooks/on_session_end.sh')
        assert 'memex' in content
        assert 'memory add' in content

    def test_session_end_has_project_dir_placeholder(self):
        content = _load_template('hooks/on_session_end.sh')
        assert '__PROJECT_DIR__' in content

    def test_session_end_checks_uv(self):
        content = _load_template('hooks/on_session_end.sh')
        assert 'command -v uv' in content


# ===========================================================================
# Unit tests — _build_hooks_config
# ===========================================================================


class TestBuildHooksConfig:
    """Verify the hooks config structure for settings.local.json."""

    def test_default_includes_session_start_and_pre_compact(self, tmp_path):
        config = _build_hooks_config(tmp_path)
        assert 'SessionStart' in config
        assert 'PreCompact' in config
        assert 'SessionEnd' not in config

    def test_session_end_included_when_requested(self, tmp_path):
        config = _build_hooks_config(tmp_path, include_session_end=True)
        assert 'SessionEnd' in config

    def test_session_start_has_startup_matcher(self, tmp_path):
        config = _build_hooks_config(tmp_path)
        entry = config['SessionStart'][0]
        assert entry['matcher'] == 'startup'

    def test_hooks_reference_correct_script_paths(self, tmp_path):
        config = _build_hooks_config(tmp_path)
        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'

        start_cmd = config['SessionStart'][0]['hooks'][0]['command']
        assert start_cmd == str(hooks_dir / 'on_session_start.sh')

        compact_cmd = config['PreCompact'][0]['hooks'][0]['command']
        assert compact_cmd == str(hooks_dir / 'on_pre_compact.sh')

    def test_session_end_references_correct_script(self, tmp_path):
        config = _build_hooks_config(tmp_path, include_session_end=True)
        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'
        end_cmd = config['SessionEnd'][0]['hooks'][0]['command']
        assert end_cmd == str(hooks_dir / 'on_session_end.sh')

    def test_hook_entries_have_command_type(self, tmp_path):
        config = _build_hooks_config(tmp_path, include_session_end=True)
        for event_hooks in config.values():
            for entry in event_hooks:
                for hook in entry['hooks']:
                    assert hook['type'] == 'command'


# ===========================================================================
# Unit tests — _merge_settings_local
# ===========================================================================


class TestMergeSettingsLocal:
    """Verify settings.local.json merge behaviour."""

    def test_creates_fresh_when_no_file(self, tmp_path):
        settings_path = tmp_path / 'settings.local.json'
        hooks: dict[str, list] = {'SessionStart': []}
        result = _merge_settings_local(settings_path, hooks)
        assert result == {'hooks': {'SessionStart': []}}

    def test_preserves_existing_keys(self, tmp_path):
        settings_path = tmp_path / 'settings.local.json'
        settings_path.write_text(
            json.dumps(
                {
                    'permissions': {'allow': ['Bash']},
                    'enableAllProjectMcpServers': True,
                }
            )
        )
        hooks: dict[str, list] = {'SessionStart': []}
        result = _merge_settings_local(settings_path, hooks)
        assert result['permissions'] == {'allow': ['Bash']}
        assert result['enableAllProjectMcpServers'] is True
        assert result['hooks'] == {'SessionStart': []}

    def test_replaces_existing_hooks(self, tmp_path):
        settings_path = tmp_path / 'settings.local.json'
        settings_path.write_text(json.dumps({'hooks': {'OldHook': []}}))
        new_hooks = {'SessionStart': [{'matcher': 'startup', 'hooks': []}]}
        result = _merge_settings_local(settings_path, new_hooks)
        assert result['hooks'] == new_hooks
        assert 'OldHook' not in result['hooks']

    def test_handles_malformed_json(self, tmp_path):
        settings_path = tmp_path / 'settings.local.json'
        settings_path.write_text('not valid json {{{')
        hooks: dict[str, list] = {'SessionStart': []}
        result = _merge_settings_local(settings_path, hooks)
        assert result == {'hooks': {'SessionStart': []}}


# ===========================================================================
# Unit tests — _load_hook_template
# ===========================================================================


class TestLoadHookTemplate:
    """Verify hook template loading and placeholder replacement."""

    def test_replaces_project_dir_placeholder(self, tmp_path):
        content = _load_hook_template('on_pre_compact.sh', tmp_path)
        assert '__PROJECT_DIR__' not in content
        assert str(tmp_path) in content

    def test_session_start_has_no_placeholder(self):
        """on_session_start.sh has no __PROJECT_DIR__ placeholder."""
        import pathlib

        content = _load_hook_template('on_session_start.sh', pathlib.Path('/fake'))
        # Should still be valid — just no replacement needed
        assert '#!/usr/bin/env bash' in content

    def test_session_end_replaces_placeholder(self, tmp_path):
        content = _load_hook_template('on_session_end.sh', tmp_path)
        assert '__PROJECT_DIR__' not in content
        assert str(tmp_path) in content


# ===========================================================================
# CLI integration tests — hook file creation
# ===========================================================================


class TestSetupCreatesHooks:
    """Verify that the setup command creates hook files correctly."""

    def test_creates_hook_scripts(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path))
        assert result.exit_code == 0

        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'
        assert (hooks_dir / 'on_session_start.sh').exists()
        assert (hooks_dir / 'on_pre_compact.sh').exists()
        # SessionEnd not created by default
        assert not (hooks_dir / 'on_session_end.sh').exists()

    def test_creates_state_directory(self, tmp_path):
        _invoke('--project-dir', str(tmp_path))
        state_dir = tmp_path / '.claude' / 'hooks' / 'memex' / '.state'
        assert state_dir.is_dir()

    def test_hook_scripts_are_executable(self, tmp_path):
        import os

        _invoke('--project-dir', str(tmp_path))
        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'
        for script in ('on_session_start.sh', 'on_pre_compact.sh'):
            assert os.access(hooks_dir / script, os.X_OK)

    def test_placeholder_replaced_in_scripts(self, tmp_path):
        _invoke('--project-dir', str(tmp_path))
        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'
        for script in ('on_pre_compact.sh',):
            content = (hooks_dir / script).read_text()
            assert '__PROJECT_DIR__' not in content
            assert str(tmp_path) in content

    def test_settings_local_json_created(self, tmp_path):
        _invoke('--project-dir', str(tmp_path))
        settings_path = tmp_path / '.claude' / 'settings.local.json'
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert 'hooks' in data
        assert 'SessionStart' in data['hooks']
        assert 'PreCompact' in data['hooks']

    def test_settings_local_preserves_existing_keys(self, tmp_path):
        settings_path = tmp_path / '.claude' / 'settings.local.json'
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({'permissions': {'allow': ['Bash']}, 'enableAllProjectMcpServers': True})
        )

        _invoke('--project-dir', str(tmp_path))

        data = json.loads(settings_path.read_text())
        assert data['permissions'] == {'allow': ['Bash']}
        assert data['enableAllProjectMcpServers'] is True
        assert 'hooks' in data

    def test_no_hooks_flag_skips_generation(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path), '--no-hooks')
        assert result.exit_code == 0
        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'
        assert not hooks_dir.exists()
        # settings.local.json should not be created by hooks step
        settings_path = tmp_path / '.claude' / 'settings.local.json'
        assert not settings_path.exists()

    def test_with_session_tracking_creates_session_end(self, tmp_path):
        result = _invoke('--project-dir', str(tmp_path), '--with-session-tracking')
        assert result.exit_code == 0
        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'
        assert (hooks_dir / 'on_session_end.sh').exists()

        data = json.loads((tmp_path / '.claude' / 'settings.local.json').read_text())
        assert 'SessionEnd' in data['hooks']

    def test_with_session_tracking_script_executable(self, tmp_path):
        import os

        _invoke('--project-dir', str(tmp_path), '--with-session-tracking')
        script = tmp_path / '.claude' / 'hooks' / 'memex' / 'on_session_end.sh'
        assert os.access(script, os.X_OK)

    def test_skips_existing_hooks_without_force(self, tmp_path):
        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'
        hooks_dir.mkdir(parents=True)
        script = hooks_dir / 'on_session_start.sh'
        script.write_text('custom hook')

        _invoke('--project-dir', str(tmp_path))

        assert script.read_text() == 'custom hook'

    def test_force_overwrites_existing_hooks(self, tmp_path):
        hooks_dir = tmp_path / '.claude' / 'hooks' / 'memex'
        hooks_dir.mkdir(parents=True)
        script = hooks_dir / 'on_session_start.sh'
        script.write_text('custom hook')

        _invoke('--project-dir', str(tmp_path), '--force')

        content = script.read_text()
        assert content != 'custom hook'
        assert '#!/usr/bin/env bash' in content

    def test_rerun_overwrites_settings_hooks(self, tmp_path):
        """Re-running always updates the hooks section in settings.local.json."""
        _invoke('--project-dir', str(tmp_path))
        _invoke('--project-dir', str(tmp_path), '--with-session-tracking')

        data = json.loads((tmp_path / '.claude' / 'settings.local.json').read_text())
        assert 'SessionEnd' in data['hooks']


# ===========================================================================
# Unit tests — hook script syntax validation
# ===========================================================================


class TestHookScriptSyntax:
    """Verify generated hook scripts pass bash syntax checks."""

    def test_session_start_passes_bash_syntax(self, tmp_path):
        import subprocess

        _invoke('--project-dir', str(tmp_path))
        script = tmp_path / '.claude' / 'hooks' / 'memex' / 'on_session_start.sh'
        result = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f'Syntax error: {result.stderr}'

    def test_pre_compact_passes_bash_syntax(self, tmp_path):
        import subprocess

        _invoke('--project-dir', str(tmp_path))
        script = tmp_path / '.claude' / 'hooks' / 'memex' / 'on_pre_compact.sh'
        result = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f'Syntax error: {result.stderr}'

    def test_session_end_passes_bash_syntax(self, tmp_path):
        import subprocess

        _invoke('--project-dir', str(tmp_path), '--with-session-tracking')
        script = tmp_path / '.claude' / 'hooks' / 'memex' / 'on_session_end.sh'
        result = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f'Syntax error: {result.stderr}'


# ===========================================================================
# CLI integration test — lazy subcommand registration
# ===========================================================================


class TestLazyRegistration:
    """Verify the ``setup`` subcommand is registered in LAZY_SUBCOMMANDS."""

    def test_setup_in_lazy_subcommands(self):
        from memex_cli.utils import LAZY_SUBCOMMANDS

        assert 'setup' in LAZY_SUBCOMMANDS
        assert LAZY_SUBCOMMANDS['setup'] == 'memex_cli.setup_claude_code:app'

    def test_main_app_lists_setup(self):
        from memex_cli import app as main_app

        result = runner.invoke(main_app, ['setup', '--help'])
        assert result.exit_code == 0
        assert 'Claude Code' in result.output or 'claude-code' in result.output.lower()
