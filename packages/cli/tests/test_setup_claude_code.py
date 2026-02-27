"""Tests for the ``memex setup claude-code`` CLI command."""

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from memex_cli.setup_claude_code import (
    CLAUDE_MD_MARKER,
    _load_template,
    _mcp_server_entry,
    app,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helper to invoke the setup command (shorthand used by most tests)
# ---------------------------------------------------------------------------


def _invoke(*args: str, **kwargs):
    return runner.invoke(app, list(args), catch_exceptions=False, **kwargs)


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
