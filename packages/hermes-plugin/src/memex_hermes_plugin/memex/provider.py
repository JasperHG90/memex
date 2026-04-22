"""``MemexMemoryProvider`` — Hermes Agent memory provider backed by Memex.

Lifecycle: Hermes calls ``initialize`` once at session start → ``get_tool_schemas``
at prompt assembly → ``system_prompt_block`` (which blocks for the briefing) →
``queue_prefetch``/``prefetch`` each turn → ``handle_tool_call`` when the model
uses a Memex tool → ``on_session_end``/``on_pre_compress``/``on_memory_write``
hooks as they fire → ``shutdown`` at exit.

The plugin talks to a running Memex server over HTTP via ``RemoteMemexAPI``.
All async calls are marshalled onto the shared event loop in ``async_bridge``.
"""

from __future__ import annotations

import atexit
import base64
import logging
import os
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]

from .async_bridge import run_sync, shutdown_loop
from .briefing import BriefingCache, format_briefing_block
from .config import HermesMemexConfig, load_config, save_config
from .prefetch import PrefetchCache
from .project import derive_project_id, resolve_vault
from .session import make_session_note_key
from .templates import HERMES_SESSION_TEMPLATE
from .tools import ALL_SCHEMAS, dispatch

logger = logging.getLogger(__name__)


def _resolve_hermes_home(kwargs: dict[str, Any]) -> Path:
    raw = kwargs.get('hermes_home') or os.environ.get('HERMES_HOME')
    if raw:
        return Path(raw).expanduser()
    return Path.home() / '.hermes'


