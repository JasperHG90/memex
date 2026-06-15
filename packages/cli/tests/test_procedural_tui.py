"""Unit tests for the procedural-plane curation TUI.

Network-free: the controller is exercised against a fake client that
satisfies the ``ProceduralCurationClient`` Protocol, and the pure
helpers (context-key validation, version diff, chain assembly) are
tested directly. Mirrors the cockpit controller test pattern.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from memex_cli.procedural_tui.controller import (
    PIN_CAP_PER_CONTEXT,
    ChainContext,
    ProceduralCurationController,
    build_chain,
    unified_version_diff,
    validate_context_key,
)


# ---------------------------------------------------------------------------
# validate_context_key — the §19.8 pin-context grammar (no user scope)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'key',
    ['global', 'project:memex', 'project:proc-eval', 'app:claude-code', 'app:hermes:trader'],
)
def test_context_key_accepts_grammar(key: str) -> None:
    assert validate_context_key(key) == key


@pytest.mark.parametrize('key', ['user', 'user:jasper', 'random', 'project:', 'app:', ''])
def test_context_key_rejects_non_grammar(key: str) -> None:
    with pytest.raises(ValueError):
        validate_context_key(key)


def test_context_key_user_error_is_actionable() -> None:
    """The 'user' rejection names WHY — per-user curation rides app/
    project contexts, not a user one (the most likely wrong carry-over
    from KV scopes)."""
    with pytest.raises(ValueError, match='no user context'):
        validate_context_key('user')


def test_context_key_strips_whitespace() -> None:
    assert validate_context_key('  global  ') == 'global'


# ---------------------------------------------------------------------------
# build_chain — layered briefing chain, most-general first
# ---------------------------------------------------------------------------


def test_build_chain_global_only() -> None:
    assert build_chain(None, None) == ['global']


def test_build_chain_full() -> None:
    assert build_chain('memex', 'claude-code') == [
        'global',
        'project:memex',
        'app:claude-code',
    ]


def test_build_chain_app_only() -> None:
    assert build_chain(None, 'hermes:trader') == ['global', 'app:hermes:trader']


@pytest.mark.parametrize(
    ('remote', 'expected'),
    [
        ('https://github.com/acme/myapp.git', 'github.com/acme/myapp'),
        ('https://oauth2:t0k@github.com/acme/myapp', 'github.com/acme/myapp'),
        ('git@github.com:acme/myapp.git', 'github.com/acme/myapp'),
        ('ssh://git@github.com/acme/myapp.git', 'github.com/acme/myapp'),
        ('https://gitlab.com/org/subgroup/repo.git', 'gitlab.com/org/subgroup/repo'),
    ],
)
def test_normalize_git_remote_matches_plugin(remote: str, expected: str) -> None:
    """Project id derived in the TUI must match the Claude Code plugin's
    normalization, else pins land under a different project key than the
    briefing reads."""
    from memex_cli.procedural_tui.controller import _normalize_git_remote

    assert _normalize_git_remote(remote) == expected


# ---------------------------------------------------------------------------
# unified_version_diff — non-destructive ledger diff
# ---------------------------------------------------------------------------


def _ver(version: int, *, title: str, trigger: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(version=version, title=title, trigger=trigger, body=body)


def test_version_diff_detects_body_change() -> None:
    older = _ver(1, title='Deploy', trigger='deploying', body='step one')
    newer = _ver(2, title='Deploy', trigger='deploying', body='step one\nstep two')
    diff = unified_version_diff(older, newer)
    assert 'v1' in diff and 'v2' in diff
    assert 'step two' in diff


def test_version_diff_detects_trigger_change() -> None:
    older = _ver(1, title='Deploy', trigger='deploying staging', body='x')
    newer = _ver(2, title='Deploy', trigger='deploying production', body='x')
    diff = unified_version_diff(older, newer)
    assert 'production' in diff


def test_version_diff_identical_is_empty() -> None:
    older = _ver(1, title='Deploy', trigger='deploying', body='same')
    newer = _ver(2, title='Deploy', trigger='deploying', body='same')
    assert unified_version_diff(older, newer) == ''


# ---------------------------------------------------------------------------
# ChainContext — capacity badge
# ---------------------------------------------------------------------------


def test_chain_context_capacity_label() -> None:
    cc = ChainContext(context_key='global', pin_count=3)
    assert cc.capacity_label == f'3/{PIN_CAP_PER_CONTEXT}'
    assert not cc.is_full


def test_chain_context_is_full_at_cap() -> None:
    cc = ChainContext(context_key='global', pin_count=PIN_CAP_PER_CONTEXT)
    assert cc.is_full


# ---------------------------------------------------------------------------
# Controller — against a fake client (no HTTP)
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records calls; satisfies ProceduralCurationClient structurally."""

    def __init__(self) -> None:
        self.pins: list[tuple[UUID, str, int | None]] = []
        self.unpinned: list[tuple[UUID, str]] = []
        self.searched: list[str] = []
        self.rolled_back: list[tuple[UUID, int]] = []

    async def procedural_search(self, request):
        self.searched.append(request.query)
        entry = SimpleNamespace(
            id=uuid4(),
            kind='procedure',
            scope='global',
            verb='deploy',
            context='nomad',
            title='Deploy to nomad',
        )
        return SimpleNamespace(hits=[SimpleNamespace(entry=entry)])

    async def procedural_list_pins(self, context_key, *, limit=None):
        return [
            SimpleNamespace(entry_id=uuid4(), position=0, pinned_by='memex-tui'),
            SimpleNamespace(entry_id=uuid4(), position=1, pinned_by='memex-tui'),
        ]

    async def procedural_pin(self, entry_id, *, context_key, position=None, pinned_by=None):
        self.pins.append((entry_id, context_key, position))
        return SimpleNamespace(entry_id=entry_id, context_key=context_key, position=position or 0)

    async def procedural_unpin(self, entry_id, *, context_key):
        self.unpinned.append((entry_id, context_key))
        return 1

    async def procedural_briefing_cards(self, context_keys, *, scope=None, limit_per_context=5):
        return SimpleNamespace(cards=[], context_keys=context_keys, total_pinned=0)

    async def procedural_list_versions(self, entry_id):
        return [
            _ver(2, title='t', trigger='x', body='b2'),
            _ver(1, title='t', trigger='x', body='b1'),
        ]

    async def procedural_rollback(self, entry_id, version, *, rolled_back_by=None):
        self.rolled_back.append((entry_id, version))
        return SimpleNamespace(id=entry_id, version=version)

    async def procedural_get(self, entry_id, *, vault_id=None):
        return SimpleNamespace(id=entry_id)

    async def procedural_list(self, *, status=None, scope=None, kind=None, vault_id=None, limit=50):
        return [
            SimpleNamespace(
                id=uuid4(),
                kind='procedure',
                scope='global',
                verb='deploy',
                context='nomad',
                title='Deploy to nomad',
                trigger='deploying a service',
                status='published',
                body='1. drain\n2. resubmit',
                success_count=3,
                failure_count=0,
                uses=3,
            )
        ]

    async def procedural_update(self, entry_id, payload):
        return SimpleNamespace(id=entry_id, version=2)


