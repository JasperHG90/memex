"""LongMemEval Phase 2: Answer questions using a Claude Code subagent.

One subprocess per question. Each question gets a fresh temporary
workspace prepared with:

- ``.mcp.json`` — stdio MCP server pointing at the running Memex REST
  server so the subagent can use the memex MCP tools.
- ``.memex.yaml`` — pins the per-run vault so all MCP calls are scoped
  to the benchmark's data.
- ``CLAUDE.md`` — retrieval-playbook system prompt (routing,
  citations, prohibitions). Mirrors the LoCoMo pattern this module
  replaced.
- ``.claude/settings.local.json`` — permissions allow-list for the
  memex MCP tools so ``--dangerously-skip-permissions`` stays off for
  anything else.

On top of that, the **memex Claude Code plugin** (``packages/
claude-code-plugin/``) is wired in via the ``--plugin-dir`` flag so
the subagent has the plugin's skills (``/remember``, ``/recall``,
``/retro``) and hooks in addition to the raw MCP tools.

Falls back to nothing else — ``AnswerMethod.GEMINI_CLI`` is reserved
for future work and currently raises ``NotImplementedError``.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from memex_eval.external.longmemeval_common import (
    LongMemEvalHypothesis,
    _load_variant,
    append_jsonl,
    read_completed_ids,
    vault_name_for,
)

logger = logging.getLogger('memex_eval.longmemeval_answer')
console = Console()


# Pinned prompt-template version — bumped whenever the system prompt
# materially changes so hypothesis fingerprints stay traceable.
ANSWER_PROMPT_TEMPLATE_VERSION = '2026-04-15.v2'


# Repo root: packages/eval/src/memex_eval/external/longmemeval_answer.py
# -> parents[5] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[5]
_PLUGIN_DIR = _REPO_ROOT / 'packages' / 'claude-code-plugin'


class AnswerMethod(str, enum.Enum):
    """Curated agent CLIs that can answer LongMemEval questions."""

    CLAUDE_CODE = 'claude-code'
    GEMINI_CLI = 'gemini-cli'


_MCP_JSON_TEMPLATE = """\
{{
  "mcpServers": {{
    "memex": {{
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "{workspace}", "run", "memex", "mcp", "run"],
      "env": {{
        "MEMEX_SERVER_URL": "{server_url_base}",
        "MEMEX_SERVER__DEFAULT_ACTIVE_VAULT": "{vault}",
        "MEMEX_SERVER__DEFAULT_READER_VAULT": "{vault}"
      }}
    }}
  }}
}}
"""


_MEMEX_YAML_TEMPLATE = """\
server_url: {server_url_base}
vault:
  active: "{vault}"
server:
  default_active_vault: "{vault}"
  default_reader_vault: "{vault}"
"""


_CLAUDE_MD = """\
# LongMemEval — Memex Retrieval

You are answering a question about a long-running multi-session
conversation. A Memex vault has been pre-populated with one note per
session (timestamped via ``publish_date`` frontmatter) and every fact
extracted into a ``MemoryUnit``. Use the Memex MCP tools to find the
answer. Use the ``/recall`` plugin skill for broad queries and the
raw tools for precise lookups.

## First action

Fetch all Memex MCP tool schemas in a single call:

```
ToolSearch(query="select:mcp__memex__memex_memory_search,mcp__memex__memex_note_search,\
mcp__memex__memex_list_entities,mcp__memex__memex_get_entity_mentions,\
mcp__memex__memex_get_entity_cooccurrences,mcp__memex__memex_get_notes_metadata,\
mcp__memex__memex_get_page_indices,mcp__memex__memex_get_nodes,\
mcp__memex__memex_read_note", max_results=9)
```

## Retrieval routing

- Memory / fact / observation lookup: ``memex_memory_search`` (broad)
  AND/OR ``memex_note_search`` (targeted). Run in parallel.
- Relationships / "how X connects to Y": ``memex_list_entities`` →
  ``memex_get_entity_cooccurrences`` → ``memex_get_entity_mentions``.
- Temporal-reasoning questions: use ``memex_memory_search`` with
  date filters when available; the per-session ``publish_date`` is
  carried into ``MemoryUnit.mentioned_at`` / ``occurred_start``.
- Source-note content: ``memex_get_page_indices`` then
  ``memex_get_nodes``; ``memex_read_note`` only for notes < 500
  tokens.

## Abstention

If the memory contains no evidence to answer the question, reply
exactly:

    I do not know based on the available memory.

Do NOT hedge; do NOT invent. Abstention is scored as correct on
``*_abs`` questions.

## Answer format

