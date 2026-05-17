"""Pluggable answer-generation backends for evaluation suites.

Every backend produces a uniform ``AgentAnswer`` so ``ExpectedOutcome``
subclasses can score against a single shape. Built-in backends:

- ``api`` — direct ``RemoteMemexAPI`` calls (the default; tests memex's
  own surfaces under controlled conditions).
- ``claude-code`` — subprocess to the ``claude`` CLI with a temp
  workspace + ``.mcp.json`` pointing at the eval vault. Captures answer
  text + tool-call trace + retrieved unit IDs from the trace.
- ``hermes`` — runs the Hermes Agent in-process via its Python library
  (``run_agent.AIAgent``) with the bundled ``memex-hermes-plugin``
  symlinked into a temp ``HERMES_HOME``. No CLI/subprocess hop; setup
  happens automatically when the backend is instantiated.

Custom backends register via ``@register_backend('myname')`` on a
subclass of ``AnswerBackend``. Suites pick a backend per scenario via
``Scenario.answer_mode`` or per suite via
``SuiteMetadata.default_answer_mode``.
"""

from __future__ import annotations

import abc
import asyncio
import atexit
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from memex_common.client import RemoteMemexAPI

    from memex_eval.judge import Judge
    from memex_eval.suite.base import Scenario

logger = logging.getLogger('memex_eval.suite.agents')


# ---------------------------------------------------------------------------
# Uniform answer shape
# ---------------------------------------------------------------------------


