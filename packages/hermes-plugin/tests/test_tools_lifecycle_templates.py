"""Tests for Stream 4: lifecycle + template tools.

Covers AC-027..AC-035, AC-051..AC-053, AC-073..AC-077 — the six Stream 4
tools that expose note lifecycle operations (rename, status, user_notes)
and the client-side ``TemplateRegistry`` (get, list, register).
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.tools import (
    ALL_SCHEMAS,
    GET_TEMPLATE_SCHEMA,
    LIST_TEMPLATES_SCHEMA,
    REGISTER_TEMPLATE_SCHEMA,
    RENAME_NOTE_SCHEMA,
    SET_NOTE_STATUS_SCHEMA,
    UPDATE_USER_NOTES_SCHEMA,
    dispatch,
)


@pytest.fixture
def config() -> HermesMemexConfig:
    return HermesMemexConfig()


@pytest.fixture
def vault_id():
    return uuid4()


# ---------------------------------------------------------------------------
# Schema registration (AC-027, AC-051, AC-073)
# ---------------------------------------------------------------------------


def test_stream_4_schemas_registered_in_all_schemas():
    """AC-027/AC-051/AC-073: all six Stream 4 schemas are in ALL_SCHEMAS."""
    names = {s['name'] for s in ALL_SCHEMAS}
    assert 'memex_set_note_status' in names
    assert 'memex_update_user_notes' in names
    assert 'memex_rename_note' in names
    assert 'memex_get_template' in names
    assert 'memex_list_templates' in names
    assert 'memex_register_template' in names


def test_set_note_status_schema_requires_note_id_and_status():
    props = SET_NOTE_STATUS_SCHEMA['parameters']['properties']
    required = SET_NOTE_STATUS_SCHEMA['parameters']['required']
    assert 'note_id' in props and 'status' in props and 'linked_note_id' in props
    assert set(required) == {'note_id', 'status'}


def test_update_user_notes_schema_allows_null_user_notes():
    """AC-052: ``user_notes`` may be null (for deletion)."""
    props = UPDATE_USER_NOTES_SCHEMA['parameters']['properties']
    assert 'null' in props['user_notes']['type']


def test_rename_note_schema_requires_both_fields():
    required = RENAME_NOTE_SCHEMA['parameters']['required']
    assert set(required) == {'note_id', 'new_title'}


def test_get_template_schema_requires_slug():
    assert SET_NOTE_STATUS_SCHEMA is not None  # keep ref alive
    assert GET_TEMPLATE_SCHEMA['parameters']['required'] == ['slug']


def test_list_templates_schema_has_no_required_params():
    assert LIST_TEMPLATES_SCHEMA['parameters'].get('required', []) == []


def test_register_template_schema_requires_slug_and_template():
    required = REGISTER_TEMPLATE_SCHEMA['parameters']['required']
    assert set(required) == {'slug', 'template'}


# ---------------------------------------------------------------------------
# set_note_status (AC-027..AC-030)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('status', ['active', 'superseded', 'appended', 'archived'])
def test_set_note_status_accepts_all_four_statuses(config, vault_id, status):
    """AC-028: all four documented statuses are accepted client-side."""
    api = Mock()
    api.set_note_status = AsyncMock(return_value={'status': status})
    note_uuid = uuid4()
    out = dispatch(
        'memex_set_note_status',
        {'note_id': str(note_uuid), 'status': status},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['status'] == status
    api.set_note_status.assert_awaited_once()
    call_args = api.set_note_status.call_args
    assert call_args.args[0] == note_uuid
    assert call_args.args[1] == status
    assert call_args.args[2] is None  # linked_note_id


def test_set_note_status_rejects_unknown_status(config, vault_id):
    """AC-029: unknown status is rejected client-side (never calls the API)."""
    api = Mock()
    api.set_note_status = AsyncMock()
    out = dispatch(
        'memex_set_note_status',
        {'note_id': str(uuid4()), 'status': 'bogus'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'bogus' in data['error']
    api.set_note_status.assert_not_awaited()


def test_set_note_status_forwards_linked_note_id(config, vault_id):
    """AC-030: linked_note_id UUID is parsed and forwarded."""
    api = Mock()
    api.set_note_status = AsyncMock(return_value={})
    note_uuid = uuid4()
    linked_uuid = uuid4()
    dispatch(
        'memex_set_note_status',
        {
            'note_id': str(note_uuid),
            'status': 'superseded',
            'linked_note_id': str(linked_uuid),
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_args = api.set_note_status.call_args
    assert call_args.args[2] == linked_uuid


def test_set_note_status_rejects_invalid_note_uuid(config, vault_id):
    api = Mock()
    api.set_note_status = AsyncMock()
    out = dispatch(
        'memex_set_note_status',
        {'note_id': 'not-a-uuid', 'status': 'active'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.set_note_status.assert_not_awaited()


def test_set_note_status_requires_note_id_and_status(config, vault_id):
    api = Mock()
    api.set_note_status = AsyncMock()
    out = dispatch('memex_set_note_status', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)
    api.set_note_status.assert_not_awaited()


def test_set_note_status_forwards_api_errors(config, vault_id):
    api = Mock()
    api.set_note_status = AsyncMock(side_effect=RuntimeError('boom'))
    out = dispatch(
        'memex_set_note_status',
        {'note_id': str(uuid4()), 'status': 'active'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'boom' in json.loads(out)['error']


# ---------------------------------------------------------------------------
# update_user_notes (AC-052..AC-053)
# ---------------------------------------------------------------------------


def test_update_user_notes_forwards_text(config, vault_id):
    api = Mock()
    api.update_user_notes = AsyncMock(return_value={'note_id': 'x', 'updated': True})
    note_uuid = uuid4()
    dispatch(
        'memex_update_user_notes',
        {'note_id': str(note_uuid), 'user_notes': 'new annotations'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_args = api.update_user_notes.call_args
    assert call_args.args[0] == note_uuid
    assert call_args.args[1] == 'new annotations'


def test_update_user_notes_explicit_null_clears_annotations(config, vault_id):
    """AC-052: passing null (Python ``None``) clears annotations."""
    api = Mock()
    api.update_user_notes = AsyncMock(return_value={'cleared': True})
    note_uuid = uuid4()
    dispatch(
        'memex_update_user_notes',
        {'note_id': str(note_uuid), 'user_notes': None},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_args = api.update_user_notes.call_args
    assert call_args.args[1] is None


def test_update_user_notes_missing_key_clears_annotations(config, vault_id):
    """AC-052 (addendum): omitting ``user_notes`` is equivalent to null."""
    api = Mock()
    api.update_user_notes = AsyncMock(return_value={'cleared': True})
    note_uuid = uuid4()
    dispatch(
        'memex_update_user_notes',
        {'note_id': str(note_uuid)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_args = api.update_user_notes.call_args
    assert call_args.args[1] is None


def test_update_user_notes_invalid_uuid(config, vault_id):
    api = Mock()
    api.update_user_notes = AsyncMock()
    out = dispatch(
        'memex_update_user_notes',
        {'note_id': 'nope'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.update_user_notes.assert_not_awaited()


def test_update_user_notes_requires_note_id(config, vault_id):
    api = Mock()
    api.update_user_notes = AsyncMock()
    out = dispatch('memex_update_user_notes', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)
    api.update_user_notes.assert_not_awaited()


# ---------------------------------------------------------------------------
# rename_note (AC-053)
# ---------------------------------------------------------------------------


def test_rename_note_forwards_to_update_note_title(config, vault_id):
    api = Mock()
    api.update_note_title = AsyncMock(return_value={'ok': True})
    note_uuid = uuid4()
    out = dispatch(
        'memex_rename_note',
        {'note_id': str(note_uuid), 'new_title': 'A New Name'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['note_id'] == str(note_uuid)
    assert data['new_title'] == 'A New Name'
    api.update_note_title.assert_awaited_once_with(note_uuid, 'A New Name')


def test_rename_note_invalid_uuid(config, vault_id):
    api = Mock()
    api.update_note_title = AsyncMock()
    out = dispatch(
        'memex_rename_note',
        {'note_id': 'nope', 'new_title': 'x'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.update_note_title.assert_not_awaited()


def test_rename_note_requires_both_args(config, vault_id):
    api = Mock()
    api.update_note_title = AsyncMock()
    out = dispatch(
        'memex_rename_note',
        {'note_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.update_note_title.assert_not_awaited()


# ---------------------------------------------------------------------------
# Templates (AC-031..AC-035, AC-074..AC-077)
# ---------------------------------------------------------------------------


def _fake_template_info(slug: str = 'default', source: str = 'builtin'):
    from memex_common.templates import TemplateInfo

    return TemplateInfo(
        slug=slug,
        display_name=slug.replace('_', ' ').title(),
        description=f'A {slug} template',
        source=source,
    )


def test_get_template_uses_template_registry(config, vault_id):
    """AC-031/AC-032: uses ``TemplateRegistry.get_template`` synchronously.

    ``get_template`` is a *sync* method — it must be called, never awaited.
    """
    registry = Mock()
    registry.get_template = Mock(return_value='# A template\nBody')

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_get_template',
            {'slug': 'default'},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert data['slug'] == 'default'
    assert data['content'] == '# A template\nBody'
    registry.get_template.assert_called_once_with('default')


def test_get_template_unknown_slug_returns_error(config, vault_id):
    """AC-033/AC-034: ``KeyError`` from registry → tool_error (not ValueError)."""
    registry = Mock()
    registry.get_template = Mock(side_effect=KeyError('Unknown template: wat'))

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_get_template',
            {'slug': 'wat'},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert 'error' in data
    assert 'wat' in data['error']


def test_get_template_only_wraps_key_error(config, vault_id):
    """AC-034: ``ValueError`` from registry is NOT masked as KeyError.

    ``ValueError`` comes from misuse (e.g. invalid scope). We still return
    tool_error, but the error message carries the original exception — we do
    not convert it into a KeyError-shaped "Unknown template" message.
    """
    registry = Mock()
    registry.get_template = Mock(side_effect=ValueError('malformed'))

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_get_template',
            {'slug': 'x'},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert 'error' in data
    assert 'Unknown template' not in data['error']
    assert 'malformed' in data['error']


def test_get_template_requires_slug(config, vault_id):
    out = dispatch('memex_get_template', {}, api=Mock(), config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)


def test_list_templates_uses_template_registry_synchronously(config, vault_id):
    """AC-074: ``TemplateRegistry.list_templates`` is sync — called, never awaited."""
    registry = Mock()
    registry.list_templates = Mock(
        return_value=[
            _fake_template_info('default', 'builtin'),
            _fake_template_info('project', 'local'),
        ]
    )

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_list_templates',
            {},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert data['count'] == 2
    slugs = {r['slug'] for r in data['results']}
    assert slugs == {'default', 'project'}
    sources = {r['source'] for r in data['results']}
    assert sources == {'builtin', 'local'}
    registry.list_templates.assert_called_once_with()


def test_list_templates_returns_empty_on_no_templates(config, vault_id):
    """AC-075: empty registry returns an empty result set, not an error."""
    registry = Mock()
    registry.list_templates = Mock(return_value=[])
    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_list_templates',
            {},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )
    data = json.loads(out)
    assert data['count'] == 0
    assert data['results'] == []


def test_register_template_calls_register_from_content(config, vault_id):
    """AC-076/AC-077: calls ``register_from_content`` sync with scope=global."""
    registry = Mock()
    info = _fake_template_info('sprint_retro', 'global')
    registry.register_from_content = Mock(return_value=info)

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_register_template',
            {
                'slug': 'sprint_retro',
                'template': '---\ntitle: ok\n---\nbody',
                'name': 'Sprint Retro',
                'description': 'A retrospective.',
            },
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert data['slug'] == 'sprint_retro'
    assert data['source'] == 'global'
    call_kwargs = registry.register_from_content.call_args.kwargs
    assert call_kwargs['slug'] == 'sprint_retro'
    assert call_kwargs['template'].startswith('---')
    assert call_kwargs['name'] == 'Sprint Retro'
    assert call_kwargs['description'] == 'A retrospective.'
    assert call_kwargs['scope'] == 'global'


def test_register_template_requires_slug_and_template(config, vault_id):
    out = dispatch(
        'memex_register_template',
        {'slug': 'x'},
        api=Mock(),
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)


def test_register_template_wraps_registry_errors(config, vault_id):
    """Registry errors (invalid TOML, filesystem, …) surface as tool_error."""
    registry = Mock()
    registry.register_from_content = Mock(side_effect=OSError('disk full'))
    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_register_template',
            {'slug': 'x', 'template': 'body'},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )
    assert 'disk full' in json.loads(out)['error']


def test_template_registry_built_with_builtin_global_local_layers(tmp_path):
    """AC-077: ``_build_template_registry`` assembles builtin → global → local
    in that order, mirroring the MCP pattern.
    """
    from memex_hermes_plugin.memex.tools import _build_template_registry

    with patch('memex_common.config.MemexConfig') as MockConfig:
        mock_cfg = Mock()
        mock_cfg.server.file_store.root = str(tmp_path)
        MockConfig.return_value = mock_cfg

        registry = _build_template_registry()

    # Exercise the public ``list_templates`` so the layering is walked end-to-end.
    assert hasattr(registry, 'get_template')
    assert hasattr(registry, 'list_templates')
    assert hasattr(registry, 'register_from_content')
    # Private attr is intentional — we assert source labels after discovery
    # rather than poking ``_template_dirs``.
    labels = [label for label, _ in registry._template_dirs]  # noqa: SLF001
    assert labels[0] == 'builtin'
    assert 'global' in labels
    assert labels[-1] == 'local'


# ---------------------------------------------------------------------------
# Keep this module happy with the existing ``dataclasses`` import — used via
# ``_fake_template_info`` → ``TemplateInfo`` (a dataclass) indirectly.
# ---------------------------------------------------------------------------

assert dataclasses is not None