Answer concisely in plain text. Do NOT wrap in code fences. The final
message you send is the hypothesis — everything else is scaffolding.
"""


def _normalise_server_url(server_url: str) -> str:
    """Strip trailing ``/api/v1`` / slash so MCP + config take the base URL."""
    base = server_url.rstrip('/')
    if base.endswith('/api/v1'):
        base = base[: -len('/api/v1')]
    return base


def _setup_subagent_workspace(server_url: str, vault_name: str) -> str:
    """Create a temp workspace with MCP config, memex YAML, and CLAUDE.md.

    The memex Claude Code plugin is wired in separately via the
    ``--plugin-dir`` flag on the ``claude`` invocation; nothing is
    copied into the workspace itself. This keeps the plugin
    installation idempotent across runs and matches the plugin
    README's "local development" install path.
    """
    tmpdir = tempfile.mkdtemp(prefix='longmemeval-claude-')
    base_url = _normalise_server_url(server_url)

    (Path(tmpdir) / '.mcp.json').write_text(
        _MCP_JSON_TEMPLATE.format(
            workspace=str(_REPO_ROOT), server_url_base=base_url, vault=vault_name
        )
    )
    (Path(tmpdir) / '.memex.yaml').write_text(
        _MEMEX_YAML_TEMPLATE.format(server_url_base=base_url, vault=vault_name)
    )
    (Path(tmpdir) / 'CLAUDE.md').write_text(_CLAUDE_MD)

    claude_dir = Path(tmpdir) / '.claude'
    claude_dir.mkdir()
    (claude_dir / 'settings.local.json').write_text(
        json.dumps(
            {
                'permissions': {
                    'allow': [
                        'mcp__memex__memex_memory_search',
                        'mcp__memex__memex_note_search',
                        'mcp__memex__memex_list_entities',
                        'mcp__memex__memex_get_entities',
                        'mcp__memex__memex_get_entity_mentions',
                        'mcp__memex__memex_get_entity_cooccurrences',
                        'mcp__memex__memex_get_notes_metadata',
                        'mcp__memex__memex_get_page_indices',
                        'mcp__memex__memex_get_nodes',
                        'mcp__memex__memex_read_note',
                        'mcp__memex__memex_list_vaults',
                    ]
                }
            },
            indent=2,
        )
    )

    # Silence ``claude`` warnings about running in a non-git workdir.
    subprocess.run(['git', 'init'], cwd=tmpdir, capture_output=True, check=False)

    return tmpdir


def _resolve_plugin_dir(override: str | None = None) -> Path:
    """Locate the memex Claude Code plugin directory.

    Resolution order:
      1. Explicit ``override`` argument.
      2. ``MEMEX_CLAUDE_PLUGIN_DIR`` env var.
      3. Repo-local ``packages/claude-code-plugin/`` (the monorepo default).
    """
    if override is not None:
        return Path(override)
    env_override = os.environ.get('MEMEX_CLAUDE_PLUGIN_DIR')
    if env_override:
        return Path(env_override)
    return _PLUGIN_DIR


def _verify_plugin_installed(plugin_dir: Path) -> None:
    """Raise if the plugin directory is missing required files.

    Catches configuration mistakes (e.g. wrong ``--plugin-dir`` path)
    before we spawn 500 subagents against a broken install.
    """
    if not plugin_dir.exists():
        raise FileNotFoundError(
            f'memex Claude Code plugin not found at {plugin_dir}. '
            f'Set MEMEX_CLAUDE_PLUGIN_DIR or pass --plugin-dir.'
        )
    manifest = plugin_dir / '.claude-plugin' / 'plugin.json'
    if not manifest.exists():
        raise FileNotFoundError(
            f'memex Claude Code plugin at {plugin_dir} is missing '
            f'{manifest.relative_to(plugin_dir)}. Is this the right directory?'
        )
    skills_dir = plugin_dir / 'skills'
    if not skills_dir.is_dir():
        logger.warning(
            "memex Claude Code plugin at %s has no 'skills/' directory — "
            'subagents will have MCP tools but no plugin skills.',
            plugin_dir,
        )
    else:
        skill_names = sorted(p.name for p in skills_dir.iterdir() if p.is_dir())
        logger.info(
            'memex Claude Code plugin at %s exposes skills: %s',
            plugin_dir,
            ', '.join(skill_names) or '(none)',
        )


# ---------------------------------------------------------------------------
# Session trace capture
# ---------------------------------------------------------------------------


def _collect_session_trace(
    workdir: str, output_dir: str, question_id: str
) -> dict[str, str | None]:
    """Copy the Claude Code session trace file for a question.

    Finds the most recently modified ``.jsonl`` in the Claude Code project
    directory for the workdir, then copies it into
    ``<output_dir>/traces/<question_id>.jsonl``.

    Returns dict with ``session_id`` and ``trace_file`` (both may be None on failure).
    """
    result: dict[str, str | None] = {'session_id': None, 'trace_file': None}

    try:
        # Claude Code stores project data under ~/.claude/projects/<slug>/
        # where slug = absolute path with / replaced by - (leading - kept).
        # Claude Code may also replace underscores with dashes in the slug.
        projects_root = Path.home() / '.claude' / 'projects'
        slug = workdir.replace('/', '-')
        project_dir = projects_root / slug

        if not project_dir.exists():
            # Fallback: try with underscores also replaced by dashes
            slug_alt = slug.replace('_', '-')
            project_dir_alt = projects_root / slug_alt
            if project_dir_alt.exists():
                project_dir = project_dir_alt
            else:
                logger.warning('Project dir not found at %s (or %s)', project_dir, project_dir_alt)
                return result

        # Find the most recently modified .jsonl (the trace for the last -p call)
        traces = sorted(
            project_dir.glob('*.jsonl'),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not traces:
            logger.warning('No session traces in %s', project_dir)
            return result

        trace_src = traces[0]
        session_id = trace_src.stem
        result['session_id'] = session_id

        # Copy to output traces dir
        traces_dir = Path(output_dir) / 'traces'
        traces_dir.mkdir(parents=True, exist_ok=True)
        trace_dst = traces_dir / f'{question_id}.jsonl'
        shutil.copy2(str(trace_src), str(trace_dst))
        result['trace_file'] = str(trace_dst)
        logger.info('Captured session trace -> %s', trace_dst)

    except Exception:
        logger.warning('Failed to collect session trace', exc_info=True)

    return result


def _extract_tool_calls_from_trace(trace_path: str) -> list[dict[str, Any]]:
    """Extract tool_use blocks from a Claude Code session trace file."""
    tool_calls: list[dict[str, Any]] = []
    try:
        for line in Path(trace_path).read_text().strip().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get('type') != 'assistant':
                continue
            for block in obj.get('message', {}).get('content', []):
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    tool_calls.append(
                        {
                            'name': block.get('name', ''),
                            'input': block.get('input', {}),
                        }
                    )
    except Exception:
        logger.warning('Failed to extract tool calls from trace', exc_info=True)
    return tool_calls


def _run_claude_subagent(
    question_text: str,
    workdir: str,
    *,
    plugin_dir: Path,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Invoke ``claude -p ... --output-format json`` for one question.

    Returns a dict with ``answer``, ``tool_calls``, ``tokens``,
    ``num_turns``, ``cost_usd``, ``duration_s``, ``model``, ``error``.
    """
    prompt = f'Answer the following LongMemEval question: {question_text}'
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE'}

    cmd = [
        'claude',
        '--model',
        'claude-sonnet-4-6',
        '--plugin-dir',
        str(plugin_dir),
        '-p',
        prompt,
        '--output-format',
        'json',
        '--dangerously-skip-permissions',
    ]

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            'answer': '',
            'tool_calls': [],
            'tokens': {'input': 0, 'output': 0},
            'num_turns': 0,
            'cost_usd': 0.0,
            'duration_s': round(time.time() - t0, 2),
            'model': None,
            'error': 'timeout',
        }

    duration = time.time() - t0

    if proc.returncode != 0:
        return {
            'answer': '',
            'tool_calls': [],
            'tokens': {'input': 0, 'output': 0},
            'num_turns': 0,
            'cost_usd': 0.0,
            'duration_s': round(duration, 2),
            'model': None,
            'error': f'claude exit code {proc.returncode}: {proc.stderr[:500]}',
        }

    return _parse_claude_json_output(proc.stdout, duration)


