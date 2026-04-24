"""Live end-to-end: real Hermes loader × real Memex FastAPI app × real Postgres.

The Memex server runs under uvicorn in a background thread, backed by a
testcontainers Postgres. The plugin talks over real HTTP and goes through
Hermes' own ``plugins.memory`` discovery + registration machinery.

No API mocks. Only LLM-bound behaviour (DSPy extraction, survey decomposition,
reranker) is avoided; the rest of the round-trip is real.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

pytestmark = pytest.mark.hermes_integration


def test_discovery_lists_memex(installed_plugin: Path):
    from plugins.memory import discover_memory_providers  # type: ignore[import-not-found]

    providers = {name: (desc, available) for name, desc, available in discover_memory_providers()}
    assert 'memex' in providers
    assert providers['memex'][1] is True  # available
    assert 'Memex' in providers['memex'][0]


def test_loaded_provider_subclasses_memory_provider_abc(loaded_provider):
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]

    assert isinstance(loaded_provider, MemoryProvider)
    assert loaded_provider.name == 'memex'


def test_initialize_resolves_vault_and_fetches_real_briefing(
    initialized_provider, live_vault: UUID
):
    assert initialized_provider._vault_id == live_vault
    assert initialized_provider._session_note_key.startswith('hermes:session:')
    block = initialized_provider.system_prompt_block()
    # The real briefing endpoint returns markdown with vault listings.
    assert '## Memex Memory' in block
    assert initialized_provider._session_note_key in block


def test_get_tool_schemas_exposes_7_tools(initialized_provider):
    names = {s['name'] for s in initialized_provider.get_tool_schemas()}
    assert names == {
        'memex_recall',
        'memex_retrieve_notes',
        'memex_survey',
        'memex_retain',
        'memex_list_entities',
        'memex_get_entity_mentions',
        'memex_get_entity_cooccurrences',
    }


def test_get_tool_schemas_at_registration_time(loaded_provider):
    """Regression for v0.1.13 production bug.

    Hermes calls ``get_tool_schemas()`` at provider *registration* time
    (before ``initialize()`` runs) to build its internal
    ``_tool_to_provider`` dispatch map. v0.1.13 returned ``[]`` there
    because ``self._config`` was None — Hermes then registered 0 memex
    tools and every model call routed to "Unknown tool".

    This test exercises exactly that code path: the plugin is loaded via
    Hermes' real loader, and we call ``get_tool_schemas()`` on it WITHOUT
    calling ``initialize()`` first.
    """
    # ``loaded_provider`` fixture returns the provider fresh from
    # ``plugins.memory.load_memory_provider('memex')`` — no ``initialize()``
    # has been called on it yet.
    assert loaded_provider._config is None, (
        'fixture contract: provider should be pre-init for this test'
    )
    schemas = loaded_provider.get_tool_schemas()
    names = {s['name'] for s in schemas}
    assert names == {
        'memex_recall',
        'memex_retrieve_notes',
        'memex_survey',
        'memex_retain',
        'memex_list_entities',
        'memex_get_entity_mentions',
        'memex_get_entity_cooccurrences',
    }, f'Pre-init schemas must cover the full tool set — got {names!r}'


@pytest.mark.asyncio
async def test_retain_roundtrip_via_real_server(initialized_provider, live_api, live_vault: UUID):
    """memex_retain → note lands in Postgres; verified by listing server-side."""
    marker = f'integration-marker-{UUID(int=0).hex}'
    result = initialized_provider.handle_tool_call(
        'memex_retain',
        {
            'name': 'integration-note',
            'description': 'integration test capture',
            'content': f'The marker is {marker}. The answer is 42.',
            'tags': ['integration'],
        },
    )
    data = json.loads(result)
    assert 'error' not in data, f'retain failed: {data}'

    # Wait a bit for the background batch job to persist the note.
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0.5)
        response = await live_api.client.get(
            'notes', params={'vault_id': str(live_vault), 'limit': 10}
        )
        if response.status_code == 200:
            body = response.text
            if 'integration-note' in body:
                return
    pytest.fail(
        f'retained note did not appear in /notes listing; last status={response.status_code}'
    )


def test_on_session_end_persists_transcript(initialized_provider, loaded_provider):
    initialized_provider.sync_turn('hello', 'hi there')
    initialized_provider.sync_turn('what did I say?', 'you said hello')
    initialized_provider.on_session_end([])
    # Transcript was buffered, then cleared after ingest.
    assert initialized_provider._turn_buffer == []


def test_on_memory_write_kv_roundtrip(initialized_provider, memex_server_url: str):
    """KV mirror must satisfy Memex's VALID_NAMESPACES contract end-to-end.

    Uses sync ``httpx.Client`` against the uvicorn-in-thread server so we stay
    off pytest-asyncio's loop (the plugin already spins up its own loop).
    """
    import hashlib

    import httpx

    from memex_core.services.kv import VALID_NAMESPACES

    initialized_provider.on_memory_write('add', 'user', 'Prefers Rust for systems code')

    digest = hashlib.sha256(b'Prefers Rust for systems code').hexdigest()[:12]
    expected_key = f'app:hermes:user:{digest}'
    with httpx.Client(base_url=f'{memex_server_url}/api/v1/', timeout=10.0) as client:
        response = client.get('kv/get', params={'key': expected_key})
    assert response.status_code == 200, f'KV lookup failed: {response.status_code} {response.text}'
    payload = response.json()
    assert payload['key'] == expected_key
    prefix = payload['key'].split(':', 1)[0]
    assert prefix in VALID_NAMESPACES
    assert payload['value'] == 'Prefers Rust for systems code'


def test_list_entities_on_empty_graph_returns_empty(initialized_provider):
    """No notes → no entities — should not error."""
    result = initialized_provider.handle_tool_call(
        'memex_list_entities', {'query': 'nonexistent-entity-xyz'}
    )
    data = json.loads(result)
    assert 'error' not in data
    assert data['results'] == []


def test_recall_with_no_matches_returns_empty(initialized_provider):
    result = initialized_provider.handle_tool_call(
        'memex_recall', {'query': 'zyxwvutsrq-no-match-hermes-integration'}
    )
    data = json.loads(result)
    assert 'error' not in data
    assert data['count'] == 0


def test_shutdown_is_idempotent(loaded_provider, hermes_home: Path):
    loaded_provider.initialize('shutdown-test', hermes_home=str(hermes_home), platform='cli')
    loaded_provider.shutdown()
    loaded_provider.shutdown()  # must not raise


def test_memory_mode_tools_hides_briefing_and_prefetch(
    installed_plugin,  # noqa: ARG001
    server_url_env: str,  # noqa: ARG001 — MEMEX_SERVER_URL set
    live_vault: UUID,  # noqa: ARG001 — vault exists
    vault_name: str,
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv('MEMEX_HERMES_MODE', 'tools')
    monkeypatch.setenv('MEMEX_VAULT', vault_name)
    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]

    provider = load_memory_provider('memex')
    try:
        provider.initialize('mode-tools', hermes_home=str(hermes_home), platform='cli')
        assert provider.system_prompt_block() == ''
        assert len(provider.get_tool_schemas()) == 7
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# v0.1.13: KV-driven vault binding + configurable session title
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_binding_resolves_vault_via_app_hermes_namespace(
    installed_plugin,  # noqa: ARG001
    server_url_env: str,  # noqa: ARG001 — MEMEX_SERVER_URL set
    live_api,
    live_vault: UUID,
    vault_name: str,
    hermes_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A KV entry under app:hermes:project:<id>:vault should win over MEMEX_VAULT.

    We point CWD at a non-git tmp dir so ``derive_project_id`` produces a
    deterministic, predictable id we can pre-bind via KV.
    """
    from memex_hermes_plugin.memex.cache import clear_vault_cache

    clear_vault_cache()

    # Force CWD to a non-git tmp dir so derive_project_id falls back to the
    # $HOME-relative or absolute path. We use HOME=tmp_path/home and
    # CWD=tmp_path/home/proj so the derived id is the predictable string 'proj'.
    home_root = tmp_path / 'home'
    proj_dir = home_root / 'proj'
    proj_dir.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home_root))
    monkeypatch.chdir(proj_dir)
    project_id = 'proj'

    # Bind that project_id to the live test vault via KV.
    kv_key = f'app:hermes:project:{project_id}:vault'
    await live_api.kv_put(value=vault_name, key=kv_key)

    # Sanity: the KV entry persists.
    entry = await live_api.kv_get(kv_key)
    assert entry is not None and entry.value == vault_name

    # Set MEMEX_VAULT to a different value so we know KV wins, not the env fallback.
    monkeypatch.setenv('MEMEX_VAULT', 'nonexistent-fallback-vault')

    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]

    provider = load_memory_provider('memex')
    try:
        provider.initialize(
            'kv-binding-session',
            hermes_home=str(hermes_home),
            platform='cli',
            agent_identity='integration',
            user_id='tester',
        )
        # CWD-derived project_id should be 'proj'.
        assert provider._project_id == project_id, (
            f'expected derived project_id={project_id!r}, got {provider._project_id!r}'
        )
        # KV binding takes precedence over MEMEX_VAULT.
        assert provider._vault_name == vault_name, (
            f'expected vault {vault_name!r} from KV binding, got {provider._vault_name!r}'
        )
        assert provider._vault_id == live_vault
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_no_synthetic_vault_lookups_against_server(
    installed_plugin,  # noqa: ARG001
    server_url_env: str,  # noqa: ARG001 — MEMEX_SERVER_URL set
    live_api,  # noqa: ARG001
    live_vault: UUID,  # noqa: ARG001 — vault exists
    vault_name: str,
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression: v0.1.12 hit the server with synthetic vault-name lookups
    like ``hermes:user:<id>``. The new design only does ``kv_get``s.

    We assert that no vault named ``hermes:user:tester`` or
    ``hermes:agent:integration`` was created (since the plugin should never
    have asked for them in the first place).
    """
    from memex_hermes_plugin.memex.cache import clear_vault_cache

    clear_vault_cache()

    monkeypatch.setenv('MEMEX_VAULT', vault_name)

    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]

    provider = load_memory_provider('memex')
    try:
        provider.initialize(
            'no-synthetic-session',
            hermes_home=str(hermes_home),
            platform='cli',
            agent_identity='integration',
            user_id='tester',
        )
    finally:
        provider.shutdown()

    # The synthetic vault names (the v0.1.12 anti-pattern) must not exist.
    vaults = await live_api.list_vaults()
    names = {v.name for v in vaults}
    assert 'hermes:user:tester' not in names
    assert 'hermes:agent:integration' not in names


@pytest.mark.asyncio
async def test_vault_cache_avoids_repeat_kv_calls(
    installed_plugin,  # noqa: ARG001
    server_url_env: str,  # noqa: ARG001 — MEMEX_SERVER_URL set
    live_api,
    live_vault: UUID,  # noqa: ARG001
    vault_name: str,
    hermes_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two sessions in a row should only hit Memex KV once (cached).

    The cache singleton lives in the plugin's loaded module namespace
    (``_hermes_user_memory.memex.cache``), which is a different module
    object from the test-side ``memex_hermes_plugin.memex.cache``. We
    inspect the loaded-side singleton directly via ``sys.modules``.
    """
    import sys

    # Same trick as test_kv_binding_resolves: deterministic project_id via CWD.
    home_root = tmp_path / 'home'
    proj_dir = home_root / 'cacheproj'
    proj_dir.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home_root))
    monkeypatch.chdir(proj_dir)
    project_id = 'cacheproj'

    kv_key = f'app:hermes:project:{project_id}:vault'
    await live_api.kv_put(value=vault_name, key=kv_key)

    monkeypatch.setenv('MEMEX_VAULT', vault_name)

    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]

    # Load once so the loader-side cache module is in sys.modules.
    provider1 = load_memory_provider('memex')
    loaded_cache_mod = sys.modules['_hermes_user_memory.memex.cache']
    # Reset any leftover state from prior tests on the loader-side singleton.
    loaded_cache_mod.configure_vault_cache(300.0)
    cache = loaded_cache_mod.vault_cache()
    assert len(cache) == 0, 'cache should be empty before any plugin init'

    try:
        provider1.initialize(
            's1',
            hermes_home=str(hermes_home),
            platform='cli',
            agent_identity='integration',
            user_id='tester',
        )
    finally:
        provider1.shutdown()

    cache_size_after_first = len(cache)
    assert cache_size_after_first >= 1, 'first init should populate the cache'

    provider2 = load_memory_provider('memex')
    try:
        provider2.initialize(
            's2',
            hermes_home=str(hermes_home),
            platform='cli',
            agent_identity='integration',
            user_id='tester',
        )
    finally:
        provider2.shutdown()

    # Cache size shouldn't grow if the second resolve hit the cache.
    assert len(cache) == cache_size_after_first


