"""LoCoMo Agent-based benchmark.

Evaluates Memex using an agentic retrieval loop: an LLM gets access to
Memex tools and the same retrieval instructions from AGENTS.md that a
real agent would use. It reasons through each question iteratively.

This tests the full two-speed recall system rather than a single search call.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path
from uuid import UUID

import httpx
from litellm import completion
from rich.console import Console
from rich.table import Table

from memex_common.client import RemoteMemexAPI

from memex_eval.judge import Judge

logger = logging.getLogger('memex_eval.locomo_agent')
console = Console()

VAULT_NAME = 'locomo-bench'

CATEGORY_MAP = {
    1: 'single_hop',
    2: 'multi_hop',
    3: 'open_domain',
    4: 'temporal',
    5: 'adversarial',
}
CATEGORY_NAMES = {
    'single_hop': 'Single-Hop',
    'multi_hop': 'Multi-Hop',
    'open_domain': 'Open Domain',
    'temporal': 'Temporal',
    'adversarial': 'Adversarial',
}
QUESTION_TYPES = list(CATEGORY_NAMES.keys())

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'memex_memory_search',
            'description': (
                'Search Memex for atomic facts, observations, and mental models '
                'across the knowledge graph. Returns memory units with text, '
                'source note IDs, and source node IDs.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'The search query.'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'memex_note_search',
            'description': (
                'Search Memex source notes (raw documents) with inline metadata '
                '(title, description, tags) via hybrid retrieval. Use the metadata '
                'to filter before reading.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'The search query.'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'memex_get_page_index',
            'description': (
                'Get the table of contents (page index) for a note. '
                'Returns section titles and node IDs. Use Note ID from search results.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'note_id': {'type': 'string', 'description': 'The UUID of the note.'},
                },
                'required': ['note_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'memex_get_node',
            'description': (
                'Read a specific section/node from a note by node ID. '
                'Returns the full text of that section.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'node_id': {'type': 'string', 'description': 'The UUID of the node.'},
                },
                'required': ['node_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'submit_answer',
            'description': 'Submit your final answer to the question.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'answer': {'type': 'string', 'description': 'Your final answer.'},
                },
                'required': ['answer'],
            },
        },
    },
]

# Derived from AGENTS.md retrieval instructions — what a real agent sees
SYSTEM_PROMPT = """\
You have access to Memex, a long-term memory system. Use it to answer questions \
about conversations between people.

## Retrieval

`memex_memory_search` — atomic facts, observations, mental models across the knowledge graph.
`memex_note_search` — raw source notes with inline metadata (title, description, tags) via \
hybrid retrieval. Use the metadata to filter before reading.

## Note reading

1. `memex_get_page_index` (Note ID -> table of contents)
2. `memex_get_node` (node ID -> section text)

## Workflow