def _parse_claude_json_output(stdout: str, duration: float) -> dict[str, Any]:
    """Parse the JSONL / JSON emitted by ``claude --output-format json``.

    Conversation messages come first as JSONL; the final line is the
    ``result`` summary. Some versions emit a single JSON object. Both
    shapes are supported.
    """
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    tool_calls: list[dict[str, Any]] = []
    result_data: dict[str, Any] = {}

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get('type') == 'assistant':
            for block in obj.get('message', {}).get('content', []) or []:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    tool_calls.append(
                        {
                            'name': block.get('name', ''),
                            'input': block.get('input', {}),
                        }
                    )
        if obj.get('type') == 'result' or 'result' in obj:
            result_data = obj

    answer = result_data.get('result', '')
    if not isinstance(answer, str):
        answer = str(answer)

    usage = result_data.get('usage', {}) or {}
    input_tokens = (
        usage.get('input_tokens', 0)
        + usage.get('cache_creation_input_tokens', 0)
        + usage.get('cache_read_input_tokens', 0)
    )
    output_tokens = usage.get('output_tokens', 0)
    num_turns = result_data.get('num_turns', 0)
    cost_usd = result_data.get('total_cost_usd', 0.0) or 0.0
    model = result_data.get('model') or None

    return {
        'answer': answer,
        'tool_calls': tool_calls,
        'tokens': {'input': input_tokens, 'output': output_tokens},
        'num_turns': num_turns,
        'cost_usd': round(float(cost_usd), 6),
        'duration_s': round(duration, 2),
        'model': model,
        'error': None,
    }


