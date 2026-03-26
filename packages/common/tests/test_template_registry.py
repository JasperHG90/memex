"""Tests for the pluggable template registry."""

import pathlib

import pytest
import tomli_w

from memex_common.templates import (
    NoteTemplateType,
    TemplateInfo,
    TemplateRegistry,
    BUILTIN_PROMPTS_DIR,
    _slug_from_filename,
    get_template,
    list_template_types,
    list_templates,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BUILTIN_SLUGS = {
    'general_note',
    'technical_brief',
    'architectural_decision_record',
    'request_for_comments',
    'quick_note',
}


def _write_toml_template(
    directory: pathlib.Path,
    slug: str,
    template: str = '---\ndate: 2025-01-01\n---\n# Test',
    name: str | None = None,
    description: str | None = None,
) -> pathlib.Path:
    """Write a .toml template file to a directory."""
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
# _slug_from_filename
# ---------------------------------------------------------------------------


class TestSlugFromFilename:
    def test_simple(self) -> None:
        assert _slug_from_filename('general_note.toml') == 'general_note'

    def test_hyphens_to_underscores(self) -> None:
        assert _slug_from_filename('sprint-retro.toml') == 'sprint_retro'

    def test_uppercase_lowered(self) -> None:
        assert _slug_from_filename('My-Template.toml') == 'my_template'


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------


class TestBuiltinTemplates:
    @pytest.fixture()
    def registry(self) -> TemplateRegistry:
        return TemplateRegistry([('builtin', BUILTIN_PROMPTS_DIR)])

    def test_discovers_all_builtins(self, registry: TemplateRegistry) -> None:
        templates = registry.list_templates()
        slugs = {t.slug for t in templates}
        assert slugs == BUILTIN_SLUGS

    def test_all_builtins_have_content(self, registry: TemplateRegistry) -> None:
        for slug in BUILTIN_SLUGS:
            content = registry.get_template(slug)
            assert len(content) > 0

    def test_builtin_source_label(self, registry: TemplateRegistry) -> None:
        for t in registry.list_templates():
            assert t.source == 'builtin'

    def test_builtin_metadata(self, registry: TemplateRegistry) -> None:
        info = registry.get_template_info('general_note')
        assert info.display_name == 'General Note'
        assert 'capturing' in info.description.lower() or len(info.description) > 0

    def test_builtin_templates_have_frontmatter(self, registry: TemplateRegistry) -> None:
        """All built-in templates should include YAML frontmatter scaffolding."""
        for slug in BUILTIN_SLUGS:
            content = registry.get_template(slug)
            assert content.lstrip('\n').startswith('---'), (
                f'Built-in template {slug!r} is missing frontmatter'
            )

    def test_unknown_slug_raises(self, registry: TemplateRegistry) -> None:
        with pytest.raises(KeyError, match='nonexistent'):
            registry.get_template('nonexistent')

    def test_unknown_slug_info_raises(self, registry: TemplateRegistry) -> None:
        with pytest.raises(KeyError, match='nonexistent'):
            registry.get_template_info('nonexistent')


# ---------------------------------------------------------------------------
# Multi-layer discovery
# ---------------------------------------------------------------------------


class TestMultiLayerDiscovery:
    def test_user_templates_discovered(self, tmp_path: pathlib.Path) -> None:
        user_dir = tmp_path / 'global'
        _write_toml_template(
            user_dir, 'sprint_retro', name='Sprint Retro', description='Retro template'
        )
        registry = TemplateRegistry(
            [
                ('builtin', BUILTIN_PROMPTS_DIR),
                ('global', user_dir),
            ]
        )
        slugs = {t.slug for t in registry.list_templates()}
        assert 'sprint_retro' in slugs
        assert BUILTIN_SLUGS.issubset(slugs)

    def test_user_template_overrides_builtin(self, tmp_path: pathlib.Path) -> None:
        user_dir = tmp_path / 'global'
        custom_content = '---\ndate: 2025-01-01\n---\n# My Custom General Note'
        _write_toml_template(
            user_dir,
            'general_note',
            template=custom_content,
            name='My General Note',
        )
        registry = TemplateRegistry(
            [
                ('builtin', BUILTIN_PROMPTS_DIR),
                ('global', user_dir),
            ]
        )
        content = registry.get_template('general_note')
        assert content == custom_content

        info = registry.get_template_info('general_note')
        assert info.source == 'global'
        assert info.display_name == 'My General Note'

    def test_local_overrides_global(self, tmp_path: pathlib.Path) -> None:
        global_dir = tmp_path / 'global'
        local_dir = tmp_path / 'local'
        _write_toml_template(global_dir, 'sprint_retro', template='# Global version')
        _write_toml_template(local_dir, 'sprint_retro', template='# Local version')

        registry = TemplateRegistry(
            [
                ('builtin', BUILTIN_PROMPTS_DIR),
                ('global', global_dir),
                ('local', local_dir),
            ]
        )
        content = registry.get_template('sprint_retro')
        assert content == '# Local version'

        info = registry.get_template_info('sprint_retro')
        assert info.source == 'local'

    def test_three_layers_priority(self, tmp_path: pathlib.Path) -> None:
        """local > global > builtin for the same slug."""
        global_dir = tmp_path / 'global'
        local_dir = tmp_path / 'local'
        _write_toml_template(global_dir, 'general_note', template='# Global override')
        _write_toml_template(local_dir, 'general_note', template='# Local override')

        registry = TemplateRegistry(
            [
                ('builtin', BUILTIN_PROMPTS_DIR),
                ('global', global_dir),
                ('local', local_dir),
            ]
        )
        assert registry.get_template('general_note') == '# Local override'
        assert registry.get_template_info('general_note').source == 'local'


# ---------------------------------------------------------------------------
# TOML parsing edge cases
# ---------------------------------------------------------------------------


class TestTomlParsing:
    def test_metadata_auto_derived(self, tmp_path: pathlib.Path) -> None:
        """Templates without name/description get auto-derived metadata."""
        _write_toml_template(tmp_path, 'my_template')
        registry = TemplateRegistry([('user', tmp_path)])
        info = registry.get_template_info('my_template')
        assert info.display_name == 'My Template'
        assert info.description == 'User-defined template'

    def test_non_toml_files_ignored(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / 'readme.md').write_text('# Not a template')
        (tmp_path / 'notes.txt').write_text('not a template')
        _write_toml_template(tmp_path, 'real_template')
        registry = TemplateRegistry([('user', tmp_path)])
        slugs = {t.slug for t in registry.list_templates()}
        assert slugs == {'real_template'}

    def test_invalid_toml_skipped(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / 'bad.toml').write_text('this is not valid toml {{{}')
        _write_toml_template(tmp_path, 'good')
        registry = TemplateRegistry([('user', tmp_path)])
        slugs = {t.slug for t in registry.list_templates()}
        assert slugs == {'good'}

    def test_missing_template_field_skipped(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / 'no_template.toml').write_text('name = "Oops"\n')
        _write_toml_template(tmp_path, 'good')
        registry = TemplateRegistry([('user', tmp_path)])
        slugs = {t.slug for t in registry.list_templates()}
        assert slugs == {'good'}

    def test_missing_directory_handled(self) -> None:
        registry = TemplateRegistry(
            [
                ('builtin', BUILTIN_PROMPTS_DIR),
                ('global', pathlib.Path('/nonexistent/path/templates')),
            ]
        )
        # Should not crash, just return built-ins
        templates = registry.list_templates()
        assert len(templates) == len(BUILTIN_SLUGS)

    def test_empty_directory(self, tmp_path: pathlib.Path) -> None:
        empty_dir = tmp_path / 'empty'
        empty_dir.mkdir()
        registry = TemplateRegistry([('user', empty_dir)])
        assert registry.list_templates() == []


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_copies_file(self, tmp_path: pathlib.Path) -> None:
        source_dir = tmp_path / 'source'
        target_dir = tmp_path / 'target'
        source_path = _write_toml_template(
            source_dir, 'sprint_retro', name='Sprint Retro', description='Retro notes'
        )
        registry = TemplateRegistry([('global', target_dir)])
        info = registry.register(source_path, scope='global')

        assert info.slug == 'sprint_retro'
        assert info.display_name == 'Sprint Retro'
        assert info.source == 'global'
        assert (target_dir / 'sprint_retro.toml').exists()

    def test_register_creates_directory(self, tmp_path: pathlib.Path) -> None:
        source_dir = tmp_path / 'source'
        target_dir = tmp_path / 'target' / 'nested'
        source_path = _write_toml_template(source_dir, 'test')
        registry = TemplateRegistry([('global', target_dir)])
        registry.register(source_path, scope='global')
        assert (target_dir / 'test.toml').exists()

    def test_register_invalid_file_raises(self, tmp_path: pathlib.Path) -> None:
        target_dir = tmp_path / 'target'
        bad_file = tmp_path / 'bad.toml'
        bad_file.write_text('not valid toml {{{}')
        registry = TemplateRegistry([('global', target_dir)])
        with pytest.raises(ValueError, match='Invalid template file'):
            registry.register(bad_file, scope='global')

    def test_register_from_content(self, tmp_path: pathlib.Path) -> None:
        target_dir = tmp_path / 'target'
        registry = TemplateRegistry([('global', target_dir)])
        info = registry.register_from_content(
            slug='daily_standup',
            template='---\ndate: 2025-01-01\n---\n# Standup',
            name='Daily Standup',
            description='Daily standup notes',
            scope='global',
        )
        assert info.slug == 'daily_standup'
        assert info.display_name == 'Daily Standup'
        assert info.source == 'global'
        assert (target_dir / 'daily_standup.toml').exists()

        # Verify the file is valid TOML and round-trips
        content = registry.get_template('daily_standup')
        assert '# Standup' in content

    def test_register_from_content_auto_metadata(self, tmp_path: pathlib.Path) -> None:
        registry = TemplateRegistry([('global', tmp_path)])
        info = registry.register_from_content(
            slug='my_template',
            template='---\ndate: 2025-01-01\n---\n# Hello',
        )
        assert info.display_name == 'My Template'
        assert info.description == 'User-defined template'


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_file(self, tmp_path: pathlib.Path) -> None:
        _write_toml_template(tmp_path, 'sprint_retro')
        registry = TemplateRegistry([('global', tmp_path)])
        assert (tmp_path / 'sprint_retro.toml').exists()

        registry.delete('sprint_retro', scope='global')
        assert not (tmp_path / 'sprint_retro.toml').exists()

    def test_delete_builtin_raises(self, tmp_path: pathlib.Path) -> None:
        registry = TemplateRegistry([('builtin', BUILTIN_PROMPTS_DIR)])
        with pytest.raises(ValueError, match='Cannot delete built-in'):
            registry.delete('general_note', scope='builtin')

    def test_delete_nonexistent_raises(self, tmp_path: pathlib.Path) -> None:
        registry = TemplateRegistry([('global', tmp_path)])
        with pytest.raises(KeyError, match='not found'):
            registry.delete('nonexistent', scope='global')

    def test_delete_unknown_scope_raises(self, tmp_path: pathlib.Path) -> None:
        registry = TemplateRegistry([('global', tmp_path)])
        with pytest.raises(ValueError, match='Unknown scope'):
            registry.delete('foo', scope='nonexistent')


# ---------------------------------------------------------------------------
# Frontmatter warning
# ---------------------------------------------------------------------------


class TestFrontmatterWarning:
    def test_no_frontmatter_warns(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = TemplateRegistry([('global', tmp_path)])
        with caplog.at_level('WARNING'):
            registry.register_from_content(slug='no_fm', template='# No frontmatter here')
        assert 'no frontmatter' in caplog.text.lower()

    def test_with_frontmatter_no_warning(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = TemplateRegistry([('global', tmp_path)])
        with caplog.at_level('WARNING'):
            registry.register_from_content(
                slug='has_fm', template='---\ndate: 2025-01-01\n---\n# Has FM'
            )
        assert 'no frontmatter' not in caplog.text.lower()


# ---------------------------------------------------------------------------
# Backward compat (module-level functions)
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_get_template_with_enum(self) -> None:
        content = get_template(NoteTemplateType.GENERAL_NOTE)
        assert len(content) > 0
        assert '---' in content

    def test_get_template_with_string(self) -> None:
        content = get_template('general_note')
        assert len(content) > 0

    def test_list_template_types(self) -> None:
        slugs = list_template_types()
        assert set(slugs) == BUILTIN_SLUGS

    def test_list_templates(self) -> None:
        templates = list_templates()
        assert len(templates) == len(BUILTIN_SLUGS)
        assert all(isinstance(t, TemplateInfo) for t in templates)