class AgentAnswer(BaseModel):
    """Uniform output of every AnswerBackend.

    Different backends populate different subsets of fields:

    - ``api`` backend populates ``units`` / ``entities`` / ``cooccurrences``
      / ``lint_findings`` based on what the scenario's outcome needs.
    - ``claude-code`` / ``hermes`` populate ``answer_text``, ``tool_calls``,
      and parse ``retrieved_unit_ids`` from the trace.

    Outcomes' ``score()`` methods consume whichever fields are populated.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    answer_text: str | None = None
    units: list[Any] = Field(default_factory=list)
    entities: list[Any] = Field(default_factory=list)
    cooccurrences: list[Any] = Field(default_factory=list)
    entity_mentions: list[Any] = Field(default_factory=list)
    lint_findings: list[Any] = Field(default_factory=list)
    # Round-2 H3: number of memory_unit-targeted lint findings whose
    # ``unit_text`` enrichment failed. Surfaced on AgentAnswer (and by
    # the runner into ``actual_summary.lint_enrichment_failures``) so a
    # downstream "this scenario silently scored against empty strings"
    # case is visible in run artifacts (MLflow, JSON output) — not just
    # the live log.
    lint_enrichment_failures: int = 0
    lint_enrichment_attempted: int = 0
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_unit_ids: list[str] = Field(default_factory=list)
    kv_value: str | None = None
    summary_text: str | None = None
    duration_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    backend_name: str = ''
    raw_trace: dict[str, Any] | None = None
    # Full agent-session log captured during the scenario.
    # - claude-code: raw stream-json stdout (one JSON message per line).
    # - hermes: newline-delimited JSON of agent callback events
    #   (tool_start, tool_complete, step, thinking, etc.).
    # Runner uploads this verbatim as an MLflow artifact per scenario.
    session_log_text: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Backend ABC + registry
# ---------------------------------------------------------------------------


class AnswerBackend(abc.ABC):
    """Pluggable answer-generation strategy."""

    name: ClassVar[str] = ''

    @abc.abstractmethod
    async def answer(
        self,
        scenario: 'Scenario',
        *,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        server_url: str,
        judge: 'Judge | None' = None,
    ) -> AgentAnswer:
        """Produce an AgentAnswer for the given scenario.

        ``api`` and ``vault_id`` are always provided so backends can
        consult the live vault; ``server_url`` is provided so subprocess
        backends can configure their own clients.
        """


class _DictAttrShim:
    """Wraps a dict so consumers can use ``getattr(obj, 'rule_name', None)``.

    The lint endpoint returns plain dicts; outcome scoring uses ``getattr``
    by design to be tolerant of both Pydantic objects and dicts.
    """

    __slots__ = ('_d',)

    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def __getattr__(self, name: str) -> Any:
        try:
            return self._d[name]
        except KeyError as e:
            raise AttributeError(name) from e


_BACKEND_REGISTRY: dict[str, type[AnswerBackend]] = {}

# Backends use a slightly looser pattern than outcomes/setup-actions so the
# existing built-in name 'claude-code' (with a hyphen) keeps validating.
_BACKEND_NAME_RE = re.compile(r'^[a-z][a-z0-9_-]*$')


def register_backend(name: str):
    """Decorator: register a backend class under ``name``.

    Refuses to overwrite an existing registration. Use ``replace_backend``
    for explicit overrides (mainly tests).

    ::

        @register_backend('my-agent')
        class MyAgentBackend(AnswerBackend):
            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                ...
    """

    if not _BACKEND_NAME_RE.match(name):
        raise ValueError(f'Backend name {name!r} must match {_BACKEND_NAME_RE.pattern!r}')

    def deco(cls: type[AnswerBackend]) -> type[AnswerBackend]:
        existing = _BACKEND_REGISTRY.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(
                f'Backend {name!r} already registered to {existing.__qualname__}. '
                f'Use replace_backend() to override.'
            )
        if not getattr(cls, 'name', ''):
            cls.name = name
        _BACKEND_REGISTRY[name] = cls
        return cls

    return deco


def replace_backend(name: str):
    """Like ``register_backend`` but allows overriding an existing entry."""

    if not _BACKEND_NAME_RE.match(name):
        raise ValueError(f'Backend name {name!r} must match {_BACKEND_NAME_RE.pattern!r}')

    def deco(cls: type[AnswerBackend]) -> type[AnswerBackend]:
        if name in _BACKEND_REGISTRY:
            logger.warning(
                'Replacing backend %r (was %s, now %s)',
                name,
                _BACKEND_REGISTRY[name].__qualname__,
                cls.__qualname__,
            )
        if not getattr(cls, 'name', ''):
            cls.name = name
        _BACKEND_REGISTRY[name] = cls
        return cls

    return deco


def unregister_backend(name: str) -> None:
    _BACKEND_REGISTRY.pop(name, None)


def get_backend(name: str) -> AnswerBackend:
    """Resolve a backend by registered name. Raises KeyError on miss."""
    if name not in _BACKEND_REGISTRY:
        raise KeyError(f'Unknown answer backend {name!r}. Registered: {sorted(_BACKEND_REGISTRY)}')
    return _BACKEND_REGISTRY[name]()


def list_backends() -> list[str]:
    return sorted(_BACKEND_REGISTRY)


# ---------------------------------------------------------------------------
# Built-in: direct API
# ---------------------------------------------------------------------------


@register_backend('api')
class DirectApiBackend(AnswerBackend):
    """Default backend — calls ``RemoteMemexAPI`` directly.

    Dispatches on the scenario's ``ExpectedOutcome`` type so the right
    server endpoint is hit. Tests memex's own surfaces under controlled
    conditions, with no agent in the loop.
    """

    async def answer(
        self,
        scenario: 'Scenario',
        *,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        server_url: str,
        judge: 'Judge | None' = None,
    ) -> AgentAnswer:
        from memex_eval.suite.base import (
            EntityCooccurs,
            EntityMentionContains,
            EntityResolves,
            KvRoundtrip,
            LintFindingPresent,
            LLMLintFlagsUnit,
            SummaryNonempty,
        )

        started = time.monotonic()
        out = AgentAnswer(backend_name=self.name)
        outcome = scenario.expected
        try:
            if isinstance(outcome, EntityResolves):
                ents = await api.search_entities(
                    query=scenario.query, limit=scenario.top_k, vault_id=vault_id
                )
                out.entities = list(ents)
            elif isinstance(outcome, EntityCooccurs):
                found = await api.search_entities(query=scenario.query, limit=1, vault_id=vault_id)
                if found:
                    out.entities = [found[0]]
                    coocc = await api.get_entity_cooccurrences(found[0].id, vault_id=vault_id)
                    out.cooccurrences = list(coocc)
            elif isinstance(outcome, EntityMentionContains):
                name = outcome.expected_name or scenario.query
                found = await api.search_entities(query=name, limit=1, vault_id=vault_id)
                if found:
                    out.entities = [found[0]]
                    mentions = await api.get_entity_mentions(found[0].id, vault_id=vault_id)
                    out.entity_mentions = list(mentions)
            elif isinstance(outcome, KvRoundtrip):
                entry = await api.kv_get(key=outcome.kv_key)
                out.kv_value = getattr(entry, 'value', None) if entry is not None else None
            elif isinstance(outcome, SummaryNonempty):
                q = outcome.entity_query or scenario.query
                found = await api.search_entities(query=q, limit=1, vault_id=vault_id)
                if found:
                    out.entities = [found[0]]
                    try:
                        summary = await api.summarize_node(entity_id=found[0].id, vault_id=vault_id)
                        observations = getattr(summary, 'new_observations', None) or []
                        parts: list[str] = []
                        for obs in observations:
                            title = (getattr(obs, 'title', '') or '').strip()
                            content = (getattr(obs, 'content', '') or '').strip()
                            if title and content:
                                parts.append(f'{title}: {content}')
                            elif content:
                                parts.append(content)
                            elif title:
                                parts.append(title)
                        out.summary_text = '\n'.join(parts)
                    except Exception as exc:
                        out.error = f'summarize_node failed: {type(exc).__name__}: {exc}'
            elif isinstance(outcome, (LintFindingPresent, LLMLintFlagsUnit)):
                # Pull every pending finding (server cap is 500). The
                # default ``limit=50`` would silently truncate any vault
                # with a noisier lint pass.
                payload = await api.lint_findings(
                    vault_id=str(vault_id), status='pending', limit=500
                )
                # /lint/findings returns {'findings': [...], ...} — extract the list.
                raw = payload.get('findings') if isinstance(payload, dict) else payload
                if isinstance(payload, dict) and len(raw or []) >= 500:
                    logger.warning(
                        'lint_findings: returned %d rows at the 500-row cap for vault %s; '
                        'paging not implemented — assertions on later findings will be incomplete.',
                        len(raw or []),
                        vault_id,
                    )
                # Enrich each memory_unit-targeted finding with its unit
                # text; ``LLMLintFlagsUnit.score`` does substring matching
                # against the unit body but the lint API returns only
                # ``target_id``. Issue the resolutions concurrently to
                # avoid an N+1 wall-clock cost.
                items: list[dict[str, Any]] = []
                resolvable_ids_set: set[str] = set()
                for item in raw or []:
                    if not isinstance(item, dict):
                        items.append(item)  # type: ignore[arg-type]
                        continue
                    # Shallow copy: only top-level fields are mutated below
                    # (we set ``unit_text``). Nested dicts (``evidence``) still
                    # alias the source — fine because nothing here writes to them.
                    items.append(dict(item))
                    if (
                        items[-1].get('target_type') == 'memory_unit'
                        and items[-1].get('target_id')
                        and 'unit_text' not in items[-1]
                    ):
                        resolvable_ids_set.add(items[-1]['target_id'])

                async def _resolve_one(uid: str) -> tuple[str, str | None]:
                    try:
                        mu = await api.get_memory_unit(uid)
                        return uid, (getattr(mu, 'text', '') or '')
                    except Exception as exc:
                        logger.warning(
                            'lint enrich: get_memory_unit(%s) failed (%s: %s)',
                            uid,
                            type(exc).__name__,
                            exc,
                        )
                        return uid, None

                resolvable_ids = sorted(resolvable_ids_set)
                resolutions: dict[str, str | None] = {}
                if resolvable_ids:
                    pairs = await asyncio.gather(*(_resolve_one(u) for u in resolvable_ids))
                    resolutions = dict(pairs)

                # Count attempts BEFORE the resolution loop sets
                # ``item['unit_text']`` on every eligible finding. Both
                # ``failures`` and ``attempted`` walk the same predicate
                # (memory_unit-targeted, target_id set, unit_text not
                # already on the wire); counting after the loop would
                # always read 0 because every eligible finding gets
                # ``unit_text`` set during enrichment.
                lint_attempted = sum(
                    1
                    for item in items
                    if isinstance(item, dict)
                    and item.get('target_type') == 'memory_unit'
                    and item.get('target_id')
                    and 'unit_text' not in item
                )
                enrichment_failures = 0
                findings_list: list[Any] = []
                for item in items:
                    if not isinstance(item, dict):
                        findings_list.append(item)
                        continue
                    if (
                        item.get('target_type') == 'memory_unit'
                        and item.get('target_id')
                        and 'unit_text' not in item
                    ):
                        text = resolutions.get(item['target_id'])
                        if text is None:
                            enrichment_failures += 1
                            item['unit_text'] = ''
                        else:
                            item['unit_text'] = text
                    findings_list.append(_DictAttrShim(item))
                if enrichment_failures:
                    logger.warning(
                        'lint enrich: %d/%d memory-unit findings could not resolve '
                        'their unit text and were scored against empty strings — '
                        'keyword assertions on those findings will register no '
                        'matches.',
                        enrichment_failures,
                        lint_attempted,
                    )
                out.lint_findings = findings_list
                out.lint_enrichment_failures = enrichment_failures
                out.lint_enrichment_attempted = lint_attempted
            else:
                # Default: memory search (covers KeywordsPresent/Absent,
                # GoldUnitIds, RankingOrder, ExcludedByDefault, LLMJudge,
                # UsefulAtK, CompositeOutcome). Switch to note-search when
                # the scenario requests it.
                if getattr(scenario, 'search_type', 'memory') == 'note':
                    notes = await api.search_notes(
                        query=scenario.query,
                        limit=scenario.top_k,
                        vault_ids=[vault_id],
                    )
                    out.units = list(notes)
                    out.retrieved_unit_ids = [str(getattr(n, 'note_id', '')) for n in out.units]
                else:
                    kwargs: dict[str, Any] = {
                        'query': scenario.query,
                        'limit': scenario.top_k,
                        'vault_ids': [vault_id],
                    }
                    if scenario.strategies:
                        kwargs['strategies'] = scenario.strategies
                    if scenario.include_superseded is not None:
                        kwargs['include_superseded'] = scenario.include_superseded
                    if scenario.include_deprioritized is not None:
                        kwargs['include_deprioritized'] = scenario.include_deprioritized
                    units = await api.search(**kwargs)
                    out.units = list(units)
                    out.retrieved_unit_ids = [str(getattr(u, 'id', '')) for u in out.units]
        except Exception as e:
            out.error = f'{type(e).__name__}: {e}'
        out.duration_ms = (time.monotonic() - started) * 1000
        return out


# ---------------------------------------------------------------------------
# Built-in: Claude Code subprocess
# ---------------------------------------------------------------------------


_CLAUDE_MCP_TEMPLATE = """\
{{
  "mcpServers": {{
    "memex": {{
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "{workspace}", "run", "memex", "mcp", "run"],
      "env": {{
        "MEMEX_SERVER_URL": "{server_url}"
      }}
    }}
  }}
}}
"""


_CLAUDE_MD_NO_PLUGIN = """\
# Memex memory retrieval

