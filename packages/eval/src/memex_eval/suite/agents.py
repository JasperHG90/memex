"""Pluggable answer-generation backends for evaluation suites.

Every backend produces a uniform ``AgentAnswer`` so ``ExpectedOutcome``
subclasses can score against a single shape. Built-in backends:

- ``api`` — direct ``RemoteMemexAPI`` calls (the default; tests memex's
  own surfaces under controlled conditions).
- ``claude-code`` — subprocess to the ``claude`` CLI with a temp
  workspace + ``.mcp.json`` pointing at the eval vault. Captures answer
  text + tool-call trace + retrieved unit IDs from the trace.
- ``hermes`` — subprocess to the ``hermes`` CLI with the
  ``memex-hermes-plugin`` providing memory. Same trace-parsing pattern.

Custom backends register via ``@register_backend('myname')`` on a
subclass of ``AnswerBackend``. Suites pick a backend per scenario via
``Scenario.answer_mode`` or per suite via
``SuiteMetadata.default_answer_mode``.
"""

from __future__ import annotations

import abc
import json
import logging
import os
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
    lint_findings: list[Any] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_unit_ids: list[str] = Field(default_factory=list)
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


def register_backend(name: str):
    """Decorator: register a backend class under ``name``.

    ::

        @register_backend('my-agent')
        class MyAgentBackend(AnswerBackend):
            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                ...
    """

    def deco(cls: type[AnswerBackend]) -> type[AnswerBackend]:
        if name in _BACKEND_REGISTRY:
            logger.warning('Overriding existing backend registration: %r', name)
        cls.name = name
        _BACKEND_REGISTRY[name] = cls
        return cls

    return deco


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
            EntityResolves,
            LintFindingPresent,
            LLMLintFlagsUnit,
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
            elif isinstance(outcome, (LintFindingPresent, LLMLintFlagsUnit)):
                payload = await api.lint_findings(vault_id=str(vault_id))
                # /lint/findings returns {'findings': [...], ...} — extract the list.
                raw = payload.get('findings') if isinstance(payload, dict) else payload
                findings_list: list[Any] = []
                for item in raw or []:
                    if isinstance(item, dict):
                        # Wrap so getattr(f, 'rule_name', ...) works in score().
                        findings_list.append(_DictAttrShim(item))
                    else:
                        findings_list.append(item)
                out.lint_findings = findings_list
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
                tool_name = block.get('name', '')
                tool_input = block.get('input', {})
                out.tool_calls.append({'tool': tool_name, 'input': tool_input})
                # Memex MCP search tools return unit lists in the result.
            elif block.get('type') == 'tool_result':
                _extract_unit_ids_from_tool_result(block, out)


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
    import re

    uuid_pattern = re.compile(
        r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
        re.IGNORECASE,
    )
    for match in uuid_pattern.findall(text_blob):
        if match not in out.retrieved_unit_ids:
            out.retrieved_unit_ids.append(match)


# ---------------------------------------------------------------------------
# Built-in: Hermes subprocess
# ---------------------------------------------------------------------------


@register_backend('hermes')
class HermesBackend(AnswerBackend):
    """Spawn the ``hermes`` CLI with the memex-hermes-plugin providing memory.

    Assumes ``hermes`` is on PATH and the plugin is installed in the
    Hermes home (run ``memex hermes install`` once before evaluating).
    Captures the agent's final answer + any tool/memory traces.

    The exact CLI invocation depends on your Hermes build — override
    via ``HERMES_BIN`` env or by subclassing this backend. The default
    invocation is ``hermes -p <prompt> --output-format json``; if your
    Hermes uses a different flag set, register a custom backend.
    """

    def __init__(
        self,
        hermes_bin: str | None = None,
        extra_args: list[str] | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.hermes_bin: str = hermes_bin or os.environ.get('HERMES_BIN') or 'hermes'
        self.extra_args = list(extra_args or [])
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
        if not shutil.which(self.hermes_bin):
            return AgentAnswer(
                backend_name=self.name,
                error=(
                    f'{self.hermes_bin!r} not on PATH; install Hermes + run '
                    f'`memex hermes install`, or set HERMES_BIN'
                ),
            )

        out = AgentAnswer(backend_name=self.name)
        started = time.monotonic()
        env = os.environ.copy()
        env.setdefault('MEMEX_SERVER_URL', server_url)

        cmd = [
            self.hermes_bin,
            '-p',
            scenario.query,
            '--output-format',
            'json',
            *self.extra_args,
        ]
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            out.error = f'hermes subprocess timed out after {self.timeout_s}s'
            out.duration_ms = (time.monotonic() - started) * 1000
            return out

        if proc.returncode != 0:
            out.error = f'hermes exited {proc.returncode}: {(proc.stderr or "").strip()[:500]}'

        # Best-effort trace parsing: try JSON first, fall back to plain text.
        try:
            payload = json.loads(proc.stdout or '{}')
            if isinstance(payload, dict):
                out.answer_text = payload.get('answer') or payload.get('result')
                out.tool_calls = list(payload.get('tool_calls') or [])
                out.retrieved_unit_ids = list(payload.get('retrieved_unit_ids') or [])
                out.tokens_in = int(payload.get('tokens_in') or 0)
                out.tokens_out = int(payload.get('tokens_out') or 0)
                out.cost_usd = float(payload.get('cost_usd') or 0.0)
                out.raw_trace = payload
        except json.JSONDecodeError:
            out.answer_text = proc.stdout

        # If the Hermes invocation didn't surface unit IDs in structured form,
        # pull any UUIDs from the answer text as a fallback.
        if not out.retrieved_unit_ids and out.answer_text:
            _extract_unit_ids_from_tool_result(
                {'content': [{'type': 'text', 'text': out.answer_text}]}, out
            )

        out.duration_ms = (time.monotonic() - started) * 1000
        return out


__all__ = [
    'AgentAnswer',
    'AnswerBackend',
    'register_backend',
    'get_backend',
    'list_backends',
    'DirectApiBackend',
    'ClaudeCodeBackend',
    'HermesBackend',
]