class MemexMemoryProvider(MemoryProvider):
    """Memex-backed memory provider."""

    def __init__(self) -> None:
        self._config: HermesMemexConfig | None = None
        self._hermes_home: Path | None = None
        self._client: httpx.AsyncClient | None = None
        self._api: Any | None = None
        self._vault_name: str | None = None
        self._vault_id: UUID | None = None
        self._project_id: str = ''
        self._session_note_key: str = ''
        self._session_id: str = ''
        self._agent_identity: str = ''
        self._user_id: str | None = None
        self._platform: str = ''
        self._briefing = BriefingCache()
        self._prefetch = PrefetchCache()
        self._turn_buffer: list[dict[str, str]] = []
        self._turn_count = 0
        self._shutdown_registered = False
        self._state_lock = threading.Lock()
        self._atexit_lock = threading.Lock()

    # -- Identity ------------------------------------------------------------

    @property
    def name(self) -> str:  # type: ignore[override]
        return 'memex'

    # -- Availability --------------------------------------------------------

    def is_available(self) -> bool:
        """True when we have enough config to talk to a Memex server.

        Checks env vars first, then the plugin config file. No network calls.
        """
        if os.environ.get('MEMEX_SERVER_URL'):
            return True
        try:
            hermes_home = Path(os.environ.get('HERMES_HOME') or str(Path.home() / '.hermes'))
            cfg_path = hermes_home / 'memex' / 'config.json'
            if cfg_path.exists():
                return True
            # Fall back to Memex's own config — if it has a server_url the
            # user already runs Memex locally and we can use it.
            from memex_common.config import MemexConfig

            mc = MemexConfig()
            return bool(mc.server_url)
        except Exception:
            return False

    # -- Config schema for ``hermes memory setup`` --------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                'key': 'server_url',
                'description': 'Memex server URL',
                'default': 'http://127.0.0.1:8000',
            },
            {
                'key': 'api_key',
                'description': 'Memex API key (optional; only for secured deployments)',
                'secret': True,
                'env_var': 'MEMEX_API_KEY',
            },
            {
                'key': 'vault_id',
                'description': 'Fallback vault name when no per-project binding is set',
            },
            {
                'key': 'memory_mode',
                'description': 'hybrid / context / tools',
                'default': 'hybrid',
                'choices': ['hybrid', 'context', 'tools'],
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:  # type: ignore[override]
        save_config(values, Path(hermes_home).expanduser())

    # -- Initialization ------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:  # type: ignore[override]
        self._session_id = session_id
        self._hermes_home = _resolve_hermes_home(kwargs)
        self._agent_identity = kwargs.get('agent_identity') or ''
        self._user_id = kwargs.get('user_id')
        self._platform = kwargs.get('platform') or ''

        self._config = load_config(self._hermes_home)
        self._session_note_key = make_session_note_key()
        self._project_id = derive_project_id()

        headers: dict[str, str] = {}
        if self._config.api_key:
            headers['X-API-Key'] = self._config.api_key

        base_url = f'{self._config.server_url.rstrip("/")}/api/v1/'
        self._client = httpx.AsyncClient(base_url=base_url, timeout=240.0, headers=headers)

        from memex_common.client import RemoteMemexAPI

        self._api = RemoteMemexAPI(self._client)

        try:
            self._vault_name = resolve_vault(
                self._api,
                project_id=self._project_id,
                agent_identity=self._agent_identity or None,
                user_id=self._user_id,
                config_vault=self._config.vault_id,
            )
        except Exception as e:
            logger.debug('Vault resolution failed: %s', e)
            self._vault_name = self._config.vault_id

        if self._vault_name:
            self._vault_id = self._resolve_or_create_vault_id(self._vault_name)

        if self._vault_id is not None and self._config.memory_mode != 'tools':
            self._briefing.start_fetch(
                self._api,
                vault_id=self._vault_id,
                budget=self._config.briefing_budget,
                project_id=self._project_id,
            )

        with self._atexit_lock:
            if not self._shutdown_registered:
                atexit.register(self._atexit_shutdown)
                self._shutdown_registered = True

        logger.debug(
            'Memex provider initialized: session=%s vault=%s project=%s',
            session_id,
            self._vault_name,
            self._project_id,
        )

    def _resolve_or_create_vault_id(self, name: str) -> UUID | None:
        """Resolve ``name`` to a UUID; optionally create if missing."""
        assert self._api is not None
        try:
            return run_sync(self._api.resolve_vault_identifier(name), timeout=5.0)
        except Exception as e:
            logger.debug('Vault %s does not exist: %s', name, e)

        if self._config is None or not self._config.create_vaults_on_init:
            return None

        from memex_common.schemas import CreateVaultRequest

        try:
            vault = run_sync(
                self._api.create_vault(CreateVaultRequest(name=name)),
                timeout=10.0,
            )
            return UUID(str(vault.id))
        except Exception as e:
            logger.warning('Failed to auto-create vault %s: %s', name, e)
            return None

    # -- System prompt block ------------------------------------------------

    def system_prompt_block(self) -> str:  # type: ignore[override]
        if self._config is None or self._config.memory_mode == 'tools':
            return ''
        briefing = self._briefing.get(timeout=5.0)
        return format_briefing_block(
            briefing,
            vault_id=self._vault_name,
            project_id=self._project_id,
            session_note_key=self._session_note_key,
            kv_instructions_if_no_vault=self._vault_name is None,
        )

    # -- Tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:  # type: ignore[override]
        if self._config is None or self._config.memory_mode == 'context':
            return []
        return list(ALL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:  # type: ignore[override]
        if self._api is None or self._config is None:
            from tools.registry import tool_error  # type: ignore[import-not-found]

            return tool_error('Memex provider is not initialized.')
        return dispatch(
            tool_name,
            args,
            api=self._api,
            config=self._config,
            vault_id=self._vault_id,
        )

    # -- Prefetch ------------------------------------------------------------

    def queue_prefetch(self, query: str, *, session_id: str = '') -> None:  # type: ignore[override]
        if self._config is None or self._config.memory_mode == 'tools':
            return
        if self._api is None or self._vault_id is None:
            return
        self._prefetch.queue(
            query,
            api=self._api,
            config=self._config,
            vault_id=self._vault_id,
        )

    def prefetch(self, query: str, *, session_id: str = '') -> str:  # type: ignore[override]
        if self._config is None or self._config.memory_mode == 'tools':
            return ''
        return self._prefetch.consume(timeout=3.0)

    # -- Turn / session hooks ------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:  # type: ignore[override]
        self._turn_count = turn_number
        if (
            self._config is not None
            and self._config.briefing_refresh_cadence > 0
            and turn_number > 0
            and turn_number % self._config.briefing_refresh_cadence == 0
            and self._vault_id is not None
            and self._api is not None
        ):
            self._briefing.reset()
            self._briefing.start_fetch(
                self._api,
                vault_id=self._vault_id,
                budget=self._config.briefing_budget,
                project_id=self._project_id,
            )

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = '') -> None:  # type: ignore[override]
        """Buffer the turn; we ingest the full transcript in ``on_session_end``."""
        with self._state_lock:
            self._turn_buffer.append({'user': user_content, 'assistant': assistant_content})

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:  # type: ignore[override]
        if self._api is None or self._config is None:
            return
        transcript = _format_transcript(messages or self._turn_buffer)
        if not transcript.strip():
            return
        self._ingest_session_note(transcript, title=self._format_session_title())

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:  # type: ignore[override]
        """Append soon-to-be-compressed messages to the session note so nothing is lost.

        Returns a short summary that Hermes includes in the compression prompt.
        """
        if self._api is None or self._config is None or not messages:
            return ''
        chunk = _format_transcript(messages)
        if chunk.strip():
            title = f'{self._format_session_title()} (pre-compress fragment)'
            self._ingest_session_note(chunk, title=title)
        return (
            f'Memex captured {len(messages)} pre-compression messages into '
            f'session note `{self._session_note_key}`.'
        )

    def on_memory_write(self, action: str, target: str, content: str) -> None:  # type: ignore[override]
        """Mirror built-in MEMORY.md/USER.md writes to the Memex KV store.

        Keys are namespaced per Memex's ``VALID_NAMESPACES`` contract:
        ``app:hermes:<target>:<hash>``. The ``app:`` prefix is Memex's
        app-scoped namespace; ``hermes`` scopes within it; ``<target>`` is
        'memory' or 'user' (from Hermes' built-in memory); hash dedupes.
        """
        if self._api is None or action == 'remove' or not content:
            return
        import hashlib

        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]
        key = f'app:hermes:{target}:{digest}'
        try:
            run_sync(self._api.kv_put(value=content, key=key), timeout=10.0)
        except Exception as e:
            logger.debug('KV mirror failed for %s: %s', key, e)

    # -- Shutdown ------------------------------------------------------------

    def shutdown(self) -> None:  # type: ignore[override]
        """Flush pending buffers and close the client."""
        if self._api is not None and self._turn_buffer:
            transcript = _format_transcript(self._turn_buffer)
            if transcript.strip():
                try:
                    self._ingest_session_note(transcript, title=self._format_session_title())
                except Exception as e:
                    logger.debug('Shutdown ingest failed: %s', e)
        client = self._client
        self._client = None
        self._api = None
        if client is not None:
            try:
                run_sync(client.aclose(), timeout=5.0)
            except Exception:
                pass
        shutdown_loop(thread_join_timeout=5.0)

    def _atexit_shutdown(self) -> None:
        """atexit callback — best-effort cleanup if ``shutdown`` was skipped."""
        try:
            self.shutdown()
        except Exception:
            pass

    # -- Helpers -------------------------------------------------------------

    def _format_session_title(self) -> str:
        """Render the session-note title from the configured template.

        Substitutes ``{agent_identity}``, ``{platform}``, ``{date}``,
        ``{session_id}``, ``{session_id_short}``. Missing/empty fields
        render as ``'agent'`` / ``'?'`` so the title stays readable.
        Falls back to a hardcoded default if the template references an
        unsupported key.
        """
        from datetime import datetime, timezone

        if self._config is None:
            return 'Hermes session'
        template = self._config.retain.session_title_template

        session_short = (self._session_id or '')[:8] or '?'
        substitutions = {
            'agent_identity': self._agent_identity or 'agent',
            'platform': self._platform or 'unknown',
            'date': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
            'session_id': self._session_id or '?',
            'session_id_short': session_short,
        }
        try:
            return template.format(**substitutions)
        except (KeyError, IndexError, ValueError) as e:
            logger.warning(
                'Session title template %r failed to render: %s — falling back to default.',
                template,
                e,
            )
            return f'Hermes session — {substitutions["date"]}'

    def _ingest_session_note(self, content: str, *, title: str) -> None:
        assert self._api is not None and self._config is not None

        from memex_common.schemas import NoteCreateDTO

        dto = NoteCreateDTO(
            name=title,
            description=f'Hermes session transcript ({self._session_id})',
            content=base64.b64encode(content.encode('utf-8')),
            note_key=self._session_note_key,
            vault_id=str(self._vault_id) if self._vault_id else None,
            tags=['hermes', self._agent_identity] if self._agent_identity else ['hermes'],
            author='hermes',
            template=self._config.retain.session_template or HERMES_SESSION_TEMPLATE,
        )
        try:
            run_sync(self._api.ingest(dto, background=True), timeout=30.0)
            with self._state_lock:
                self._turn_buffer = []
        except Exception as e:
            logger.warning('Session note ingest failed: %s', e)


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    """Render a list of turn dicts as a flat markdown transcript.

    Accepts both ``{user, assistant}`` pairs (our sync_turn buffer format) and
    Hermes' own ``{role, content}`` message objects.
    """
    lines: list[str] = []
    for m in messages:
        if 'user' in m or 'assistant' in m:
            if m.get('user'):
                lines.append(f'**User:** {m["user"]}')
            if m.get('assistant'):
                lines.append(f'**Assistant:** {m["assistant"]}')
        else:
            role = str(m.get('role', 'user')).strip() or 'user'
            content = m.get('content', '')
            if isinstance(content, list):
                content = '\n'.join(
                    c.get('text', '') if isinstance(c, dict) else str(c) for c in content
                )
            if content:
                lines.append(f'**{role.capitalize()}:** {content}')
        lines.append('')
    return '\n'.join(lines).strip()


__all__ = ['MemexMemoryProvider']