You have access to Memex, a long-term memory system, via the MCP tool prefix
``memex_*``. The active vault is ``{vault_name}``. Use the Memex tools to
answer the user's question; do NOT rely on prior knowledge.

When answering:
1. Search the vault using ``memex_memory_search`` and/or ``memex_note_search``.
2. Optionally explore entity context with ``memex_get_entities`` /
   ``memex_get_entity_cooccurrences``.
3. Cite relevant memory unit IDs in your final answer.
4. Keep the answer concise and grounded in the retrieved evidence.
"""


_CLAUDE_MD_WITH_PLUGIN = """\
# Agent integration evaluation

You are answering an evaluation question against a Memex vault. The
active vault is ``{vault_name}``. All vault data is reached via the
Memex MCP tools (prefix ``memex_*``). Ground every answer in retrieved
evidence; do NOT use prior knowledge.

## Plugin briefing

Retrieval routing, KV-namespace conventions, citation discipline, and
tool-usage rules come from the memex Claude Code plugin (loaded
automatically via ``--plugin-dir``). Follow them.

## First action

Load schemas for the tools the suite exercises in a single ToolSearch
call:

```
ToolSearch(query="select:mcp__memex__memex_memory_search,mcp__memex__memex_note_search,\
mcp__memex__memex_find_note,mcp__memex__memex_get_page_indices,\
mcp__memex__memex_get_nodes,mcp__memex__memex_list_entities,\
mcp__memex__memex_get_entity_mentions,mcp__memex__memex_get_entity_cooccurrences,\
mcp__memex__memex_kv_write,mcp__memex__memex_append_note,\
mcp__memex__memex_set_note_status", max_results=11)
```

## Answer format

