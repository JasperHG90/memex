"""LoCoMo Phase 2: Answer questions using a curated CLI agent.

Reads questions from JSONL, shells out to an agent CLI (e.g. claude-code),
and writes answers to a separate JSONL file with full statistics
(token usage, duration, cost, tool calls).
Supports resume via skip-if-exists.
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

import sh
from rich.console import Console

from memex_eval.external.locomo_common import (
    VAULT_NAME,
    append_jsonl,
    read_completed_ids,
    read_jsonl,
)

logger = logging.getLogger('memex_eval.locomo_answer')
console = Console()

_WORKSPACE_ROOT = str(Path(__file__).resolve().parents[5])


class AnswerMethod(str, enum.Enum):
    """Curated agent CLIs that can answer LoCoMo questions."""

    CLAUDE_CODE = 'claude-code'
    GEMINI_CLI = 'gemini-cli'


# ---------------------------------------------------------------------------
# Claude Code workspace setup
# ---------------------------------------------------------------------------

_MCP_JSON_TEMPLATE = """\
{{
  "mcpServers": {{
    "memex": {{
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "{workspace}", "run", "memex", "mcp", "run"],
      "env": {{
        "MEMEX_SERVER_URL": "http://localhost:8001"
      }}
    }}
  }}
}}
"""

_MEMEX_YAML_TEMPLATE = """\
server_url: {server_url}
vault:
  active: "{vault}"
server:
  default_active_vault: "{vault}"
  default_reader_vault: "{vault}"
"""

_CLAUDE_MD = """\
# Memex Memory Retrieval

You have access to Memex, a long-term memory system. Use it to answer questions \
about conversations between people.

## CRITICAL — First action

Before doing anything else, fetch ALL tool schemas in a single call:

```
ToolSearch(query="select:mcp__memex__memex_memory_search,mcp__memex__memex_note_search,\
mcp__memex__memex_list_entities,mcp__memex__memex_get_entity_mentions,\
mcp__memex__memex_get_entity_cooccurrences,mcp__memex__memex_get_notes_metadata,\
mcp__memex__memex_get_page_indices,mcp__memex__memex_get_nodes,\
mcp__memex__memex_read_note", max_results=9)
```

Do NOT call ToolSearch more than once. After this single call you have all the tools you need.

### Retrieval

Route by query type:

IF query asks about relationships, connections, "how X relates to Y", or landscape:
- `memex_list_entities(query="X")` → entity IDs, types, mention counts
- `memex_get_entity_cooccurrences(entity_id)` → related entities with names, types, counts
- `memex_get_entity_mentions(entity_id)` → source facts linking back to notes
- Then read source notes via Search/Read below as needed

IF query asks about specific content or document lookup:
- **Search**: `memex_memory_search` (broad) and/or `memex_note_search` (targeted). Run in parallel.
- **Filter**: after `memex_memory_search`, call `memex_get_notes_metadata` with Note IDs. \
After `memex_note_search`, metadata is inline — skip.
- **Read**: `memex_get_page_indices` → `memex_get_nodes` (batch). \
`memex_read_note` only when total_tokens < 500.
- **Assets**: IF `has_assets: true` in page_index/metadata → `memex_list_assets` → \
`memex_get_resources` for each. Use images as visual input. Reproduce diagrams as \
Mermaid/ASCII in response. NEVER skip this step.

IF query is broad: run entity exploration AND search in parallel.

PROHIBITED:
- `memex_recent_notes` for discovery.
- Fabricating Note/Node/Unit IDs. Only use IDs from tool output.
- `memex_get_notes_metadata` after `memex_note_search` (metadata already inline).
- `memex_read_note` on notes over 500 tokens. Use `memex_get_page_indices` + `memex_get_nodes`.
- Creating diagrams without first checking assets via `memex_list_assets` → `memex_get_resources`.
- Presenting Memex information without citations.

### Citations — MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim from Memex.
2. Reference list at end of response. Each entry uses a type prefix:
   - `[note]` — title + note ID
   - `[memory]` — title + memory ID + source note ID
   - `[asset]` — filename + note ID