def test_session_title_uses_template_against_real_server(
    installed_plugin,  # noqa: ARG001
    memex_server_url: str,
    server_url_env: str,  # noqa: ARG001 — MEMEX_SERVER_URL set
    live_vault: UUID,
    vault_name: str,
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Session note title is no longer hardcoded — it follows the template.

    Uses sync ``httpx.Client`` for verification (the plugin already runs on
    its own asyncio loop; pytest-asyncio + plugin loop = bound-to-different-
    loop errors).
    """
    import json
    import sys
    import time

    import httpx

    cfg_dir = hermes_home / 'memex'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'memex' / 'config.json' if False else cfg_dir / 'config.json').write_text(
        json.dumps(
            {
                'server_url': memex_server_url,
                'vault_id': vault_name,
                'retain': {
                    'session_title_template': ('IntegrationTitle [{agent_identity}@{platform}]'),
                },
            }
        )
    )
    monkeypatch.setenv('MEMEX_VAULT', vault_name)

    # Reset the loaded-side cache so resolve_vault hits Memex fresh.
    if '_hermes_user_memory.memex.cache' in sys.modules:
        sys.modules['_hermes_user_memory.memex.cache'].clear_vault_cache()

    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]

    provider = load_memory_provider('memex')
    try:
        provider.initialize(
            'title-test',
            hermes_home=str(hermes_home),
            platform='cli',
            agent_identity='coder',
            user_id='tester',
        )
        provider.sync_turn('hi', 'hello')
        provider.on_session_end([])
    finally:
        provider.shutdown()

    # Poll the server (sync httpx) for the new note to appear with our title.
    deadline = time.monotonic() + 30.0
    found_titles: list[str] = []
    with httpx.Client(base_url=f'{memex_server_url}/api/v1/', timeout=10.0) as client:
        while time.monotonic() < deadline:
            response = client.post(
                'notes/search',
                json={
                    'query': 'IntegrationTitle',
                    'limit': 10,
                    'vault_ids': [str(live_vault)],
                },
            )
            if response.status_code == 200:
                # NDJSON stream — one note per line.
                titles = []
                for line in response.text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    metadata = obj.get('metadata') or {}
                    title = metadata.get('name') or metadata.get('title')
                    if title:
                        titles.append(title)
                if any('IntegrationTitle' in t for t in titles):
                    found_titles = titles
                    break
            time.sleep(0.5)

    assert any('IntegrationTitle [coder@cli]' in t for t in found_titles), (
        f'Expected templated title in {found_titles!r}'
    )


# ---------------------------------------------------------------------------
# Live LLM check: does Claude actually follow the retain content contract?
# ---------------------------------------------------------------------------


@pytest.mark.llm
def test_retain_content_is_structured_markdown():
    """Given our `RETAIN_SCHEMA` as a tool, Claude must emit markdown with a
    single H1 title and `##` section headings — never the `**Label:**` inline
    prose we saw hermes default to before the contract was added.

    We route through Hermes' own ``build_anthropic_client`` so the test fails
    if Hermes' client construction stops working. We don't run the full
    ``AIAgent`` loop — it would pull in credentials, context engine, memory
    provider, and 12k lines of orchestration for one tool-call check.
    """
    import os
    import sys

    if not os.environ.get('ANTHROPIC_API_KEY'):
        pytest.skip('live LLM turn requires ANTHROPIC_API_KEY')

    # The unit-test conftest injects a bare-``ModuleType`` stub for ``agent`` so
    # unit tests can import the plugin without hermes-agent installed. That
    # stub shadows the real package and breaks ``agent.anthropic_adapter``.
    # Pop the stubs so the real import resolves.
    for _name in list(sys.modules):
        if (
            _name == 'agent'
            or _name.startswith('agent.')
            or _name == 'tools'
            or _name.startswith('tools.')
        ):
            sys.modules.pop(_name, None)

    from agent.anthropic_adapter import (  # type: ignore[import-not-found]
        build_anthropic_client,
    )

    from memex_hermes_plugin.memex.tools import RETAIN_SCHEMA

    tool = {
        'name': RETAIN_SCHEMA['name'],
        'description': RETAIN_SCHEMA['description'],
        'input_schema': RETAIN_SCHEMA['parameters'],
    }

    client = build_anthropic_client(api_key=os.environ['ANTHROPIC_API_KEY'])
    resp = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=1024,
        temperature=0,
        tool_choice={'type': 'tool', 'name': 'memex_retain'},
        tools=[tool],
        messages=[
            {
                'role': 'user',
                'content': (
                    "Today at 00:00 UTC our 'Daily reflect' cron ran, but the "
                    'assistant could not retrieve its conclusions later. The '
                    'reflection output was buried inside a raw Hermes session '
                    'log instead of being indexed as a standalone summary, so '
                    'semantic search for "conclusions" missed it. The assistant '
                    'fell back to a generalized summary of recent activity. '
                    'Capture this as a structured post-mortem via memex_retain '
                    'with name "Retrieval Failure: Daily Reflection Conclusions", '
                    'a one-sentence description, and content covering date, '
                    'symptom, root cause, outcome, and lesson.'
                ),
            }
        ],
    )

    tool_uses = [b for b in resp.content if getattr(b, 'type', None) == 'tool_use']
    assert tool_uses, f'model did not call any tool: {resp.content!r}'
    retain_call = next((b for b in tool_uses if b.name == 'memex_retain'), None)
    assert retain_call is not None, f'model called a different tool: {tool_uses!r}'

    body = retain_call.input.get('content', '')
    assert body, 'memex_retain called with empty content'

    # Structural assertions — brittle-by-design; this IS the contract.
    assert body.lstrip().startswith('# '), (
        f'body must open with a single `# Title`. Got: {body[:80]!r}'
    )
    assert '\n## ' in body, f'body must contain at least one `##` section heading. Got:\n{body}'

    # Anti-pattern: a line of the form `**Label:** value` at top level (not in
    # a bullet). Bullets like `- **sub-label**: detail` are allowed.
    import re

    inline_label = re.compile(r'^\*\*[A-Z][^*]{0,40}:\*\*\s+\S')
    offenders = [
        line
        for line in body.splitlines()
        if inline_label.match(line.lstrip()) and not line.lstrip().startswith(('-', '*'))
    ]
    assert not offenders, 'inline `**Label:** value` anti-pattern leaked into body:\n' + '\n'.join(
        offenders
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# (No module-level patches needed — tests force a deterministic project_id by
# setting HOME + chdir to a non-git tmp dir; ``derive_project_id`` then falls
# back to the $HOME-relative path.)