Use the tools above to find the answer. When done, call `submit_answer` with your response."""


def _load_dataset(dataset_path: str) -> list[dict]:
    path = Path(dataset_path)
    for candidate in ['locomo.json', 'locomo10.json']:
        f = path / candidate
        if f.exists():
            return json.loads(f.read_text())
    if path.is_file() and path.suffix == '.json':
        return json.loads(path.read_text())
    raise FileNotFoundError(f'No LoCoMo dataset found in {path}.')


async def _execute_tool(
    api: RemoteMemexAPI,
    vault_id: str,
    tool_name: str,
    arguments: dict,
) -> str:
    """Execute a tool call and return the result as a string."""
    if tool_name == 'memex_memory_search':
        query = arguments['query']
        memories = await api.search(query=query, limit=10, vault_ids=[vault_id])
        results = []
        for m in memories:
            entry = f'- {m.text}'
            if m.note_id:
                entry += f'\n  [source_note: {m.note_id}]'
            if m.node_ids:
                entry += f'\n  [source_nodes: {", ".join(m.node_ids)}]'
            results.append(entry)
        return '\n'.join(results) if results else 'No results found.'

    elif tool_name == 'memex_note_search':
        query = arguments['query']
        notes = await api.search_notes(query=query, limit=5, vault_ids=[vault_id])
        results = []
        for n in notes:
            meta = n.metadata or {}
            title = meta.get('title', meta.get('name', 'Untitled'))
            desc = meta.get('description', '')
            snippets = '\n'.join(f'  > {s.text[:200]}' for s in (n.snippets or [])[:3])
            results.append(
                f'- Note: {title} [note_id: {n.note_id}]\n  Description: {desc}\n{snippets}'
            )
        return '\n'.join(results) if results else 'No notes found.'

    elif tool_name == 'memex_get_page_index':
        note_id = arguments['note_id']
        page_index = await api.get_note_page_index(UUID(note_id))
        if not page_index:
            return 'No table of contents available for this note.'
        return json.dumps(page_index, indent=2, default=str)

    elif tool_name == 'memex_get_node':
        node_id = arguments['node_id']
        node = await api.get_node(UUID(node_id))
        if not node:
            return 'Section not found.'
        return f'## {node.title}\n\n{node.text}'

    elif tool_name == 'submit_answer':
        return arguments['answer']

    return f'Unknown tool: {tool_name}'


async def _run_agent(
    api: RemoteMemexAPI,
    vault_id: str,
    question: str,
    agent_model: str,
    max_turns: int = 10,
) -> tuple[str, int]:
    """Run the agent loop for a single question.

    Returns (answer, tool_call_count).
    """
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': question},
    ]
    tool_calls_total = 0

    for _turn in range(max_turns):
        response = completion(
            model=agent_model,
            messages=messages,
            tools=TOOLS,
            tool_choice='auto',
            temperature=0.0,
        )

        choice = response.choices[0]
        message = choice.message

        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            return message.content or '', tool_calls_total

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                arguments = {}

            tool_calls_total += 1

            if tool_name == 'submit_answer':
                return arguments.get('answer', ''), tool_calls_total

            result = await _execute_tool(api, vault_id, tool_name, arguments)

            messages.append(
                {
                    'role': 'tool',
                    'tool_call_id': tool_call.id,
                    'content': result,
                }
            )

    return 'Could not determine answer within tool call limit.', tool_calls_total


async def _evaluate_questions(
    api: RemoteMemexAPI,
    vault_id: str,
    qa_items: list[dict],
    judge: Judge,
    agent_model: str,
) -> dict[str, dict]:
    """Evaluate QA pairs using agent retrieval + LLM judge."""
    results: dict[str, dict] = {
        qt: {'correct': 0, 'total': 0, 'details': []} for qt in QUESTION_TYPES
    }

    for i, q in enumerate(qa_items):
        cat_int = q.get('category', 1)
        q_type = CATEGORY_MAP.get(cat_int, 'single_hop')
        question_text = q['question']
        expected = q.get('answer', q.get('adversarial_answer', ''))

        logger.info(
            '  [%d/%d] %s: %s',
            i + 1,
            len(qa_items),
            CATEGORY_NAMES.get(q_type, q_type),
            question_text[:60],
        )

        try:
            answer, tool_count = await _run_agent(api, vault_id, question_text, agent_model)
            logger.info('    %d tool calls. Answer: %s', tool_count, answer[:80])
        except Exception as e:
            logger.warning('    Agent error: %s', e)
            answer = ''
            tool_count = 0

        if answer:
            is_correct, reasoning = judge.judge_correctness(
                question=question_text,
                expected=expected,
                response=answer,
            )
        else:
            is_correct = False
            reasoning = 'No answer produced'

        results[q_type]['total'] += 1
        if is_correct:
            results[q_type]['correct'] += 1
        results[q_type]['details'].append(
            {
                'question': question_text,
                'expected': expected,
                'answer': answer[:500],
                'correct': is_correct,
                'reasoning': reasoning,
                'tool_calls': tool_count,
            }
        )

    return results


async def run_locomo_agent(
    dataset_path: str,
    server_url: str,
    agent_model: str | None = None,
    judge_model: str | None = None,
    limit: int | None = None,
    seed: int = 42,
    conversation_index: int = 0,
) -> dict:
    """Run the LoCoMo agent-based benchmark.

    Assumes the conversation is already ingested in the locomo-bench vault.
    """
    agent_model = agent_model or os.environ.get('EVAL_AGENT_MODEL') or 'gemini/gemini-2.5-flash'
    judge = Judge(model=judge_model)
    conversations = _load_dataset(dataset_path)

    if conversation_index >= len(conversations):
        raise ValueError(
            f'Conversation index {conversation_index} out of range (0-{len(conversations) - 1})'
        )

    conv_data = conversations[conversation_index]
    sample_id = conv_data.get('sample_id', conversation_index)
    all_qa = conv_data.get('qa', [])
    total_qa = len(all_qa)

    logger.info(
        'Conversation %s (%d QA pairs). Agent: %s',
        sample_id,
        total_qa,
        agent_model,
    )

    if limit and limit < total_qa:
        rng = random.Random(seed)
        all_qa = rng.sample(all_qa, limit)
        logger.info('Sampled %d QA pairs (seed=%d).', limit, seed)

    async with httpx.AsyncClient(base_url=server_url, timeout=300.0) as client:
        api = RemoteMemexAPI(client)

        vaults = await api.list_vaults()
        vault_id = None
        for v in vaults:
            if v.name == VAULT_NAME:
                vault_id = str(v.id)
                break

        if not vault_id:
            raise RuntimeError(
                f'Vault "{VAULT_NAME}" not found. Run `memex-eval locomo` first to ingest data.'
            )

        logger.info('Evaluating %d questions with agent loop...', len(all_qa))
        t0 = time.time()
        results = await _evaluate_questions(api, vault_id, all_qa, judge, agent_model)
        eval_time = time.time() - t0
        logger.info('Evaluation completed in %.1fs.', eval_time)

    total_correct = sum(r['correct'] for r in results.values())
    total_questions = sum(r['total'] for r in results.values())
    total_tool_calls = sum(d['tool_calls'] for r in results.values() for d in r['details'])

    return {
        'benchmark': 'LoCoMo-Agent',
        'conversation': sample_id,
        'agent_model': agent_model,
        'total_qa_in_conversation': total_qa,
        'sampled': len(all_qa),
        'seed': seed,
        'eval_time_s': round(eval_time, 1),
        'total_tool_calls': total_tool_calls,
        'avg_tool_calls': round(total_tool_calls / max(total_questions, 1), 1),
        'question_types': results,
        'overall': {
            'correct': total_correct,
            'total': total_questions,
            'accuracy': total_correct / total_questions if total_questions > 0 else 0.0,
        },
    }


def print_locomo_agent_report(results: dict) -> None:
    """Print LoCoMo Agent results as a rich table."""
    console.print()
    console.rule('[bold]LoCoMo Agent Benchmark Results[/bold]')
    console.print()

    console.print(
        f'[dim]Conversation: {results.get("conversation", "?")} | '
        f'Agent: {results.get("agent_model", "?")} | '
        f'{results["sampled"]}/{results["total_qa_in_conversation"]} QA pairs '
        f'(seed={results["seed"]}) | '
        f'Tool calls: {results["total_tool_calls"]} '
        f'(avg {results["avg_tool_calls"]}/q) | '
        f'Eval: {results.get("eval_time_s", "?")}s[/dim]'
    )
    console.print()

    table = Table(show_header=True, header_style='bold cyan')
    table.add_column('Question Type', style='white')
    table.add_column('Correct', justify='right')
    table.add_column('Total', justify='right')
    table.add_column('Accuracy', justify='right')

    for qt in QUESTION_TYPES:
        data = results['question_types'].get(qt, {'correct': 0, 'total': 0})
        total = data['total']
        correct = data['correct']
        if total == 0:
            continue
        acc = correct / total
        style = 'green' if acc >= 0.7 else ('yellow' if acc >= 0.4 else 'red')
        table.add_row(
            CATEGORY_NAMES.get(qt, qt),
            str(correct),
            str(total),
            f'[{style}]{acc:.1%}[/{style}]',
        )

    overall = results['overall']
    acc = overall['accuracy']
    style = 'green' if acc >= 0.7 else ('yellow' if acc >= 0.4 else 'red')
    table.add_row(
        '[bold]Overall[/bold]',
        f'[bold]{overall["correct"]}[/bold]',
        f'[bold]{overall["total"]}[/bold]',
        f'[bold {style}]{acc:.1%}[/bold {style}]',
        end_section=True,
    )

    console.print(table)
    console.print()
