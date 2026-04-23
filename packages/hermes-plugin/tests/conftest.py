"""Shared test fixtures and Hermes stub injection.

Hermes plugin modules import from ``agent.memory_provider`` and ``tools.registry``.
Neither is pip-installable — they live inside the hermes-agent repo. We inject
the vendored stubs in ``tests/_stubs/`` into ``sys.modules`` before any plugin
code is imported so tests run without a Hermes checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

# Make the vendored stubs importable.
_STUBS_DIR = Path(__file__).parent / '_stubs'
if str(_STUBS_DIR) not in sys.path:
    sys.path.insert(0, str(_STUBS_DIR))


def _install_hermes_stubs() -> None:
    """Install ``agent.memory_provider`` and ``tools.registry`` into ``sys.modules``."""
    import importlib

    if 'agent.memory_provider' not in sys.modules:
        agent_mod = sys.modules.get('agent') or ModuleType('agent')
        mp_mod = importlib.import_module('memory_provider')
        agent_mod.memory_provider = mp_mod  # type: ignore[attr-defined]
        sys.modules['agent'] = agent_mod
        sys.modules['agent.memory_provider'] = mp_mod

    if 'tools.registry' not in sys.modules:
        tools_mod = sys.modules.get('tools') or ModuleType('tools')
        reg_mod = importlib.import_module('tools_registry')
        tools_mod.registry = reg_mod  # type: ignore[attr-defined]
        sys.modules['tools'] = tools_mod
        sys.modules['tools.registry'] = reg_mod


_install_hermes_stubs()


import pytest  # noqa: E402

from memex_hermes_plugin.memex import async_bridge  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_async_bridge():
    """Ensure the shared event loop is torn down between tests."""
    yield
    try:
        async_bridge._reset_for_tests()
    except Exception:
        pass


# --- Shared fixtures for new-tool tests (pre-installed by Stream 1) ---
# Each stream fills in the fixtures relevant to its tools; fixtures that
# multiple streams use go here (not in stream-specific test sections).


@pytest.fixture
def _fake_vault_dto():
    """Stream 2+ supplies the body when it lands ``memex_list_vaults`` / ``memex_get_vault_summary``."""
    return None


@pytest.fixture
def _fake_find_note_result():
    """Stream 2 supplies the body when it lands ``memex_find_note``."""
    return None


@pytest.fixture
def _fake_note_dto():
    """Stream 2 supplies the body when it lands ``memex_read_note`` / ``memex_get_notes_metadata``."""
    return None


@pytest.fixture
def _fake_node_dto():
    """Stream 2 supplies the body when it lands ``memex_get_nodes``."""
    return None
