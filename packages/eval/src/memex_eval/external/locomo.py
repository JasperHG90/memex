"""LoCoMo benchmark runner.

Evaluates Memex against the LoCoMo multi-session conversation benchmark.
Reports accuracy by question type: Single-Hop, Multi-Hop, Open Domain, Temporal.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import CreateVaultRequest, NoteCreateDTO

from memex_eval.judge import Judge

logger = logging.getLogger('memex_eval.locomo')
console = Console()

VAULT_NAME = 'locomo-bench'

QUESTION_TYPES = ['single_hop', 'multi_hop', 'open_domain', 'temporal']
TYPE_NAMES = {
    'single_hop': 'Single-Hop',
    'multi_hop': 'Multi-Hop',
    'open_domain': 'Open Domain',
    'temporal': 'Temporal',
}


def _load_dataset(dataset_path: str) -> list[dict]:
    """Load the LoCoMo dataset.

    Expected structure:
        dataset_path/
            conversations/
                conv_001.json  — each has sessions + questions
                conv_002.json
                ...
    OR:
        dataset_path/
            locomo.json  — single file with all conversations
    """
    path = Path(dataset_path)

    # Try single-file format
    single_file = path / 'locomo.json'
    if single_file.exists():
        return json.loads(single_file.read_text())

    # Try multi-file format
    conv_dir = path / 'conversations'
    if conv_dir.exists():
        conversations = []
        for f in sorted(conv_dir.glob('*.json')):
            conversations.append(json.loads(f.read_text()))
        return conversations

    raise FileNotFoundError(f'Expected locomo.json or conversations/ directory in {path}')


async def _process_conversation(
    api: RemoteMemexAPI,
    vault_id: str,
    conversation: dict,
    conv_index: int,
    judge: Judge,
) -> dict[str, dict]:
    """Process a single LoCoMo conversation: ingest sessions, evaluate questions."""
    results: dict[str, dict] = {
        qt: {'correct': 0, 'total': 0, 'details': []} for qt in QUESTION_TYPES
    }

    # Ingest sessions
    sessions = conversation.get('sessions', conversation.get('dialogues', []))
    for j, session in enumerate(sessions):
        content = session.get('content', session.get('text', json.dumps(session)))
        title = session.get('title', f'Conv {conv_index + 1} Session {j + 1}')

        note = NoteCreateDTO(
            name=title,
            description=f'LoCoMo conversation {conv_index + 1}, session {j + 1}',
            content=base64.b64encode(content.encode('utf-8')),
            tags=['locomo', 'benchmark'],
            vault_id=vault_id,
            note_key=f'locomo-c{conv_index}-s{j}',
        )

        response = await api.ingest(note)
        if hasattr(response, 'status') and response.status != 'skipped':
            logger.debug('  Session %d.%d ingested.', conv_index, j)

    # Wait for extraction
    await asyncio.sleep(5)

    # Evaluate questions
    questions = conversation.get('questions', conversation.get('qa_pairs', []))
    for q in questions:
        q_type = q.get('type', q.get('question_type', 'single_hop'))
        question_text = q.get('question', q.get('query', ''))
        expected = q.get('answer', q.get('expected_answer', ''))

        if q_type not in results:
            q_type = 'single_hop'

        try:
            memories = await api.search(
                query=question_text,
                limit=10,
                vault_ids=[vault_id],
            )
            response_text = '\n'.join(m.text for m in memories)
        except Exception as e:
            logger.warning('    Query error: %s', e)
            response_text = ''

        if response_text:
            is_correct, reasoning = judge.judge_correctness(
                question=question_text,
                expected=expected,
                response=response_text,
            )
        else:
            is_correct = False
            reasoning = 'No results returned'

        results[q_type]['total'] += 1
        if is_correct:
            results[q_type]['correct'] += 1
        results[q_type]['details'].append(
            {
                'question': question_text,
                'expected': expected,
                'correct': is_correct,
                'reasoning': reasoning,
            }
        )

    return results


async def run_locomo(
    dataset_path: str,
    server_url: str,
    judge_model: str | None = None,
    limit: int | None = None,
) -> dict:
    """Run the full LoCoMo benchmark.

    Returns a dict with per-type results and overall accuracy.
    """
    judge = Judge(model=judge_model)
    conversations = _load_dataset(dataset_path)

    if limit:
        conversations = conversations[:limit]

    async with httpx.AsyncClient(base_url=server_url, timeout=180.0) as client:
        api = RemoteMemexAPI(client)

        # Setup vault
        vaults = await api.list_vaults()
        vault_id = None
        for v in vaults:
            if v.name == VAULT_NAME:
                vault_id = str(v.id)
                break

        if not vault_id:
            vault = await api.create_vault(
                CreateVaultRequest(name=VAULT_NAME, description='LoCoMo benchmark vault.')
            )
            vault_id = str(vault.id)

        await api.set_writer_vault(vault_id)

        # Process each conversation
        aggregate: dict[str, dict] = {
            qt: {'correct': 0, 'total': 0, 'details': []} for qt in QUESTION_TYPES
        }

        for i, conv in enumerate(conversations):
            logger.info('Processing conversation %d/%d...', i + 1, len(conversations))
            conv_results = await _process_conversation(api, vault_id, conv, i, judge)

            for qt in QUESTION_TYPES:
                aggregate[qt]['correct'] += conv_results[qt]['correct']
                aggregate[qt]['total'] += conv_results[qt]['total']
                aggregate[qt]['details'].extend(conv_results[qt]['details'])

    total_correct = sum(r['correct'] for r in aggregate.values())
    total_questions = sum(r['total'] for r in aggregate.values())

    return {
        'benchmark': 'LoCoMo',
        'question_types': aggregate,
        'overall': {
            'correct': total_correct,
            'total': total_questions,
            'accuracy': total_correct / total_questions if total_questions > 0 else 0.0,
        },
    }


def print_locomo_report(results: dict) -> None:
    """Print LoCoMo results as a rich table."""
    console.print()
    console.rule('[bold]LoCoMo Benchmark Results[/bold]')
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
        acc = correct / total if total > 0 else 0.0
        style = 'green' if acc >= 0.7 else ('yellow' if acc >= 0.4 else 'red')
        table.add_row(
            TYPE_NAMES.get(qt, qt),
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
