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

import asyncio
import atexit
import base64
import concurrent.futures
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]
from memex_common.asset_cache import SessionAssetCache
from memex_common.note_utils import derive_note_uuid_from_key

from .async_bridge import run_sync
from .briefing import BriefingCache, format_briefing_block
from .config import HermesMemexConfig, RetainConfig, load_config, save_config
from .prefetch import PrefetchCache
from .project import derive_project_id, resolve_vault
from .session import make_session_note_key
from .templates import HERMES_SESSION_TEMPLATE
from .tools import ALL_SCHEMAS, TOOLS_MODE_SCHEMAS, dispatch
from .transcript import (
    _content_chars,
    format_transcript,
    passes_quality_gate,
    preprocess_turns,
)

logger = logging.getLogger(__name__)

# Hard cap on the in-memory retry queue: bounds memory if Memex is
# unreachable for an entire session.
_PENDING_MAX = 256

# Non-transient HTTP statuses. The append/create will never succeed if
# resent verbatim, so we drop the failing entry and continue draining the
# rest of the queue. 5xx, 408, 429, and network errors are transient.
#
# Asymmetry on 409:
# - 409 on CREATE is the server's overlap detector signalling an in-flight
#   ingest of the same note_key. The note row WILL materialise once that
#   job completes, so `_drain_pending` special-cases this branch and waits
#   on `_wait_for_note_row` instead of dropping.
# - 409 on APPEND has different semantics (note status / id conflict) and
#   is final — drop and let the next append carry on. Append idempotency
#   is keyed on `append_id`; re-POSTing the same body without a new id
#   would only re-trigger the same 409.
_NON_TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({400, 404, 409, 410, 422})

# Exceptions that must NEVER be silently swallowed by the drain loop:
# - KeyboardInterrupt / SystemExit — shell-level cooperative shutdown.
# - asyncio.CancelledError — direct asyncio cancellation (would surface
#   here only if code is refactored to await directly).
# - concurrent.futures.CancelledError — `run_sync` translates cancellation
#   of the bridged asyncio Task into this on the calling thread (via
#   `_chain_future` recognising the asyncio Task entered cancelled state).
#   Importantly: concurrent.futures.CancelledError inherits from Exception
#   (NOT BaseException), so a bare `except Exception` clause would absorb
#   it and turn cooperative cancellation into "warn + retry on next flush"
#   — exactly the swallow A.6 is meant to prevent.
_PROPAGATE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    SystemExit,
    asyncio.CancelledError,
    concurrent.futures.CancelledError,
)

# Default deadline for _wait_for_note_row — sized for the p99 of
# background ingest (LLM extraction) on resource-constrained hardware
# (e.g. Jetson Orin Nano). The previous 10s deadline expired before
# extraction completed on most session notes, which combined with the
# overlap-409 drop produced the orphaned-session-note storm documented
# in the 2026-05-29 tech report.
_WAIT_FOR_NOTE_ROW_DEFAULT_TIMEOUT = 120.0

# Exponential backoff schedule for _wait_for_note_row attempts (seconds
# between attempts). After the schedule is exhausted, attempts space at
# _WAIT_FOR_NOTE_ROW_BACKOFF_PLATEAU until the deadline. Keeps the early
# cycles snappy (fast happy-path return) while bounding poll volume for
# multi-minute extractions.
_WAIT_FOR_NOTE_ROW_BACKOFF: tuple[float, ...] = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0)
_WAIT_FOR_NOTE_ROW_BACKOFF_PLATEAU = 10.0


