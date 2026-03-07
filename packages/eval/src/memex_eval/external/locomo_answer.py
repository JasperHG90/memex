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
server:
  active_vault: "{vault}"
  attached_vaults: []
"""

_CLAUDE_MD = """\
# Memex Memory Retrieval

You have access to Memex, a long-term memory system. Use it to answer questions \
about conversations between people.

## Retrieval

`memex_memory_search` — atomic facts, observations, mental models across the knowledge graph.
`memex_note_search` — raw source notes with inline metadata (title, description, tags) \
via hybrid retrieval. Use the metadata to filter before reading.

## Note reading

1. `memex_get_page_index` (Note ID -> table of contents)
2. `memex_get_node` (node ID -> section text)
3. Fallback only: `memex_read_note`

## Workflow

1. Search memories first with `memex_memory_search`
2. If needed, search source notes with `memex_note_search`
3. For verification, use two-speed reading: `memex_get_page_index` then `memex_get_node`
4. Answer the question concisely based on what you found
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
                        'mcp__memex__memex_get_page_index',
                        'mcp__memex__memex_get_node',
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

        record = {'id': q['id'], **result}
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
