"""E2E tests for the `memex note template` CLI sub-group."""

import pathlib

import tomli_w
import pytest
from typer.testing import CliRunner

from memex_cli.notes import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml_template(
    directory: pathlib.Path,
    slug: str,
    template: str = '---\ndate: 2025-01-01\n---\n# Test',
    name: str | None = None,
    description: str | None = None,
) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    data: dict[str, str] = {}
    if name:
        data['name'] = name
    if description:
        data['description'] = description
    data['template'] = template
    path = directory / f'{slug}.toml'
    path.write_bytes(tomli_w.dumps(data).encode('utf-8'))
    return path


# ---------------------------------------------------------------------------
# template list
# ---------------------------------------------------------------------------


class TestTemplateList:
    def test_lists_builtin_templates(self, runner: CliRunner, mock_config) -> None:
        result = runner.invoke(app, ['template', 'list'], obj=mock_config)
        assert result.exit_code == 0
        assert 'general_note' in result.stdout
        assert 'technical_brief' in result.stdout
        # Rich table wraps long text; check for a substring that won't be split
        assert 'architectural_decis' in result.stdout
        assert 'request_for_comments' in result.stdout
        assert 'quick_note' in result.stdout

    def test_list_shows_source_column(self, runner: CliRunner, mock_config) -> None:
        result = runner.invoke(app, ['template', 'list'], obj=mock_config)
        assert result.exit_code == 0
        assert 'builtin' in result.stdout

    def test_list_shows_user_templates(
        self, runner: CliRunner, mock_config, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """User templates in the global dir appear in the list."""
        templates_dir = tmp_path / 'templates'
        _write_toml_template(templates_dir, 'sprint_retro', name='Sprint Retro')

        # Point the filestore root to tmp_path so global = tmp_path/templates
        mock_config.server.file_store.root = str(tmp_path)
        result = runner.invoke(app, ['template', 'list'], obj=mock_config)
        assert result.exit_code == 0
        assert 'sprint_retro' in result.stdout
        assert 'Sprint Retro' in result.stdout


# ---------------------------------------------------------------------------
# template get
# ---------------------------------------------------------------------------


class TestTemplateGet:
    def test_get_builtin_template(self, runner: CliRunner, mock_config) -> None:
        result = runner.invoke(app, ['template', 'get', 'general_note'], obj=mock_config)
        assert result.exit_code == 0
        assert 'Executive Summary' in result.stdout

    def test_get_unknown_template(self, runner: CliRunner, mock_config) -> None:
        result = runner.invoke(app, ['template', 'get', 'nonexistent'], obj=mock_config)
        assert result.exit_code == 1
        assert 'Unknown template' in result.stdout

    def test_get_user_template(
        self, runner: CliRunner, mock_config, tmp_path: pathlib.Path
    ) -> None:
        templates_dir = tmp_path / 'templates'
        _write_toml_template(
            templates_dir, 'standup', template='---\ndate: 2025-01-01\n---\n# Daily Standup'
        )
        mock_config.server.file_store.root = str(tmp_path)
        result = runner.invoke(app, ['template', 'get', 'standup'], obj=mock_config)
        assert result.exit_code == 0
        assert 'Daily Standup' in result.stdout


# ---------------------------------------------------------------------------
# template register
# ---------------------------------------------------------------------------


class TestTemplateRegister:
    def test_register_global(self, runner: CliRunner, mock_config, tmp_path: pathlib.Path) -> None:
        source_dir = tmp_path / 'source'
        source_path = _write_toml_template(
            source_dir, 'standup', name='Standup', description='Daily standup'
        )
        mock_config.server.file_store.root = str(tmp_path)

        result = runner.invoke(app, ['template', 'register', str(source_path)], obj=mock_config)
        assert result.exit_code == 0
        assert 'Registered' in result.stdout
        assert 'standup' in result.stdout
        assert (tmp_path / 'templates' / 'standup.toml').exists()

    def test_register_local(
        self, runner: CliRunner, mock_config, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        source_dir = tmp_path / 'source'
        source_path = _write_toml_template(source_dir, 'retro')

        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ['template', 'register', str(source_path), '--local'], obj=mock_config
        )
        assert result.exit_code == 0
        assert 'local' in result.stdout
        assert (tmp_path / '.memex' / 'templates' / 'retro.toml').exists()

    def test_register_non_toml_rejected(
        self, runner: CliRunner, mock_config, tmp_path: pathlib.Path
    ) -> None:
        md_file = tmp_path / 'template.md'
        md_file.write_text('# Not TOML')
        result = runner.invoke(app, ['template', 'register', str(md_file)], obj=mock_config)
        assert result.exit_code == 1
        assert '.toml' in result.stdout


# ---------------------------------------------------------------------------
# template delete
# ---------------------------------------------------------------------------


class TestTemplateDelete:
    def test_delete_with_confirmation(
        self, runner: CliRunner, mock_config, tmp_path: pathlib.Path
    ) -> None:
        templates_dir = tmp_path / 'templates'
        _write_toml_template(templates_dir, 'standup')
        mock_config.server.file_store.root = str(tmp_path)

        result = runner.invoke(app, ['template', 'delete', 'standup'], input='y\n', obj=mock_config)
        assert result.exit_code == 0
        assert 'Deleted' in result.stdout
        assert not (templates_dir / 'standup.toml').exists()

    def test_delete_cancelled(self, runner: CliRunner, mock_config, tmp_path: pathlib.Path) -> None:
        templates_dir = tmp_path / 'templates'
        _write_toml_template(templates_dir, 'standup')
        mock_config.server.file_store.root = str(tmp_path)

        result = runner.invoke(app, ['template', 'delete', 'standup'], input='n\n', obj=mock_config)
        assert result.exit_code == 0
        assert 'Cancelled' in result.stdout
        assert (templates_dir / 'standup.toml').exists()

    def test_delete_with_yes_flag(
        self, runner: CliRunner, mock_config, tmp_path: pathlib.Path
    ) -> None:
        templates_dir = tmp_path / 'templates'
        _write_toml_template(templates_dir, 'standup')
        mock_config.server.file_store.root = str(tmp_path)

        result = runner.invoke(app, ['template', 'delete', 'standup', '--yes'], obj=mock_config)
        assert result.exit_code == 0
        assert 'Deleted' in result.stdout

    def test_delete_nonexistent(self, runner: CliRunner, mock_config) -> None:
        result = runner.invoke(app, ['template', 'delete', 'nonexistent', '--yes'], obj=mock_config)
        assert result.exit_code == 1
        assert 'not found' in result.stdout

    def test_delete_builtin_rejected(self, runner: CliRunner, mock_config) -> None:
        """Cannot delete built-in templates (no 'builtin' scope accessible via --local or default)."""
        result = runner.invoke(
            app, ['template', 'delete', 'general_note', '--yes'], obj=mock_config
        )
        # Will fail because general_note.toml doesn't exist in the global scope dir
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# template dir
# ---------------------------------------------------------------------------


class TestTemplateDir:
    def test_dir_global(self, runner: CliRunner, mock_config) -> None:
        result = runner.invoke(app, ['template', 'dir'], obj=mock_config)
        assert result.exit_code == 0
        assert 'templates' in result.stdout

    def test_dir_local(self, runner: CliRunner, mock_config) -> None:
        result = runner.invoke(app, ['template', 'dir', '--local'], obj=mock_config)
        assert result.exit_code == 0
        assert '.memex' in result.stdout
        assert 'templates' in result.stdout


# ---------------------------------------------------------------------------
# S3/GCS filestore guard
# ---------------------------------------------------------------------------


class TestRemoteFilestoreGuard:
    @pytest.mark.parametrize('root', ['s3://my-bucket/memex', 'gs://my-bucket/memex'])
    def test_remote_root_skips_global(self, runner: CliRunner, mock_config, root: str) -> None:
        """When filestore root is S3/GCS, global templates are skipped (not broken)."""
        mock_config.server.file_store.root = root
        result = runner.invoke(app, ['template', 'list'], obj=mock_config)
        assert result.exit_code == 0
        # Built-ins still present
        assert 'general_note' in result.stdout

    @pytest.mark.parametrize('root', ['s3://my-bucket/memex', 'gs://my-bucket/memex'])
    def test_remote_root_dir_shows_local(self, runner: CliRunner, mock_config, root: str) -> None:
        """template dir still works (shows local) even with remote filestore."""
        mock_config.server.file_store.root = root
        result = runner.invoke(app, ['template', 'dir', '--local'], obj=mock_config)
        assert result.exit_code == 0