def _backoff_delay(attempt: int) -> float:
    """Return the delay (seconds) to sleep before the next poll attempt."""
    if attempt < len(_WAIT_FOR_NOTE_ROW_BACKOFF):
        return _WAIT_FOR_NOTE_ROW_BACKOFF[attempt]
    return _WAIT_FOR_NOTE_ROW_BACKOFF_PLATEAU


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
        self._asset_cache: SessionAssetCache | None = None
        self._turn_buffer: list[dict[str, str]] = []
        self._turn_count = 0
        # Watermark: turns at index < _flushed_index were already captured
        # into _pending. Avoids double-writes across flush boundaries.
        self._flushed_index = 0
        self._note_initialized = False
        # FIFO queue of pending writes (create or append). Each entry
        # snapshots the vault_id at enqueue time so a mid-session vault
        # rebind doesn't redirect in-flight chunks.
        self._pending: list[dict[str, Any]] = []
        self._shutdown_registered = False
        self._shutdown_started = False
        self._state_lock = threading.Lock()
        self._atexit_lock = threading.Lock()
        # Serializes _drain_pending so concurrent flush callers don't
        # race on the head item. _state_lock alone isn't enough: the
        # network call happens with _state_lock released.
        self._drain_lock = threading.Lock()

    @property
    def asset_cache(self) -> SessionAssetCache | None:
        return self._asset_cache

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
        assert self._asset_cache is None, 'initialize() called twice; prior asset cache leaked'
        self._asset_cache = SessionAssetCache()

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
                # Pin-chain consumer identity (V7 §19.8): the
                # per-agent app context is app:hermes:<agent_identity>
                # so a trader and a researcher get different pinned
                # procedure cards without new identity plumbing.
                app=(f'hermes:{self._agent_identity}' if self._agent_identity else 'hermes'),
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

        try:
            vault = run_sync(
                self._api.create_vault(name=name),
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
            lint_pending_count=self._fetch_lint_pending_count(),
            lint_pending_winner_proposals=self._fetch_pending_winner_proposals(),
        )

    def _fetch_lint_pending_count(self) -> int | None:
        """Fetch pending lint-finding count for the active vault.

        Best-effort: any failure returns None and the briefing renders
        without the maintenance section. Vault must be resolved.
        """
        if self._api is None or self._vault_id is None:
            return None
        try:
            payload = run_sync(
                self._api.lint_status(scope='vault', vault_id=str(self._vault_id)),
                timeout=2.0,
            )
            return int(payload.get('pending', 0))
        except Exception as e:
            logger.debug('lint_status fetch failed: %s', e)
            return None

    def _fetch_pending_winner_proposals(self) -> int | None:
        """Count pending winner-proposal findings for the briefing cue.

        Best-effort: any failure returns None and the briefing renders
        without the extra cue.
        """
        if self._api is None or self._vault_id is None:
            return None
        try:
            payload = run_sync(
                self._api.lint_findings(
                    vault_id=str(self._vault_id),
                    status='pending',
                    limit=200,
                ),
                timeout=2.0,
            )
            findings = payload.get('findings') or []
            return sum(1 for f in findings if f.get('rule_name') == 'propose_contradiction_winner')
        except Exception as e:
            logger.debug('pending_winner_proposals fetch failed: %s', e)
            return None

    # -- Tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:  # type: ignore[override]
        """Return the static tool schemas.

        IMPORTANT: Hermes calls ``get_tool_schemas()`` at provider *registration
        time* — before ``initialize()`` runs — to build its
        ``_tool_to_provider`` dispatch map. If we returned ``[]`` here because
        ``self._config is None``, Hermes would register zero memex tools and
        every subsequent call from the model would fall through to the
        "Unknown tool" error path.

        The schemas are static module-level constants; there's no reason to
        gate them on runtime state pre-init.

        Memory-mode contracts (post-init):
        - ``hybrid`` (default): full ``ALL_SCHEMAS`` surface — briefing +
          prefetch + every Memex verb the agent can dispatch.
        - ``context``: empty list — context-only mode, no tool dispatch.
        - ``tools``: minimal ``TOOLS_MODE_SCHEMAS`` (primary 7) — briefing +
          prefetch are skipped, so we hand the agent only the LLM-most-used
          verbs and trust it to compose them. The narrow surface is the
          contract; do not auto-expand it when new MCP verbs land.
        """
        if self._config is None:
            return list(ALL_SCHEMAS)
        if self._config.memory_mode == 'context':
            return []
        if self._config.memory_mode == 'tools':
            return list(TOOLS_MODE_SCHEMAS)
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
            asset_cache=self._asset_cache,
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
                # Pin-chain consumer identity (V7 §19.8): the
                # per-agent app context is app:hermes:<agent_identity>
                # so a trader and a researcher get different pinned
                # procedure cards without new identity plumbing.
                app=(f'hermes:{self._agent_identity}' if self._agent_identity else 'hermes'),
            )
            self._refresh_vault_binding()

    def _refresh_vault_binding(self) -> None:
        """Best-effort re-resolve the active vault.

        Once the session has committed to a vault — either because the
        create landed (``_note_initialized``) OR because a create is
        already queued with a snapshotted ``vault_id`` — we hold the
        binding constant. Letting the active vault drift after the queued
        create would land the create in vault A but every subsequent
        snapshotted-against-B append in vault B, splitting the transcript.

        The gate is checked twice: once as a fast-path before the network
        calls, and once again under the lock at the point of mutation. The
        second check closes the TOCTOU window where a queued create could
        appear (or land) while the network calls were in flight.
        """
        if self._api is None or self._config is None:
            return
        if self._committed_to_vault():
            return
        try:
            new_vault_name = resolve_vault(
                self._api,
                project_id=self._project_id,
                agent_identity=self._agent_identity or None,
                user_id=self._user_id,
                config_vault=self._config.vault_id,
            )
        except Exception as e:
            logger.debug('Vault re-resolution failed: %s', e)
            return
        if not new_vault_name or new_vault_name == self._vault_name:
            return
        new_vault_id = self._resolve_or_create_vault_id(new_vault_name)
        if new_vault_id is None:
            return
        with self._state_lock:
            # Re-validate under the lock at the point of mutation: a create
            # may have been enqueued or initialized while the network calls
            # above were in flight. Mutating the binding now would split
            # the transcript across vaults.
            if self._note_initialized or any(p['kind'] == 'create' for p in self._pending):
                return
            self._vault_name = new_vault_name
            self._vault_id = new_vault_id

    def _committed_to_vault(self) -> bool:
        """True if the session has effectively bound a vault.

        Either the create has succeeded (``_note_initialized``) or a
        create is queued with a snapshotted ``vault_id``. Reads of
        ``_pending`` are protected by ``_state_lock``; ``_note_initialized``
        is sampled cheaply outside the lock as a fast-path.
        """
        if self._note_initialized:
            return True
        with self._state_lock:
            return any(p['kind'] == 'create' for p in self._pending)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = '') -> None:  # type: ignore[override]
        """Buffer the turn locally. Flushes happen at chunk boundaries
        (``on_pre_compress`` / ``on_session_end`` / ``shutdown``)."""
        with self._state_lock:
            self._turn_buffer.append({'user': user_content, 'assistant': assistant_content})

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:  # type: ignore[override]
        if self._api is None or self._config is None:
            return
        chunk = self._capture_unflushed_buffer_slice()
        if not chunk and messages:
            # Degenerate case: ``sync_turn`` was never called this session
            # (some Hermes deployments only fire on_session_end). Trust
            # Hermes' history as the fallback source.
            retain = self._config.retain if self._config else RetainConfig()
            cleaned = preprocess_turns(
                messages,
                strip_system_prompts=retain.strip_system_prompts,
                strip_system_metadata=retain.strip_system_metadata,
                strip_html_content=retain.strip_html_content,
                html_content_threshold=retain.html_content_threshold,
            )
            if passes_quality_gate(
                cleaned,
                min_turns=retain.min_capture_turns,
                min_content_chars=retain.min_capture_chars,
            ):
                chunk = format_transcript(cleaned)
            else:
                chunk = ''
        if chunk:
            self._enqueue_chunk(chunk, title=self._format_session_title())
        with self._state_lock:
            self._turn_buffer = []
            self._flushed_index = 0

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:  # type: ignore[override]
        """Flush the unflushed buffer slice so soon-to-be-evicted turns are
        persisted before Hermes compacts them.

        Returns a short summary that Hermes includes in the compression prompt.
        We trust the local buffer as the verbatim source of truth — Hermes'
        ``messages`` parameter is used only to size the summary string.
        """
        if self._api is None or self._config is None:
            return ''
        chunk = self._capture_unflushed_buffer_slice()
        if chunk:
            self._enqueue_chunk(chunk, title=self._format_session_title())
        return (
            f'Memex captured {len(messages)} pre-compression messages into '
            f'session note `{self._session_note_key}`.'
        )

    def on_memory_write(self, action: str, target: str, content: str) -> None:  # type: ignore[override]
        """Mirror built-in MEMORY.md/USER.md writes to the Memex KV store.

        Target → namespace mapping preserves semantic intent so the mirror
        lands in the same namespace as an explicit ``memex_kv_put`` would:

        - ``target='user'`` → ``user:hermes:<digest>`` (user-scoped facts)
        - ``target='memory'`` → ``app:hermes:memory:<digest>`` (agent scratchpad)
        """
        if self._api is None or action == 'remove' or not content:
            return
        import hashlib

        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]
        if target == 'user':
            key = f'user:hermes:{digest}'
        else:
            key = f'app:hermes:{target}:{digest}'
        try:
            run_sync(self._api.kv_put(value=content, key=key), timeout=10.0)
        except Exception as e:
            logger.debug('KV mirror failed for %s: %s', key, e)

    # -- Shutdown ------------------------------------------------------------

    def shutdown(self) -> None:  # type: ignore[override]
        """Flush pending buffers and close the client.

        Idempotent: the second call observes ``_shutdown_started`` and
        returns. Concurrent calls (Hermes' shutdown + atexit fallback) are
        gated by ``_atexit_lock`` so the teardown happens exactly once.

        The async-bridge loop is **process-lifetime** and is NOT torn down
        here — closing it mid-process races with concurrent ``run_sync``
        callers (prefetch worker, briefing fetcher, in-flight tool
        dispatches) and surfaces as ``RuntimeError('Event loop is
        closed')``. The daemon thread owning the loop is reaped at process
        exit. See ``async_bridge`` module docstring for the lifetime
        invariant.
        """
        with self._atexit_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True

        if self._api is not None:
            chunk = self._capture_unflushed_buffer_slice()
            if chunk:
                try:
                    self._enqueue_chunk(chunk, title=self._format_session_title())
                except Exception as e:
                    logger.debug('Shutdown enqueue failed: %s', e)
            try:
                self._drain_pending()
            except Exception as e:
                logger.debug('Shutdown drain failed: %s', e)
        client = self._client
        self._client = None
        self._api = None
        if client is not None:
            try:
                run_sync(client.aclose(), timeout=5.0)
            except Exception:
                pass
        cache = self._asset_cache
        self._asset_cache = None
        if cache is not None:
            try:
                cache.cleanup()
            except Exception:
                pass

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

    def _capture_unflushed_buffer_slice(self) -> str:
        """Atomically read and watermark the unflushed slice of the buffer.

        Returns the formatted-transcript string for ``buffer[flushed_index:]``
        and advances ``_flushed_index`` to the current buffer length. The
        watermark moves on capture, not on successful flush — the pending
        queue handles retries via stable ``append_id``s, so we never need to
        reread the same buffer slice twice.

        The raw turns are preprocessed (system-prompt stripping, HTML
        sanitization) and quality-gated before formatting.
        """
        with self._state_lock:
            unflushed = self._turn_buffer[self._flushed_index :]
            if not unflushed:
                return ''
            snapshot = list(unflushed)
            # Pin the watermark target to the snapshot range so concurrent
            # sync_turn appends are not silently skipped.
            end_index = self._flushed_index + len(snapshot)
            retain = self._config.retain if self._config else RetainConfig()

        cleaned = preprocess_turns(
            snapshot,
            strip_system_prompts=retain.strip_system_prompts,
            strip_system_metadata=retain.strip_system_metadata,
            strip_html_content=retain.strip_html_content,
            html_content_threshold=retain.html_content_threshold,
        )
        if not passes_quality_gate(
            cleaned,
            min_turns=retain.min_capture_turns,
            min_content_chars=retain.min_capture_chars,
        ):
            logger.info(
                'Transcript quality gate rejected session (%d turns, %d content chars)',
                len(cleaned),
                _content_chars(cleaned),
            )
            with self._state_lock:
                self._flushed_index = max(self._flushed_index, end_index)
            return ''
        formatted = format_transcript(cleaned)
        if not formatted.strip():
            with self._state_lock:
                self._flushed_index = max(self._flushed_index, end_index)
            return ''
        with self._state_lock:
            if self._flushed_index >= end_index:
                return ''
            self._flushed_index = end_index
        return formatted

    def _enqueue_chunk(self, content: str, *, title: str) -> None:
        """Enqueue a transcript chunk and try to drain the pending queue.

        First chunk of the session (when ``_note_initialized`` is False AND
        no other create is pending) is enqueued as a ``create``; everything
        else is an ``append`` with a freshly-minted ``append_id`` for
        idempotent retry semantics.

        Backpressure: at cap we drop the NEW chunk rather than evicting
        an older one. Older transcript content is more valuable for
        downstream reflection (it is the part the model has already
        forgotten), and evicting it silently would re-introduce the
        original missing-chunks bug under sustained outage.
        """
        if not content.strip():
            return
        with self._state_lock:
            if len(self._pending) >= _PENDING_MAX:
                logger.error(
                    'Pending session-note write queue at cap (%d); dropping new chunk '
                    '(%d bytes). Investigate: Memex unreachable for the full session?',
                    _PENDING_MAX,
                    len(content),
                )
                return
            has_pending_create = any(p['kind'] == 'create' for p in self._pending)
            vault_id = str(self._vault_id) if self._vault_id else None
            if not self._note_initialized and not has_pending_create:
                entry: dict[str, Any] = {
                    'kind': 'create',
                    'content': content,
                    'title': title,
                    'vault_id': vault_id,
                }
            else:
                entry = {
                    'kind': 'append',
                    'content': content,
                    'append_id': uuid4(),
                    'vault_id': vault_id,
                }
            self._pending.append(entry)
        self._drain_pending()

    def _drain_pending(self) -> None:
        """Process the pending queue FIFO.

        Serialized via ``_drain_lock`` so concurrent callers (hooks,
        atexit, shutdown) take turns instead of racing on the head item.
        Successful items are popped; transient failures keep the head in
        place for retry; non-transient failures (4xx HTTP statuses)
        drop the failing entry so a poisoned item can't block the rest of
        the queue.

        Latency profile (A.2 trade-off): the 409-on-create branch waits
        up to ``_WAIT_FOR_NOTE_ROW_DEFAULT_TIMEOUT`` (120s) for the
        in-flight ingest to materialise. Callers on latency-sensitive
        paths (UI threads, outer timeouts) should be aware that
        ``on_pre_compress`` / ``on_session_end`` / ``shutdown`` can
        therefore block for up to 2 minutes when the overlap branch
        fires. The previous 10s ceiling was the root cause of the
        404+409 storm — see tech report Bug A for the trade-off.
        """
        if self._api is None or self._config is None:
            return
        if not self._drain_lock.acquire(blocking=False):
            return  # Another caller is already draining; let them.
        try:
            while True:
                with self._state_lock:
                    if not self._pending:
                        return
                    head = self._pending[0]
                try:
                    if head['kind'] == 'create':
                        self._do_create(
                            head['content'],
                            title=head['title'],
                            vault_id=head.get('vault_id'),
                        )
                        if not self._wait_for_note_row():
                            logger.warning(
                                'Session note row did not appear after ingest; '
                                'will retry on next flush.',
                            )
                            return
                        with self._state_lock:
                            self._note_initialized = True
                            if self._pending and self._pending[0] is head:
                                self._pending.pop(0)
                    else:
                        self._do_append(
                            head['content'],
                            append_id=head['append_id'],
                            vault_id=head.get('vault_id'),
                        )
                        with self._state_lock:
                            if self._pending and self._pending[0] is head:
                                self._pending.pop(0)
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code
                    # A.1: 409 on create means the server's overlap detector
                    # found an in-flight ingest for the same note_key (see
                    # packages/core/.../processing/batch.py overlap probe).
                    # The note row WILL materialise once the in-flight job
                    # finishes — wait for it rather than dropping the head
                    # (which would orphan the session note and break every
                    # subsequent append). The Location header on 409 points
                    # to the job; we poll the note row instead (deterministic
                    # id from note_key) since that's the materialisation
                    # signal we actually need.
                    if head['kind'] == 'create' and code == 409:
                        logger.info(
                            'Session note create returned 409 (overlap); '
                            'waiting for in-flight job at %s',
                            e.response.headers.get('Location', '<unknown>'),
                        )
                        if self._wait_for_note_row():
                            with self._state_lock:
                                self._note_initialized = True
                                if self._pending and self._pending[0] is head:
                                    self._pending.pop(0)
                            continue
                        logger.warning(
                            'Session note create overlap did not resolve '
                            'before deadline; will retry on next flush.',
                        )
                        return
                    if code in _NON_TRANSIENT_HTTP_STATUSES:
                        logger.error(
                            'Session note %s rejected (HTTP %d); dropping entry: %s',
                            head['kind'],
                            code,
                            e,
                        )
                        with self._state_lock:
                            if self._pending and self._pending[0] is head:
                                self._pending.pop(0)
                        continue
                    logger.warning(
                        'Session note %s transient error (HTTP %d); will retry: %s',
                        head['kind'],
                        code,
                        e,
                    )
                    return
                except _PROPAGATE_EXCEPTIONS:
                    # A.6: see _PROPAGATE_EXCEPTIONS docstring for rationale.
                    raise
                except Exception as e:
                    logger.warning(
                        'Session note %s failed; will retry on next flush: %s',
                        head['kind'],
                        e,
                    )
                    return
        finally:
            self._drain_lock.release()

    def _do_create(self, content: str, *, title: str, vault_id: str | None) -> None:
        """Create the session note via ``api.ingest``. May raise.

        Uses ``background=True`` so the LLM extraction phase runs
        asynchronously server-side. ``_drain_pending`` calls
        ``_wait_for_note_row`` afterwards to confirm the row is visible
        before issuing appends.
        """
        assert self._api is not None and self._config is not None
        from memex_common.schemas import NoteCreateDTO

        dto = NoteCreateDTO(
            name=title,
            description=f'Hermes session transcript ({self._session_id})',
            content=base64.b64encode(content.encode('utf-8')),
            note_key=self._session_note_key,
            vault_id=vault_id,
            tags=['hermes', self._agent_identity] if self._agent_identity else ['hermes'],
            author='hermes',
            template=self._config.retain.session_template or HERMES_SESSION_TEMPLATE,
        )
        run_sync(self._api.ingest(dto, background=True), timeout=30.0)

    def _wait_for_note_row(self, *, timeout: float = _WAIT_FOR_NOTE_ROW_DEFAULT_TIMEOUT) -> bool:
        """Poll for the session note row to appear server-side.

        Returns True if the row is visible, False on timeout. The id is
        deterministic from ``note_key`` so we can poll before the server
        finishes the background insert.

        Status semantics here differ from the append site:
        - **404** means "not yet visible" — keep polling. (At the append
          site, 404 means "note doesn't exist" and is hard-fail.)
        - **400 / 409 / 410 / 422** are non-transient — bail early; waiting
          won't change the outcome.
        - Everything else (5xx, 408, 429, network errors) is transient —
          retry within the deadline.

        A.2: exponential backoff between attempts (`_backoff_delay`). The
        previous flat 0.1s sleep produced 100 polls per 10s window, which
        was both too aggressive (burning CPU on tight loops) and too short
        (LLM extraction p99 well above 10s on resource-constrained hosts).
        New default timeout is 120s; backoff plateaus at 10s/attempt for
        the long tail.
        """
        if self._api is None:
            return False
        note_id = derive_note_uuid_from_key(self._session_note_key)
        deadline = time.monotonic() + timeout
        attempt = 0
        while time.monotonic() < deadline:
            try:
                # A.5: HEAD instead of GET so each poll is a pk-index hit
                # without hydrating the full Note model. Returns bool —
                # True for 200 (exists), False for 404 (not yet visible).
                # Other status codes raise httpx.HTTPStatusError via the
                # client and route through the branches below.
                if run_sync(self._api.head_note(note_id), timeout=1.0):
                    return True
                # head_note → False means 404 (still materialising) —
                # fall through to backoff.
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in _NON_TRANSIENT_HTTP_STATUSES and code != 404:
                    return False
                # 404 = not yet visible; other codes (5xx, 408, 429, etc.)
                # = transient — both fall through to backoff.
            except _PROPAGATE_EXCEPTIONS:
                # A.6: cancellation/shutdown — propagate, don't retry.
                raise
            except Exception as poll_exc:
                # Treat as transient and back off. Log at DEBUG so a hidden
                # programming error (e.g. AttributeError from a refactor of
                # the API client) is visible to ops who flip the level —
                # the previous bare-pass made these invisible for the
                # full 120s poll window.
                logger.debug('Note-row poll attempt %d failed: %s', attempt, poll_exc)
            # Bound the sleep so we don't overrun the deadline.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_backoff_delay(attempt), remaining))
            attempt += 1
        return False

    def _do_append(self, content: str, *, append_id: UUID, vault_id: str | None) -> None:
        """Append a delta to the existing session note. May raise.

        Idempotent on ``append_id``: the server replays a cached outcome
        if the same id was previously processed, so retries are safe.
        """
        assert self._api is not None
        run_sync(
            self._api.append_to_note(
                note_key=self._session_note_key,
                vault_id=vault_id,
                delta=content,
                append_id=append_id,
                joiner='paragraph',
            ),
            timeout=30.0,
        )


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    """Render a list of turn dicts as a structured markdown transcript.

    Delegates to ``transcript.format_transcript``.
    """
    return format_transcript(messages)


__all__ = ['MemexMemoryProvider']