Reply concisely in plain text. The final message you send is the answer.
"""


# Allow-list of memex MCP tools the agent_integration suite exercises.
# Written into ``.claude/settings.local.json`` so Claude Code permits the
# tool calls without prompting; ``--permission-mode bypassPermissions``
# stays on the command line as belt-and-suspenders.
_MEMEX_TOOL_ALLOWLIST: tuple[str, ...] = (
    # retrieval
    'mcp__memex__memex_memory_search',
    'mcp__memex__memex_note_search',
    'mcp__memex__memex_find_note',
    'mcp__memex__memex_survey',
    'mcp__memex__memex_recent_notes',
    'mcp__memex__memex_search_user_notes',
    'mcp__memex__memex_read_note',
    'mcp__memex__memex_get_page_indices',
    'mcp__memex__memex_get_nodes',
    'mcp__memex__memex_get_notes_metadata',
    # entity graph
    'mcp__memex__memex_list_entities',
    'mcp__memex__memex_get_entities',
    'mcp__memex__memex_get_entity_mentions',
    'mcp__memex__memex_get_entity_cooccurrences',
    'mcp__memex__memex_get_memory_units',
    'mcp__memex__memex_get_memory_links',
    'mcp__memex__memex_get_lineage',
    'mcp__memex__memex_get_vault_summary',
    'mcp__memex__memex_list_vaults',
    'mcp__memex__memex_active_vault',
    # lifecycle writes
    'mcp__memex__memex_add_note',
    'mcp__memex__memex_append_note',
    'mcp__memex__memex_set_note_status',
    'mcp__memex__memex_rename_note',
    # KV
    'mcp__memex__memex_kv_write',
    'mcp__memex__memex_kv_get',
    'mcp__memex__memex_kv_list',
    'mcp__memex__memex_kv_search',
    # assets
    'mcp__memex__memex_list_assets',
    'mcp__memex__memex_get_resources',
    'mcp__memex__memex_add_assets',
    'mcp__memex__memex_delete_assets',
)


def _resolve_suite_plugin_dir() -> Path | None:
    """Locate the memex Claude Code plugin directory for the suite backend.

    Resolution order:
      1. ``MEMEX_CLAUDE_PLUGIN_DIR`` env var.
      2. ``<workspace_root>/packages/claude-code-plugin/`` (monorepo default;
         workspace root located by ascending until ``pyproject.toml`` +
         ``packages/`` are siblings).

    Returns ``None`` (with a logged warning) if neither candidate has a
    ``.claude-plugin/plugin.json`` manifest. The backend then falls back
    to plugin-less invocation; suite scenarios that depend on plugin
    briefing (KV namespaces, append_note, citation discipline) will fail
    loudly — that's the intended signal.
    """
    candidates: list[Path] = []
    env_override = os.environ.get('MEMEX_CLAUDE_PLUGIN_DIR')
    if env_override:
        candidates.append(Path(env_override))
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / 'pyproject.toml').is_file() and (parent / 'packages').is_dir():
            candidates.append(parent / 'packages' / 'claude-code-plugin')
            break
    for cand in candidates:
        if (cand / '.claude-plugin' / 'plugin.json').is_file():
            logger.info('memex Claude Code plugin resolved at %s', cand)
            return cand
    logger.warning(
        'memex Claude Code plugin not found (checked: %s). '
        'Suite scenarios that depend on plugin briefing will likely fail.',
        ', '.join(str(c) for c in candidates) or '<no candidates>',
    )
    return None


@register_backend('claude-code')
class ClaudeCodeBackend(AnswerBackend):
    """Spawn the ``claude`` CLI as a subagent with Memex MCP wired up.

    Builds a temporary workspace with ``.mcp.json``, ``CLAUDE.md``, and
    ``.claude/settings.local.json``, spawns ``claude -p <question>`` with
    streaming JSON output, and parses the trace for the final answer +
    tool calls + retrieved unit IDs.

    The memex Claude Code plugin (resolved via ``_resolve_suite_plugin_dir``)
    is mounted via ``--plugin-dir`` so the agent gets the same briefing
    Hermes gets via the ``memex-hermes-plugin``. If the plugin can't be
    found, the backend falls back to plugin-less invocation and logs a
    warning.

    The model defaults to ``claude-sonnet-4-6`` (matching longmemeval's
    pin) and is overridable via ``MEMEX_EVAL_CLAUDE_MODEL`` for
    reproducible per-shell runs.

    Requires ``claude`` on the PATH.
    """

    DEFAULT_MODEL: ClassVar[str] = 'claude-sonnet-4-6'

    def __init__(self, claude_bin: str | None = None, timeout_s: float = 300.0) -> None:
        self.claude_bin: str = claude_bin or os.environ.get('CLAUDE_BIN') or 'claude'
        self.timeout_s = timeout_s
        self.model: str = os.environ.get('MEMEX_EVAL_CLAUDE_MODEL') or self.DEFAULT_MODEL
        self.plugin_dir: Path | None = _resolve_suite_plugin_dir()
        # Per-suite tmpdir cache: same vault_id → same workspace path →
        # same plugin-resolved project_id across scenarios in one suite run.
        # Cross-scenario KV reads (e.g. ``kv_retrieves_convention``
        # depending on ``kv_writes_project_preference``) need a stable
        # project namespace. Cleanup runs at process exit.
        self._suite_workspaces: dict[str, Path] = {}

    def _suite_workspace(self, vault_id: UUID) -> Path:
        key = str(vault_id)
        if key not in self._suite_workspaces:
            workspace = Path(tempfile.mkdtemp(prefix='memex-eval-claude-'))
            self._suite_workspaces[key] = workspace
            atexit.register(shutil.rmtree, str(workspace), ignore_errors=True)
        return self._suite_workspaces[key]

    def _executable_path(self) -> str:
        """Binary that must be on PATH for this backend; subclasses override."""
        return self.claude_bin

    def _executable_missing_error(self) -> str:
        return f'{self.claude_bin!r} not on PATH; install Claude Code or set CLAUDE_BIN'

    def _build_claude_flags(self, scenario: 'Scenario') -> list[str]:
        """Flags passed to the claude CLI after the executable (or after the
        ``--`` separator when launched via a wrapper like ``ollama launch``).
        Does NOT include ``--model`` — wrappers may inject that themselves."""
        flags: list[str] = []
        if self.plugin_dir is not None:
            flags += ['--plugin-dir', str(self.plugin_dir)]
        flags += [
            '-p',
            scenario.query,
            '--output-format',
            'stream-json',
            '--include-partial-messages',
            '--verbose',
            '--permission-mode',
            'bypassPermissions',
            # Isolate from the operator's global MCP config. Without this
            # the subprocess also loads the operator's globally-configured
            # ``memex`` MCP server (alongside the suite-provided plugin
            # memex), which typically points at a different vault — the
            # agent then sees two ``memex_*`` tool surfaces and picks
            # whichever, returning empty results from the wrong store.
            # This is a test-rig requirement (the eval must point at the
            # eval server, not whatever else the operator has running);
            # all other built-in tools (Bash, Read, Grep, Glob, Write,
            # Edit, Task, etc.) remain enabled because real plugin users
            # have them and the plugin's agent_surface / harnesses are
            # responsible for keeping the agent focused on memex tools
            # for content.
            '--strict-mcp-config',
            '--mcp-config',
            '.mcp.json',
        ]
        return flags

    def _build_subprocess_cmd(self, scenario: 'Scenario') -> list[str]:
        return [
            self.claude_bin,
            '--model',
            self.model,
            *self._build_claude_flags(scenario),
        ]

    async def answer(
        self,
        scenario: 'Scenario',
        *,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        server_url: str,
        judge: 'Judge | None' = None,
    ) -> AgentAnswer:
        if not shutil.which(self._executable_path()):
            return AgentAnswer(
                backend_name=self.name,
                error=self._executable_missing_error(),
            )

        out = AgentAnswer(backend_name=self.name)
        started = time.monotonic()
        # Resolve workspace root for `uv --directory` of the memex MCP runner.
        # Prefer an explicit env override (works for installed wheels and CI).
        env_root = os.environ.get('MEMEX_PROJECT_DIR')
        if env_root and Path(env_root).is_dir():
            workspace_root = env_root
        else:
            # Fall back to ascending until we find a `pyproject.toml` with the
            # memex workspace marker. Bare parents[5] is brittle in installed wheels.
            here = Path(__file__).resolve()
            workspace_root = ''
            for parent in here.parents:
                if (parent / 'pyproject.toml').is_file() and (parent / 'packages').is_dir():
                    workspace_root = str(parent)
                    break
            if not workspace_root:
                return AgentAnswer(
                    backend_name=self.name,
                    error=(
                        'Could not locate memex workspace root for MCP server; '
                        'set MEMEX_PROJECT_DIR=/path/to/memex-repo'
                    ),
                )

        # Vault name the agent should see (best-effort; fall back to vault_id str)
        vault_name = ''
        try:
            for v in await api.list_vaults():
                if v.id == vault_id:
                    vault_name = v.name
                    break
        except Exception:
            pass

        # The MCP server's lifespan appends ``/api/v1/`` to MEMEX_SERVER_URL
        # itself; passing the eval's full ``…/api/v1/`` URL would double the
        # prefix and 404 every MCP call. Strip before injecting.
        mcp_server_url = server_url.rstrip('/')
        if mcp_server_url.endswith('/api/v1'):
            mcp_server_url = mcp_server_url[: -len('/api/v1')]

        # Per-suite stable workspace: same vault_id across scenarios →
        # same path → same plugin-resolved project_id, so
        # ``kv_writes_project_preference`` and ``kv_retrieves_convention``
        # share the ``project:<workspace>:…`` namespace. The dir lives for
        # the lifetime of the backend instance (one process) and is cleaned
        # at exit by the atexit handler registered in ``_suite_workspace``.
        tmpdir = self._suite_workspace(vault_id)
        # Bind the eval vault to this suite's project_id so the CC
        # plugin's SessionStart hook resolves it as the active vault. The
        # plugin's resolver derives project_id from either a real git
        # remote or, lacking one, the PWD. We deliberately do NOT create a
        # synthetic git remote (it isn't a real project) — let the plugin
        # fall through to the path branch, and bind the KV under the same
        # path the plugin will compute.
        if vault_name:
            try:
                await api.kv_put(
                    value=vault_name,
                    key=f'app:claude-code:project:{tmpdir}:vault',
                )
            except Exception as _kv_err:
                logger.warning(
                    'Failed to bind project KV → vault (%s); the CC plugin '
                    'may not resolve the active vault for scenario %r',
                    _kv_err,
                    scenario.id,
                )
        (tmpdir / '.mcp.json').write_text(
            _CLAUDE_MCP_TEMPLATE.format(workspace=workspace_root, server_url=mcp_server_url)
        )
        md_template = (
            _CLAUDE_MD_WITH_PLUGIN if self.plugin_dir is not None else _CLAUDE_MD_NO_PLUGIN
        )
        (tmpdir / 'CLAUDE.md').write_text(
            md_template.format(vault_name=vault_name or str(vault_id))
        )
        claude_dir = tmpdir / '.claude'
        claude_dir.mkdir(exist_ok=True)
        # Block Claude Code's built-in auto-memory skill from
        # intercepting "remember X" / "save this" intents that should
        # route to ``memex_kv_write``. Auto-memory persists to
        # ``~/.claude/projects/<id>/memory/MEMORY.md`` via the ``Write``
        # tool; denying writes to that path forces the agent to use
        # the memex KV layer instead. The path glob covers both the
        # per-project sub-tree and the bare project root in case the
        # skill ever stores at a sibling location.
        _AUTO_MEMORY_DENY = (
            'Write(/home/*/.claude/projects/**)',
            'Edit(/home/*/.claude/projects/**)',
        )
        (claude_dir / 'settings.local.json').write_text(
            json.dumps(
                {
                    'permissions': {
                        'allow': list(_MEMEX_TOOL_ALLOWLIST),
                        'deny': list(_AUTO_MEMORY_DENY),
                    }
                },
                indent=2,
            )
        )
        cmd = self._build_subprocess_cmd(scenario)
        # Inject MEMEX_LOCAL_PATH so the plugin's SessionStart hook
        # resolves `memex` against the workspace checkout (where this
        # branch's agent-surface refactor lives) instead of `uvx --from
        # git+https://github.com/.../memex@latest`. Explicit env values
        # are respected so callers can opt out for distribution tests.
        child_env = os.environ.copy()
        if 'MEMEX_LOCAL_PATH' not in os.environ:
            child_env['MEMEX_LOCAL_PATH'] = workspace_root
        # Disable Claude Code's built-in auto-memory skill (undocumented
        # env var per anthropics/claude-code#23750). Without this the
        # agent's "remember X" intent is intercepted into
        # ``~/.claude/projects/<id>/memory/MEMORY.md`` via the Write
        # tool, and — worse for eval correctness — the auto-memory layer
        # reads from the user's PRE-EXISTING auto-memory store and
        # surfaces unrelated session content as if it came from the
        # eval vault. Empirically this contaminated sonnet's pass rate
        # by ≥30 percentage points (NVIDIA/Exxon/Google Finance content
        # leaking into Acme-Corp scenarios). Disabling auto-memory
        # forces every retrieval through the memex MCP, which is the
        # eval's measurement target.
        if 'CLAUDE_CODE_DISABLE_AUTO_MEMORY' not in os.environ:
            child_env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] = '1'
        # Force the plugin's bash hooks (which call ``memex`` CLI for
        # KV vault resolution, briefing fetch, etc.) onto the eval
        # server. Without this override, the operator's
        # ``MEMEX_SERVER_URL`` env (e.g. their personal memex) leaks
        # into the subprocess and the plugin's ``memex kv get`` reads
        # from the wrong store — so the project→vault binding written
        # by this backend to the eval server is invisible, and the
        # SessionStart hook concludes "No vault set" even though MCP
        # (configured via ``.mcp.json``) is correctly bound. API key
        # is dropped because the eval server doesn't require auth and
        # the operator's key would belong to a different server.
        child_env['MEMEX_SERVER_URL'] = mcp_server_url
        child_env.pop('MEMEX_API_KEY', None)
        # Make the eval vault the default reader/writer for the MCP
        # server's own fallback chain. Without this, an agent that
        # doesn't pass ``vault_ids`` (Sonnet routinely omits it; GLM
        # threads it through) hits ``config.read_vaults`` →
        # ``[server.default_reader_vault]`` → "global" → empty
        # search results. The MCP config object reads
        # ``MEMEX_VAULT__ACTIVE`` via the ``MEMEX_`` env prefix and
        # nested-delimiter ``__``, so this is the cleanest hook to
        # bind both write and read defaults to the eval vault
        # without requiring agent-side vault_ids discipline.
        if vault_name:
            child_env['MEMEX_VAULT__ACTIVE'] = vault_name
        try:
            proc = subprocess.run(
                cmd,
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            out.error = f'{cmd[0]} subprocess timed out after {self.timeout_s}s'
            out.duration_ms = (time.monotonic() - started) * 1000
            return out

        if proc.returncode != 0:
            out.error = f'{cmd[0]} exited {proc.returncode}: {(proc.stderr or "").strip()[:500]}'

        # Stash the raw stream-json verbatim for MLflow artifact upload.
        out.session_log_text = proc.stdout or ''

        # Parse stream-json trace: one JSON-encoded message per line.
        for line in (proc.stdout or '').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            _absorb_claude_message(msg, out)

        out.duration_ms = (time.monotonic() - started) * 1000
        return out


@register_backend('ollama-claude')
class OllamaClaudeBackend(ClaudeCodeBackend):
    """Spawn ``claude`` via ``ollama launch claude`` so the model is supplied by Ollama.

    Identical to ``ClaudeCodeBackend`` (workspace setup, plugin mount,
    stream-json parsing, session-log capture) except for the subprocess
    prefix:

        ollama launch claude --model <model> -- <standard claude flags>

    Use this when you don't have ANTHROPIC_API_KEY but do have Ollama
    Cloud access (``OLLAMA_API_KEY`` for hosted ``:cloud`` models, or a
    local model registered with ``ollama pull``). The default model is
    ``glm-5.1:cloud`` — override via ``MEMEX_EVAL_OLLAMA_CLAUDE_MODEL``.

    Requires both ``ollama`` and ``claude`` on the PATH; the former
    launches the latter and routes its model traffic through Ollama.
    """

    DEFAULT_MODEL: ClassVar[str] = 'glm-5.1:cloud'

    def __init__(self, ollama_bin: str | None = None, timeout_s: float = 300.0) -> None:
        super().__init__(timeout_s=timeout_s)
        self.ollama_bin: str = ollama_bin or os.environ.get('OLLAMA_BIN') or 'ollama'
        # The parent picks up MEMEX_EVAL_CLAUDE_MODEL; we want a separate
        # env var so the two backends can coexist with different defaults
        # in the same shell.
        self.model = os.environ.get('MEMEX_EVAL_OLLAMA_CLAUDE_MODEL') or self.DEFAULT_MODEL

    def _executable_path(self) -> str:
        return self.ollama_bin

    def _executable_missing_error(self) -> str:
        return f'{self.ollama_bin!r} not on PATH; install Ollama or set OLLAMA_BIN'

    def _build_subprocess_cmd(self, scenario: 'Scenario') -> list[str]:
        return [
            self.ollama_bin,
            'launch',
            'claude',
            '--model',
            self.model,
            '--',
            *self._build_claude_flags(scenario),
        ]


def _strip_mcp_prefix(name: str) -> str:
    """Strip the ``mcp__<server>__`` prefix Claude Code adds to MCP tool names.

    Claude Code's stream-json emits memex MCP tools as
    ``mcp__memex__memex_memory_search``; suite outcomes
    (``ToolCallContains`` etc.) compare against bare names like
    ``memex_memory_search`` so the same scenarios work under both
    ``hermes`` and ``claude-code`` backends. Non-prefixed names pass
    through unchanged so this is safe to call on any source.
    """
    if name.startswith('mcp__'):
        parts = name.split('__', 2)
        if len(parts) == 3:
            return parts[2]
    return name


def _absorb_claude_message(msg: dict[str, Any], out: AgentAnswer) -> None:
    """Pull answer text, tool calls, and retrieved unit IDs from a stream-json message."""
    msg_type = msg.get('type')
    if msg_type == 'result':
        # Final assistant message
        if 'result' in msg and isinstance(msg['result'], str):
            out.answer_text = msg['result']
        usage = msg.get('usage') or {}
        # Claude stream-json splits input tokens across three fields when
        # prompt caching is in play: ``input_tokens`` (fresh / non-cached),
        # ``cache_creation_input_tokens`` (written into cache this turn),
        # and ``cache_read_input_tokens`` (served from cache). All three
        # are billed and represent the actual input volume the model
        # processed. Counting only the fresh slice under-reported a sonnet
        # 29-scenario run as 28K-in vs 50K-out — visibly impossible —
        # because the static MCP-tool-description prefix is cached on
        # every turn after the first.
        out.tokens_in += int(usage.get('input_tokens', 0) or 0)
        out.tokens_in += int(usage.get('cache_creation_input_tokens', 0) or 0)
        out.tokens_in += int(usage.get('cache_read_input_tokens', 0) or 0)
        out.tokens_out += int(usage.get('output_tokens', 0) or 0)
        if 'total_cost_usd' in msg:
            out.cost_usd += float(msg.get('total_cost_usd', 0.0) or 0.0)
    elif msg_type in ('assistant', 'user'):
        message = msg.get('message') or {}
        for block in message.get('content', []) or []:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use':
                tool_name = _strip_mcp_prefix(block.get('name', ''))
                tool_input = block.get('input', {})
                out.tool_calls.append({'tool': tool_name, 'input': tool_input})
                # Memex MCP search tools return unit lists in the result.
            elif block.get('type') == 'tool_result':
                _extract_unit_ids_from_tool_result(block, out)


# Tool-name prefixes that emit memory-unit IDs in their results. Used by
# the answer-text UUID fallback to avoid scraping non-unit UUIDs (vault,
# note, entity, citation block) that the agent may mention in unrelated
# answers.
_SEARCH_TOOL_PREFIXES: tuple[str, ...] = (
    'memex_memory_search',
    'memex_note_search',
    'memex_survey',
    'memex_search_user_notes',
    'memex_recent_notes',
    'memex_find_note',
)


def _extract_unit_ids_from_tool_result(block: dict[str, Any], out: AgentAnswer) -> None:
    content = block.get('content')
    text_blob = ''
    if isinstance(content, str):
        text_blob = content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'text':
                text_blob += str(part.get('text', '')) + '\n'
            elif isinstance(part, str):
                text_blob += part + '\n'
    # Memex MCP results carry unit IDs as UUIDs in JSON; pull them via simple regex.
    uuid_pattern = re.compile(
        r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
        re.IGNORECASE,
    )
    for match in uuid_pattern.findall(text_blob):
        if match not in out.retrieved_unit_ids:
            out.retrieved_unit_ids.append(match)


# ---------------------------------------------------------------------------
# Built-in: Hermes Python library (no subprocess, no CLI)
# ---------------------------------------------------------------------------


@register_backend('hermes')
class HermesBackend(AnswerBackend):
    """Run the Hermes Agent via its Python library (``run_agent.AIAgent``).

    The eval framework owns the integration setup end-to-end — no
    ``memex hermes install``, no ``hermes`` CLI on PATH. On first use,
    the backend:

    1. Creates a temp ``HERMES_HOME`` directory.
    2. Symlinks the bundled ``memex-hermes-plugin`` directory
       (``memex_hermes_plugin.PLUGIN_DIR``) into
       ``$HERMES_HOME/plugins/memex/``.
    3. Writes a minimal ``config.yaml`` selecting the memex provider.
    4. Sets ``MEMEX_SERVER_URL`` and ``MEMEX_VAULT`` so the plugin binds
       to the eval vault.

    Then for each scenario, it instantiates ``AIAgent`` and calls
    ``run_conversation(scenario.query)``, capturing the final answer,
    tool calls, and per-session token / cost stats.

    Required (single canonical install command — used everywhere):

        uv sync --extra hermes --group hermes-integration

    That pulls in ``hermes-agent`` (Python library) and
    ``memex-hermes-plugin`` (workspace package) together.

    Plus an LLM API key for the Hermes agent itself. The backend routes
    by model prefix (see ``_PROVIDER_KEY_ORDER``):

    - ``gemini/*`` → ``GOOGLE_API_KEY`` or ``GEMINI_API_KEY``
    - ``anthropic/*`` → ``ANTHROPIC_API_KEY``
    - ``openai/*`` → ``OPENAI_API_KEY``
    - ``openrouter/*`` → ``OPENROUTER_API_KEY``
    - ``ollama/*`` / ``ollama_chat/*`` → ``OLLAMA_API_KEY`` (Ollama
      Cloud / Turbo; also set ``OLLAMA_API_BASE=https://ollama.com``
      so litellm routes against the hosted endpoint instead of the
      local default ``http://localhost:11434``).
    - Anything else → falls back to ``HERMES_API_KEY``, with a
      provider-key sweep in between for slash-less model strings.

    Override the model with ``HERMES_MODEL`` (default
    ``gemini/gemini-2.5-flash``). No CLI flags beyond model + key.
    """

    # Provider→env-var preference order, used by ``_discover_api_key``.
    # The first var matching the model's provider wins; if none match, fall
    # back to ``HERMES_API_KEY`` (caller-supplied generic).
    _PROVIDER_KEY_ORDER: ClassVar[dict[str, tuple[str, ...]]] = {
        'gemini': ('GOOGLE_API_KEY', 'GEMINI_API_KEY'),
        'anthropic': ('ANTHROPIC_API_KEY',),
        'openai': ('OPENAI_API_KEY',),
        'openrouter': ('OPENROUTER_API_KEY',),
        'ollama': ('OLLAMA_API_KEY',),
        'ollama_chat': ('OLLAMA_API_KEY',),
    }

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        max_iterations: int = 12,
    ) -> None:
        # Default aligned with the model.default Hermes config the eval
        # writes — otherwise AIAgent's explicit ``model=`` arg overrides
        # the YAML and we end up shipping the wrong model name to the
        # configured provider endpoint (e.g. gemini/gemini-2.5-flash hits
        # ollama.com/v1/ → 404 model not found).
        self.model = model or os.environ.get('HERMES_MODEL') or 'glm-5.1:cloud'
        self.api_key = api_key or self._discover_api_key(self.model)
        self.max_iterations = max_iterations
        # Created lazily on first ``answer()`` call so the symlink lifetime
        # is bounded by the backend instance, not the import.
        self._hermes_home: Path | None = None

    @classmethod
    def _discover_api_key(cls, model: str) -> str | None:
        """Resolve the API key for ``model`` by routing on its provider prefix.

        Picking the first non-empty env var without checking the model
        produces confusing auth failures (e.g. handing an Anthropic key
        to a Gemini call). The provider prefix is taken from ``model``.

        If the model has no ``provider/`` prefix (e.g. ``gpt-4o``), or
        the prefix is unknown, we fall back to probing every provider
        key in deterministic order — a user with ``OPENAI_API_KEY`` set
        and ``HERMES_MODEL=gpt-4o`` should not see "no key" errors.
        ``HERMES_API_KEY`` is the final fallback.
        """
        provider = model.split('/', 1)[0].lower() if '/' in model else ''
        primary = cls._PROVIDER_KEY_ORDER.get(provider, ())
        if primary:
            for var in primary:
                v = os.environ.get(var)
                if v:
                    return v
            # Provider was recognized but its key wasn't set — fall back
            # to HERMES_API_KEY only. Don't grab a sibling provider's key.
            return os.environ.get('HERMES_API_KEY')
        # No prefix or unrecognized prefix — sweep every known provider.
        seen: set[str] = set()
        for vars_tuple in cls._PROVIDER_KEY_ORDER.values():
            for var in vars_tuple:
                if var in seen:
                    continue
                seen.add(var)
                v = os.environ.get(var)
                if v:
                    return v
        return os.environ.get('HERMES_API_KEY')

    def _ensure_hermes_home(self) -> Path:
        """Create HERMES_HOME with the memex plugin symlinked in. Idempotent."""
        if self._hermes_home is not None and self._hermes_home.exists():
            return self._hermes_home

        try:
            from memex_hermes_plugin import PLUGIN_DIR
        except ImportError as e:
            raise RuntimeError(
                'memex-hermes-plugin not importable; the eval-suite hermes backend '
                'needs it. Run `uv sync --extra hermes --group hermes-integration`.'
            ) from e

        hermes_home = Path(tempfile.mkdtemp(prefix='memex-eval-hermes-'))
        plugins_dir = hermes_home / 'plugins'
        plugins_dir.mkdir(parents=True)
        (plugins_dir / 'memex').symlink_to(PLUGIN_DIR.resolve(), target_is_directory=True)
        # Model + memory + plugins config. Mirrors the user-facing
        # Hermes config shape so memex memory features and model
        # routing match production. Without an explicit ``model:``
        # block, Hermes falls back to its built-in OpenRouter
        # default — which 401s if OPENROUTER_API_KEY isn't set even
        # when the user intended a different provider.
        model_provider = os.environ.get('HERMES_MODEL_PROVIDER', 'ollama-cloud')
        model_default = os.environ.get('HERMES_MODEL', 'glm-5.1:cloud')
        model_ctx_len = os.environ.get('HERMES_MODEL_CONTEXT_LENGTH', '128000')
        (hermes_home / 'config.yaml').write_text(
            'model:\n'
            f'  provider: "{model_provider}"\n'
            f'  default: "{model_default}"\n'
            f'  context_length: {model_ctx_len}\n'
            'plugins:\n'
            '  enabled:\n'
            '    - memex\n'
            '  disabled: []\n'
            'memory:\n'
            '  memory_enabled: true\n'
            '  user_profile_enabled: true\n'
            '  memory_char_limit: 2200\n'
            '  user_char_limit: 1375\n'
            '  provider: memex\n'
            'auxiliary:\n'
            '  compression:\n'
            # Hermes auto-detects context length from each provider's
            # model registry. Some Ollama Cloud models (e.g. ``gemma4:31b-
            # cloud``) advertise an 8K detected length even though their
            # actual context is 262K; Hermes refuses to load the agent if
            # the auxiliary compression model is below its 64K minimum.
            # Pin the override to the main model's declared
            # ``context_length`` since we reuse the main model as the
            # compression model by default — the same provider with the
            # same actual capability.
            f'    context_length: {model_ctx_len}\n'
        )
        os.environ['HERMES_HOME'] = str(hermes_home)
        self._hermes_home = hermes_home
        logger.info('Hermes plugin set up at %s', hermes_home)
        return hermes_home

    async def answer(
        self,
        scenario: 'Scenario',
        *,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        server_url: str,
        judge: 'Judge | None' = None,
    ) -> AgentAnswer:
        out = AgentAnswer(backend_name=self.name)
        started = time.monotonic()

        if not self.api_key:
            out.error = (
                'No API key for the Hermes agent LLM. Set HERMES_API_KEY '
                '(or a provider-specific key matching --hermes-model, e.g. '
                'GOOGLE_API_KEY for gemini/*, ANTHROPIC_API_KEY for anthropic/*).'
            )
            out.duration_ms = (time.monotonic() - started) * 1000
            return out

        # ``run_agent`` reads HERMES_HOME at module-import time for .env
        # discovery. Make sure HERMES_HOME is set BEFORE the import so any
        # plugin .env (now or in the future) is loaded from the right dir.
        try:
            self._ensure_hermes_home()
        except RuntimeError as e:
            out.error = str(e)
            out.duration_ms = (time.monotonic() - started) * 1000
            return out

        try:
            from run_agent import AIAgent  # type: ignore[import-not-found]
        except ImportError as e:
            out.error = (
                'hermes-agent not importable. Install with: '
                '`uv sync --extra hermes --group hermes-integration`. '
                'Original error: ' + str(e)
            )
            out.duration_ms = (time.monotonic() - started) * 1000
            return out

        # Bind the plugin to the eval vault for this scenario. Strip the
        # ``/api/v1/`` suffix — the plugin expects the bare server origin.
        # Save the prior env so post-agent ``parse_memex_config()`` calls
        # (e.g. the runner's eval_import_state cleanup) don't trip over
        # ``MEMEX_VAULT=<uuid>`` being parsed as the ``vault`` config
        # block. The hermes plugin needs these only for the duration of
        # the agent loop.
        bare_url = re.sub(r'/api/v1/?$', '', server_url.rstrip('/'))
        _saved_env: dict[str, str | None] = {
            'MEMEX_SERVER_URL': os.environ.get('MEMEX_SERVER_URL'),
            'MEMEX_VAULT': os.environ.get('MEMEX_VAULT'),
        }
        os.environ['MEMEX_SERVER_URL'] = bare_url
        os.environ['MEMEX_VAULT'] = str(vault_id)

        # Capture tool calls in order. AIAgent invokes
        # ``tool_start_callback(tool_id, name, args)`` from the agent's
        # main thread (not the per-tool worker pool).
        tool_calls: list[dict[str, Any]] = []

        # Per-scenario session log: append an event per agent callback so
        # the runner can upload the full transcript to MLflow as an artifact.
        # ndjson — one JSON event per line, ordered, mirrors claude-code's
        # stream-json shape so downstream tooling can treat them uniformly.
        #
        # Each callback uses (*args, **kwargs) because ``run_agent.AIAgent``
        # has minor signature variations across versions (e.g. tool_complete
        # passes ``(id, name, args, result)``, step passes
        # ``(api_call_count, prev_tools)``, interim_assistant passes
        # ``(text, already_streamed=...)``). Pinning to a specific arity
        # would silently no-op on a version skew — run_agent wraps every
        # callback in try/except, so mismatches don't crash the agent,
        # they just produce empty logs.
        session_events: list[dict[str, Any]] = []
        # Cap to bound memory + MLflow artifact size on long traces.
        # Average event ~200 bytes → ~10MB cap per scenario.
        _MAX_EVENTS = 50_000

        def _log_event(event_type: str, **payload: Any) -> None:
            if len(session_events) >= _MAX_EVENTS:
                return
            entry: dict[str, Any] = {'type': event_type}
            entry.update(payload)
            session_events.append(entry)

        def _safe_repr(value: Any, max_len: int = 4_000) -> Any:
            """Return a JSON-friendly representation; truncate long strings."""
            if isinstance(value, (str, int, float, bool, type(None))):
                if isinstance(value, str) and len(value) > max_len:
                    return value[:max_len] + f'...<truncated {len(value) - max_len}b>'
                return value
            if isinstance(value, (list, dict)):
                # Pydantic / Mapping / list-of-primitives serialize fine via
                # json.dumps(default=str); deep structures get truncated by
                # the per-line dump cap below if they exceed sensible size.
                return value
            return str(value)[:max_len]

        def _on_tool_start(*args: Any, **kwargs: Any) -> None:
            # Signature: (tool_id, name, args). Defend against version skew.
            tool_id = args[0] if len(args) > 0 else kwargs.get('tool_id', '')
            name = args[1] if len(args) > 1 else kwargs.get('name', '')
            tool_input = args[2] if len(args) > 2 else kwargs.get('args')
            tool_calls.append({'tool': name, 'input': tool_input})
            _log_event(
                'tool_start', tool_id=str(tool_id), name=str(name), input=_safe_repr(tool_input)
            )

        def _on_tool_complete(*args: Any, **kwargs: Any) -> None:
            # Signature: (tool_id, name, args, result). 4 positional in
            # current run_agent; some older versions pass (tool_id, name, result).
            tool_id = args[0] if len(args) > 0 else ''
            name = args[1] if len(args) > 1 else ''
            result = args[-1] if len(args) >= 3 else kwargs.get('result')
            _log_event(
                'tool_complete', tool_id=str(tool_id), name=str(name), result=_safe_repr(result)
            )

        def _on_step(*args: Any, **kwargs: Any) -> None:
            # Signature: (api_call_count, prev_tools).
            api_call_count = args[0] if len(args) > 0 else kwargs.get('api_call_count')
            prev_tools = args[1] if len(args) > 1 else kwargs.get('prev_tools')
            _log_event(
                'step', api_call_count=_safe_repr(api_call_count), prev_tools=_safe_repr(prev_tools)
            )

        def _on_thinking(*args: Any, **kwargs: Any) -> None:
            # Signature: (text,).
            text = args[0] if len(args) > 0 else kwargs.get('text', '')
            _log_event('thinking', text=_safe_repr(text))

        def _on_interim_assistant(*args: Any, **kwargs: Any) -> None:
            # Signature: (visible, already_streamed=bool).
            text = args[0] if len(args) > 0 else kwargs.get('visible', kwargs.get('text', ''))
            already_streamed = kwargs.get('already_streamed', False)
            _log_event(
                'interim_assistant',
                text=_safe_repr(text),
                already_streamed=bool(already_streamed),
            )

        def _on_reasoning(*args: Any, **kwargs: Any) -> None:
            # Signature: (text,).
            text = args[0] if len(args) > 0 else kwargs.get('text', '')
            _log_event('reasoning', text=_safe_repr(text))

        agent: Any = None
        try:
            agent = AIAgent(
                model=self.model,
                api_key=self.api_key,
                max_iterations=self.max_iterations,
                save_trajectories=False,
                quiet_mode=True,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                step_callback=_on_step,
                thinking_callback=_on_thinking,
                interim_assistant_callback=_on_interim_assistant,
                reasoning_callback=_on_reasoning,
            )
            # AIAgent.run_conversation is sync; offload to a worker thread so
            # the asyncio event loop in the runner stays responsive.
            import asyncio

            final_response = await asyncio.to_thread(agent.run_conversation, scenario.query)
            self._populate_answer_from_response(out, final_response, tool_calls)
            _log_event(
                'final_response',
                response=_safe_repr(
                    final_response
                    if isinstance(final_response, (dict, str))
                    else str(final_response)
                ),
            )
        except Exception as e:
            out.error = f'hermes agent failed: {type(e).__name__}: {e}'
            logger.exception('HermesBackend.answer raised')
            _log_event('error', message=out.error)
        finally:
            # Serialize the collected events to ndjson regardless of pass/fail
            # so a failed scenario still produces an inspectable transcript.
            # Wrap per-event so a single unserializable payload doesn't blank
            # the entire transcript.
            _lines: list[str] = []
            for ev in session_events:
                try:
                    _lines.append(json.dumps(ev, default=str))
                except (TypeError, ValueError) as ser_err:
                    _lines.append(
                        json.dumps(
                            {'type': ev.get('type', 'unknown'), 'serialize_error': str(ser_err)}
                        )
                    )
            out.session_log_text = '\n'.join(_lines)
            if agent is not None:
                out.tokens_in = int(getattr(agent, 'session_input_tokens', 0) or 0)
                out.tokens_out = int(getattr(agent, 'session_output_tokens', 0) or 0)
                out.cost_usd = float(getattr(agent, 'session_estimated_cost_usd', 0.0) or 0.0)
                # Drop the agent reference promptly so its internal threads /
                # provider clients can be GC'd before the next scenario.
                shutdown = getattr(agent, 'shutdown', None)
                if callable(shutdown):
                    with contextlib.suppress(Exception):
                        shutdown()
            # Restore the prior MEMEX_SERVER_URL / MEMEX_VAULT values so
            # later ``parse_memex_config()`` calls aren't tripped by the
            # plugin-binding env we just wrote.
            for k, v in _saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        # Fallback UUID extraction from the answer text is intentionally
        # gated on the agent having surfaced search-shaped tool calls — see
        # _SEARCH_TOOL_PREFIXES — so non-unit UUIDs (vault, note, entity)
        # mentioned in unrelated answers don't poison GoldUnitIds recall.
        if (
            not out.retrieved_unit_ids
            and out.answer_text
            and any(tc.get('tool', '').startswith(_SEARCH_TOOL_PREFIXES) for tc in out.tool_calls)
        ):
            _extract_unit_ids_from_tool_result(
                {'content': [{'type': 'text', 'text': out.answer_text}]}, out
            )

        out.duration_ms = (time.monotonic() - started) * 1000
        return out

    @staticmethod
    def _populate_answer_from_response(
        out: AgentAnswer, response: Any, tool_calls: list[dict[str, Any]]
    ) -> None:
        """Translate ``run_conversation``'s return value into the AgentAnswer.

        ``run_conversation`` returns a dict on the success path with
        ``final_response`` (str). Failure / partial / interrupted paths
        return a dict that may carry ``failed=True``, ``partial=True``,
        ``interrupted=True``, or an ``error`` string with no usable text.
        We surface those as ``out.error`` so the runner records the
        scenario as ``status='error'`` (excluded from suite.pass_rate)
        instead of a degenerate ``fail`` against an empty answer.
        """
        out.tool_calls = tool_calls
        if isinstance(response, dict):
            out.raw_trace = response
            text = response.get('final_response') or response.get('response') or ''
            out.answer_text = text or None
            err = response.get('error')
            failed = bool(
                response.get('failed') or response.get('partial') or response.get('interrupted')
            )
            if not text and (err or failed):
                flags = {
                    k: response.get(k)
                    for k in ('failed', 'partial', 'interrupted', 'completed')
                    if k in response
                }
                out.error = f'hermes run did not complete: error={err!r} flags={flags}'
        else:
            out.answer_text = str(response or '') or None

    def close(self) -> None:
        """Tear down the temp HERMES_HOME. Called by the runner on suite end."""
        if self._hermes_home and self._hermes_home.exists():
            with contextlib.suppress(Exception):
                shutil.rmtree(self._hermes_home, ignore_errors=True)
        self._hermes_home = None

    def __del__(self) -> None:
        # Backstop for the unusual path where the runner doesn't call
        # close() (test code, ad-hoc usage). Cannot be relied on for
        # production correctness — finalizer ordering is non-deterministic.
        with contextlib.suppress(Exception):
            self.close()


__all__ = [
    'AgentAnswer',
    'AnswerBackend',
    'register_backend',
    'replace_backend',
    'unregister_backend',
    'get_backend',
    'list_backends',
    'DirectApiBackend',
    'ClaudeCodeBackend',
    'HermesBackend',
]
