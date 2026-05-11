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
                    out.retrieved_unit_ids = [str(getattr(n, 'id', '')) for n in out.units]
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


_CLAUDE_MD = """\
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


@register_backend('claude-code')
class ClaudeCodeBackend(AnswerBackend):
    """Spawn the ``claude`` CLI as a subagent with Memex MCP wired up.

    Builds a temporary workspace with ``.mcp.json`` + ``CLAUDE.md``,
    spawns ``claude -p <question>`` with streaming JSON output, and
    parses the trace for the final answer + tool calls + retrieved unit
    IDs.

    Requires ``claude`` on the PATH.
    """

    def __init__(self, claude_bin: str | None = None, timeout_s: float = 300.0) -> None:
        self.claude_bin: str = claude_bin or os.environ.get('CLAUDE_BIN') or 'claude'
        self.timeout_s = timeout_s

    async def answer(
        self,
        scenario: 'Scenario',
        *,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        server_url: str,
        judge: 'Judge | None' = None,
    ) -> AgentAnswer:
        if not shutil.which(self.claude_bin):
            return AgentAnswer(
                backend_name=self.name,
                error=f'{self.claude_bin!r} not on PATH; install Claude Code or set CLAUDE_BIN',
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

        with tempfile.TemporaryDirectory(prefix='memex-eval-claude-') as tmp:
            tmpdir = Path(tmp)
            (tmpdir / '.mcp.json').write_text(
                _CLAUDE_MCP_TEMPLATE.format(workspace=workspace_root, server_url=server_url)
            )
            (tmpdir / 'CLAUDE.md').write_text(
                _CLAUDE_MD.format(vault_name=vault_name or str(vault_id))
            )
            cmd = [
                self.claude_bin,
                '-p',
                scenario.query,
                '--output-format',
                'stream-json',
                '--include-partial-messages',
                '--verbose',
                '--permission-mode',
                'bypassPermissions',
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                out.error = f'claude subprocess timed out after {self.timeout_s}s'
                out.duration_ms = (time.monotonic() - started) * 1000
                return out

            if proc.returncode != 0:
                out.error = f'claude exited {proc.returncode}: {(proc.stderr or "").strip()[:500]}'

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
        out.tokens_in += int(usage.get('input_tokens', 0) or 0)
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

        def _on_tool_start(_tool_id: str, name: str, args: Any) -> None:
            tool_calls.append({'tool': name, 'input': args})

        agent: Any = None
        try:
            agent = AIAgent(
                model=self.model,
                api_key=self.api_key,
                max_iterations=self.max_iterations,
                save_trajectories=False,
                quiet_mode=True,
                tool_start_callback=_on_tool_start,
            )
            # AIAgent.run_conversation is sync; offload to a worker thread so
            # the asyncio event loop in the runner stays responsive.
            import asyncio

            final_response = await asyncio.to_thread(agent.run_conversation, scenario.query)
            self._populate_answer_from_response(out, final_response, tool_calls)
        except Exception as e:
            out.error = f'hermes agent failed: {type(e).__name__}: {e}'
            logger.exception('HermesBackend.answer raised')
        finally:
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