"""


def _setup_claude_workdir(server_url: str) -> str:
    """Create a temp directory with .mcp.json, .memex.yaml, CLAUDE.md and git init."""
    tmpdir = tempfile.mkdtemp(prefix='locomo-claude-')

    (Path(tmpdir) / '.mcp.json').write_text(
        _MCP_JSON_TEMPLATE.format(workspace=_WORKSPACE_ROOT, vault=VAULT_NAME)
    )

    base_url = server_url.rstrip('/')
    if base_url.endswith('/api/v1'):
        base_url = base_url[: -len('/api/v1')]
    (Path(tmpdir) / '.memex.yaml').write_text(
        _MEMEX_YAML_TEMPLATE.format(server_url=base_url, vault=VAULT_NAME)
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
                        'mcp__memex__memex_set_active_vault',
                        'mcp__memex__memex_list_vaults',
                    ]
                }
            },
            indent=2,
        )
    )

    subprocess.run(['git', 'init'], cwd=tmpdir, capture_output=True)

    return tmpdir


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------


def _run_claude_code(question: str, workdir: str) -> dict[str, Any]:
    """Run ``claude -p ... --output-format json`` via ``sh`` for a single question.

    Returns a dict with: answer, tool_calls, tokens, num_turns,
    cost_usd, duration_s, error.
    """
    prompt = f'Search the locomo-bench vault: {question}'
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE'}
    claude = sh.Command('claude')

    t0 = time.time()
    try:
        out = claude(
            '-p',
            prompt,
            '--output-format',
            'json',
            '--dangerously-skip-permissions',
            _cwd=workdir,
            _env=env,
            _timeout=300,
        )
        duration = time.time() - t0

        # claude --output-format json emits one JSON object per line (JSONL).
        # Conversation messages come first, the final line is the result summary.
        lines = str(out).strip().splitlines()
        result_data: dict[str, Any] = {}
        tool_calls: list[dict[str, Any]] = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            # Conversation messages: extract tool_use blocks from assistant turns
            if obj.get('type') == 'assistant':
                for block in obj.get('message', {}).get('content', []):
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        tool_calls.append(
                            {
                                'name': block.get('name', ''),
                                'input': block.get('input', {}),
                            }
                        )

            # Final result summary
            if obj.get('type') == 'result':
                result_data = obj

        answer = result_data.get('result', '')
        if not isinstance(answer, str):
            answer = str(answer)

        usage = result_data.get('usage', {})
        input_tokens = (
            usage.get('input_tokens', 0)
            + usage.get('cache_creation_input_tokens', 0)
            + usage.get('cache_read_input_tokens', 0)
        )
        output_tokens = usage.get('output_tokens', 0)
        num_turns = result_data.get('num_turns', 0)
        cost_usd = result_data.get('total_cost_usd', 0.0)

        return {
            'answer': answer,
            'tool_calls': tool_calls,
            'tokens': {'input': input_tokens, 'output': output_tokens},
            'num_turns': num_turns,
            'cost_usd': round(cost_usd, 6) if cost_usd else 0.0,
            'duration_s': round(duration, 2),
            'error': None,
        }

    except sh.TimeoutException:
        duration = time.time() - t0
        return {
            'answer': '',
            'tool_calls': [],
            'tokens': {'input': 0, 'output': 0},
            'num_turns': 0,
            'cost_usd': 0.0,
            'duration_s': round(duration, 2),
            'error': 'timeout',
        }
    except sh.ErrorReturnCode as e:
        duration = time.time() - t0
        return {
            'answer': '',
            'tool_calls': [],
            'tokens': {'input': 0, 'output': 0},
            'num_turns': 0,
            'cost_usd': 0.0,
            'duration_s': round(duration, 2),
            'error': f'claude exit code {e.exit_code}: {str(e.stderr)[:500]}',
        }
    except (json.JSONDecodeError, KeyError) as e:
        duration = time.time() - t0
        return {
            'answer': '',
            'tool_calls': [],
            'tokens': {'input': 0, 'output': 0},
            'num_turns': 0,
            'cost_usd': 0.0,
            'duration_s': round(duration, 2),
            'error': f'parse error: {e}',
        }


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def answer_questions(
    method: AnswerMethod,
    questions_path: str,
    output_path: str,
    server_url: str,
) -> int:
    """Answer questions from JSONL and write results.

    Returns the number of questions answered in this run.
    """
    if method != AnswerMethod.CLAUDE_CODE:
        raise NotImplementedError(f'{method.value} is not yet implemented.')

    questions = read_jsonl(questions_path)
    completed = read_completed_ids(output_path)
    pending = [q for q in questions if q['id'] not in completed]

    if not pending:
        console.print('[dim]All questions already answered. Nothing to do.[/dim]')
        return 0

    console.print(
        f'[bold]{len(pending)} questions to answer[/bold] '
        f'({len(completed)} already done, {len(questions)} total)'
    )

    workdir = _setup_claude_workdir(server_url)
    logger.info('Claude Code workdir: %s', workdir)

    answered = 0
    for i, q in enumerate(pending):
        logger.info(
            '[%d/%d] %s: %s',
            i + 1,
            len(pending),
            q['id'],
            q['question'][:60],
        )

        result = _run_claude_code(q['question'], workdir=workdir)

        # Capture session trace and extract tool calls from it
        output_dir = str(Path(output_path).parent)
        trace_info = _collect_session_trace(workdir, output_dir, q['id'])

        # If stdout parsing missed tool calls, extract from the trace file
        trace_file = trace_info.get('trace_file')
        if not result['tool_calls'] and trace_file:
            result['tool_calls'] = _extract_tool_calls_from_trace(trace_file)

        record = {
            'id': q['id'],
            **result,
            'session_id': trace_info['session_id'],
            'trace_file': trace_info['trace_file'],
        }
        append_jsonl(output_path, record)
        answered += 1

        tool_names = [tc['name'] for tc in result['tool_calls']]
        logger.info(
            '  -> %d tool calls (%s), %d turns, %.1fs, $%.4f. Answer: %s',
            len(result['tool_calls']),
            ', '.join(tool_names) if tool_names else 'none',
            result.get('num_turns', 0),
            result['duration_s'],
            result.get('cost_usd', 0),
            (result['answer'] or '')[:80],
        )

    console.print(f'\n[bold green]Answered {answered} questions -> {output_path}[/bold green]')
    return answered