@pytest.mark.asyncio
async def test_controller_search_unwraps_entries() -> None:
    ctrl = ProceduralCurationController(_FakeClient())
    entries = await ctrl.search('deploy')
    assert len(entries) == 1
    assert entries[0].title == 'Deploy to nomad'


@pytest.mark.asyncio
async def test_controller_search_empty_query_short_circuits() -> None:
    client = _FakeClient()
    ctrl = ProceduralCurationController(client)
    assert await ctrl.search('   ') == []
    assert client.searched == []  # no round-trip


@pytest.mark.asyncio
async def test_controller_pin_validates_context_before_call() -> None:
    client = _FakeClient()
    ctrl = ProceduralCurationController(client)
    with pytest.raises(ValueError):
        await ctrl.pin(uuid4(), 'user')
    assert client.pins == []  # rejected before the network call


@pytest.mark.asyncio
async def test_controller_pin_appends() -> None:
    client = _FakeClient()
    ctrl = ProceduralCurationController(client)
    eid = uuid4()
    await ctrl.pin(eid, 'global')
    assert client.pins == [(eid, 'global', None)]


@pytest.mark.asyncio
async def test_controller_context_state_counts_pins() -> None:
    ctrl = ProceduralCurationController(_FakeClient())
    state = await ctrl.context_state('global')
    assert state.pin_count == 2
    assert state.capacity_label == f'2/{PIN_CAP_PER_CONTEXT}'


@pytest.mark.asyncio
async def test_controller_rollback_threads_actor() -> None:
    client = _FakeClient()
    ctrl = ProceduralCurationController(client)
    eid = uuid4()
    await ctrl.rollback(eid, 1)
    assert client.rolled_back == [(eid, 1)]


# ---------------------------------------------------------------------------
# App construction + bindings (no run)
# ---------------------------------------------------------------------------


def test_app_constructs_and_registers_bindings() -> None:
    from memex_cli.procedural_tui.app import ProceduralCurationApp

    app = ProceduralCurationApp(
        ProceduralCurationController(_FakeClient()),
        project_id='memex',
        app_identity='claude-code',
    )
    keys = {b.key for b in app.BINDINGS}
    # The browse-cockpit verbs are all bound (filter / cycle-context / pin /
    # versions / edit / quit). Rollback ('r') lives on the version sub-screen.
    for k in ('slash', 'c', 'p', 'v', 'e', 'q'):
        assert k in keys, f'binding {k!r} missing'