def _fingerprint(model: str | None, method: AnswerMethod) -> str:
    model_part = model or 'claude-sonnet-4-6'
    return f'{method.value}:{model_part}@{ANSWER_PROMPT_TEMPLATE_VERSION}'


def _resolve_answer_method(method: AnswerMethod | None) -> AnswerMethod:
    """Resolve the answer method. Defaults to CLAUDE_CODE when no flag is set."""
    return method if method is not None else AnswerMethod.CLAUDE_CODE


async def answer_questions(
    server_url: str,
    dataset_path: str,
    variant: str,
    run_id: str,
    output_path: str,
    *,
    method: AnswerMethod | None = None,
    answer_model: str | None = None,  # noqa: ARG001 — reserved for future (e.g. --model passthrough)
    answer_api_key: str | None = None,  # noqa: ARG001 — reserved for future
    retrieval_limit: int = 20,  # noqa: ARG001 — retrieval is subagent-driven now
    question_limit: int | None = None,
    allow_unpinned_checksum: bool = False,
    plugin_dir: str | None = None,
    subagent_timeout_s: float = 300.0,
) -> int:
    """Answer LongMemEval questions via Claude Code subagents.

    Returns the number of hypotheses produced in this run. Resumes
    safely: questions already present in ``output_path`` are skipped.
    """
    resolved_method = _resolve_answer_method(method)
    if resolved_method is not AnswerMethod.CLAUDE_CODE:
        raise NotImplementedError(f'{resolved_method.value} is not yet implemented.')

    questions = _load_variant(Path(dataset_path), variant, allow_unpinned=allow_unpinned_checksum)
    if question_limit is not None:
        questions = questions[:question_limit]
    completed = read_completed_ids(output_path)
    pending = [q for q in questions if q.question_id not in completed]

    if not pending:
        console.print('[dim]All questions already answered. Nothing to do.[/dim]')
        return 0

    console.print(
        f'[bold]{len(pending)} questions to answer[/bold] '
        f'({len(completed)} already done, {len(questions)} total)'
    )

    vault_name = vault_name_for(variant, run_id)
    resolved_plugin_dir = _resolve_plugin_dir(plugin_dir)
    _verify_plugin_installed(resolved_plugin_dir)

    workdir = _setup_subagent_workspace(server_url, vault_name)
    logger.info('Subagent workspace: %s (plugin-dir: %s)', workdir, resolved_plugin_dir)

    output_dir = str(Path(output_path).parent)
    answered = 0
    try:
        for i, question in enumerate(pending):
            result = _run_claude_subagent(
                question.question_text,
                workdir=workdir,
                plugin_dir=resolved_plugin_dir,
                timeout_s=subagent_timeout_s,
            )

            # Capture session trace and extract tool calls from it
            trace_info = _collect_session_trace(workdir, output_dir, question.question_id)

            # If stdout parsing missed tool calls, extract from the trace file
            trace_file = trace_info.get('trace_file')
            if not result['tool_calls'] and trace_file:
                result['tool_calls'] = _extract_tool_calls_from_trace(trace_file)

            from memex_eval.external.longmemeval_common import LongMemEvalToolCall

            tool_call_records = [
                LongMemEvalToolCall(
                    name=call.get('name', ''),
                    input=call.get('input', {}),
                )
                for call in result['tool_calls']
            ]
            hypothesis = LongMemEvalHypothesis(
                question_id=question.question_id,
                hypothesis=(result['answer'] or '').strip(),
                retrieved_unit_ids=[
                    tc.input.get('unit_id', '') or ''
                    for tc in tool_call_records
                    if isinstance(tc.input, dict)
                ],
                answer_model_fingerprint=_fingerprint(result.get('model'), resolved_method),
                latency_ms=round(result['duration_s'] * 1000.0, 2),
                cost_usd=result['cost_usd'],
                input_tokens=result['tokens']['input'],
                output_tokens=result['tokens']['output'],
                num_turns=result['num_turns'],
                tool_calls=tool_call_records,
                session_id=trace_info.get('session_id'),
                trace_file=trace_info.get('trace_file'),
            )
            append_jsonl(output_path, hypothesis.model_dump())
            answered += 1
            logger.info(
                '[%d/%d] %s -> %s',
                i + 1,
                len(pending),
                question.question_id,
                (result['answer'] or '').strip()[:80],
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    console.print(f'\n[bold green]Answered {answered} questions -> {output_path}[/bold green]')
    return answered
