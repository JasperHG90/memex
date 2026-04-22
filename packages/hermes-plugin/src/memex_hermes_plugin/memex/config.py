"""Hermes-side config for the Memex memory provider.

Resolution order (highest precedence first):
1. Environment variables (``MEMEX_SERVER_URL``, ``MEMEX_API_KEY``, ``MEMEX_VAULT``,
   ``MEMEX_HERMES_MODE``)
2. ``$HERMES_HOME/memex/config.json``
3. Memex's own ``MemexConfig`` — reads ``~/.config/memex/config.yaml`` and any
   local ``.memex.yaml``. Frictionless for users who already run Memex locally.

Secrets (api_key) belong in ``$HERMES_HOME/.env`` and load via env vars.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MemoryMode = Literal['hybrid', 'context', 'tools']


# Retrieval strategies accepted by the Memex server.
# Keep in sync with ``memex_core.memory.retrieval.models.VALID_STRATEGIES``.
# ``test_default_strategies_match_server`` asserts parity.
VALID_STRATEGIES = frozenset({'semantic', 'keyword', 'graph', 'temporal', 'mental_model'})


class RecallConfig(BaseModel):
    facts_limit: int = 5
    notes_limit: int = 3
    strategies: list[str] = Field(
        default_factory=lambda: ['semantic', 'keyword', 'temporal', 'graph', 'mental_model']
    )
    token_budget: int = 2048
    include_stale: bool = False
    include_superseded: bool = False
    expand_query: bool = False

    @field_validator('strategies')
    @classmethod
    def _validate_strategies(cls, v: list[str]) -> list[str]:
        invalid = set(v) - VALID_STRATEGIES
        if invalid:
            raise ValueError(
                f'Invalid strategies: {sorted(invalid)}. Valid: {sorted(VALID_STRATEGIES)}'
            )
        return v


class RetainConfig(BaseModel):
    session_template: str = 'hermes-session'
    # Format string for the session-note title. Available substitutions:
    #   {agent_identity} {platform} {date} {session_id} {session_id_short}
    # The agent can also override the title mid-session by calling
    # ``memex_retain(name=..., note_key=<session_note_key>)`` directly.
    session_title_template: str = 'Hermes session [{agent_identity}@{platform}] — {date}'


class HermesMemexConfig(BaseModel):
    """Plugin configuration resolved from file + env + MemexConfig fallback."""

    server_url: str = 'http://127.0.0.1:8000'
    api_key: str | None = None
    vault_id: str | None = None
    memory_mode: MemoryMode = 'hybrid'
    create_vaults_on_init: bool = True
    briefing_budget: int = 2000
    briefing_refresh_cadence: int = 0
    recall: RecallConfig = Field(default_factory=RecallConfig)
    retain: RetainConfig = Field(default_factory=RetainConfig)

    @field_validator('server_url')
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip('/')

    @field_validator('briefing_budget')
    @classmethod
    def _validate_budget(cls, v: int) -> int:
        # Memex server currently accepts 1000 or 2000 only (see memex_cli/session.py).
        if v not in (1000, 2000):
            raise ValueError('briefing_budget must be 1000 or 2000')
        return v


def _config_path(hermes_home: Path) -> Path:
    return hermes_home / 'memex' / 'config.json'


def _load_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}


def _apply_env(data: dict[str, Any]) -> dict[str, Any]:
    """Layer env vars on top of file data."""
    if 'MEMEX_SERVER_URL' in os.environ:
        data['server_url'] = os.environ['MEMEX_SERVER_URL']
    if 'MEMEX_API_KEY' in os.environ:
        data['api_key'] = os.environ['MEMEX_API_KEY']
    if 'MEMEX_VAULT' in os.environ:
        data['vault_id'] = os.environ['MEMEX_VAULT']
    if 'MEMEX_HERMES_MODE' in os.environ:
        data['memory_mode'] = os.environ['MEMEX_HERMES_MODE']
    return data


def _apply_memex_fallback(data: dict[str, Any]) -> dict[str, Any]:
    """If server_url or api_key are missing, try MemexConfig.

    This makes users who already run Memex locally frictionless: no
    Hermes-side config file needed.
    """
    need_server = 'server_url' not in data
    need_api_key = 'api_key' not in data
    need_vault = 'vault_id' not in data
    if not (need_server or need_api_key or need_vault):
        return data
    try:
        from memex_common.config import MemexConfig

        mc = MemexConfig()
        if need_server and mc.server_url:
            data['server_url'] = mc.server_url
        if need_api_key and mc.api_key is not None:
            data['api_key'] = mc.api_key.get_secret_value()
        if need_vault and mc.vault.active:
            data['vault_id'] = mc.vault.active
    except Exception:
        # MemexConfig can fail on malformed local config — ignore, use defaults.
        pass
    return data


def load_config(hermes_home: Path) -> HermesMemexConfig:
    """Resolve the plugin's config from file, env, and MemexConfig fallback."""
    path = _config_path(hermes_home)
    data = _load_file(path)
    data = _apply_env(data)
    data = _apply_memex_fallback(data)
    return HermesMemexConfig.model_validate(data)


def save_config(
    values: dict[str, Any],
    hermes_home: Path,
) -> Path:
    """Merge ``values`` into ``$HERMES_HOME/memex/config.json``.

    Only non-secret keys should arrive here — secrets go to ``.env`` via
    Hermes' setup flow. Returns the path written.
    """
    path = _config_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_file(path)
    existing.update({k: v for k, v in values.items() if v is not None})
    path.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
    return path


__all__ = [
    'HermesMemexConfig',
    'MemoryMode',
    'RecallConfig',
    'RetainConfig',
    'load_config',
    'save_config',
]
