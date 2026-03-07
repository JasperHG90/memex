"""LongMemEval benchmark runner.

Ingests LongMemEval conversations into a Memex vault, then evaluates
retrieval accuracy across 5 categories using LLM-as-a-judge.
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

logger = logging.getLogger('memex_eval.longmemeval')
console = Console()

VAULT_NAME = 'longmemeval-bench'

# LongMemEval question categories
CATEGORIES = ['IE', 'MR', 'TR', 'KU', 'ABS']
CATEGORY_NAMES = {
    'IE': 'Information Extraction',
    'MR': 'Multi-hop Reasoning',
    'TR': 'Temporal Reasoning',
    'KU': 'Knowledge Update',
    'ABS': 'Abstraction',
}


def _load_dataset(dataset_path: str) -> dict:
    """Load the LongMemEval dataset from a directory.

    Expected structure:
        dataset_path/
            conversations.json  — list of conversation sessions
            questions.json      — list of questions with categories + expected answers
    """
    path = Path(dataset_path)
    conversations_file = path / 'conversations.json'
    questions_file = path / 'questions.json'

    if not conversations_file.exists():
        raise FileNotFoundError(f'conversations.json not found in {path}')
    if not questions_file.exists():
        raise FileNotFoundError(f'questions.json not found in {path}')

    conversations = json.loads(conversations_file.read_text())
    questions = json.loads(questions_file.read_text())

    return {'conversations': conversations, 'questions': questions}


async def _ingest_conversations(
    api: RemoteMemexAPI,
    vault_id: str,
    conversations: list[dict],
) -> None:
    """Ingest conversation sessions as Memex notes."""
    for i, conv in enumerate(conversations):
        content = conv.get('content', conv.get('text', json.dumps(conv)))
        title = conv.get('title', f'Conversation {i + 1}')

        note = NoteCreateDTO(
            name=title,
            description=f'LongMemEval conversation session {i + 1}',
            content=base64.b64encode(content.encode('utf-8')),
            tags=['longmemeval', 'benchmark'],
            vault_id=vault_id,
            note_key=f'longmemeval-conv-{i}',
        )

        response = await api.ingest(note)
        if hasattr(response, 'status') and response.status == 'skipped':
            logger.debug('  Conv %d skipped: %s', i, response.reason)
        else:
            logger.debug('  Conv %d ingested.', i)

    # Wait for extraction
    logger.info('Waiting for extraction to stabilize...')
    await asyncio.sleep(10)


async def _evaluate_questions(
    api: RemoteMemexAPI,
    vault_id: str,
    questions: list[dict],
    judge: Judge,
    limit: int | None = None,
) -> dict:
    """Evaluate each question against Memex results using LLM judge."""
    results_by_category: dict[str, dict] = {
        cat: {'correct': 0, 'total': 0, 'details': []} for cat in CATEGORIES
    }

    eval_questions = questions[:limit] if limit else questions

    for i, q in enumerate(eval_questions):
        category = q.get('category', 'IE')
        question_text = q['question']
        expected = q['expected_answer']

        logger.info('  [%d/%d] %s: %s', i + 1, len(eval_questions), category, question_text[:60])

        # Query Memex
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

        # Judge
        if response_text:
            is_correct, reasoning = judge.judge_correctness(
                question=question_text,
                expected=expected,
                response=response_text,
            )
        else:
            is_correct = False
            reasoning = 'No results returned'

        if category in results_by_category:
            results_by_category[category]['total'] += 1
            if is_correct:
                results_by_category[category]['correct'] += 1
            results_by_category[category]['details'].append(
                {
                    'question': question_text,
                    'expected': expected,
                    'response': response_text[:500],
                    'correct': is_correct,
                    'reasoning': reasoning,
                }
            )

    return results_by_category


async def run_longmemeval(
    dataset_path: str,
    server_url: str,
    judge_model: str | None = None,
    limit: int | None = None,
) -> dict:
    """Run the full LongMemEval benchmark.

    Returns a dict with per-category results and overall accuracy.
    """
    judge = Judge(model=judge_model)
    dataset = _load_dataset(dataset_path)

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
                CreateVaultRequest(name=VAULT_NAME, description='LongMemEval benchmark vault.')
            )
            vault_id = str(vault.id)

        await api.set_writer_vault(vault_id)

        # Ingest
        logger.info('Ingesting %d conversations...', len(dataset['conversations']))
        await _ingest_conversations(api, vault_id, dataset['conversations'])

        # Evaluate
        logger.info('Evaluating %d questions...', len(dataset['questions']))
        results = await _evaluate_questions(api, vault_id, dataset['questions'], judge, limit=limit)

    # Compute overall
    total_correct = sum(r['correct'] for r in results.values())
    total_questions = sum(r['total'] for r in results.values())

    return {
        'benchmark': 'LongMemEval',
        'categories': results,
        'overall': {
            'correct': total_correct,
            'total': total_questions,
            'accuracy': total_correct / total_questions if total_questions > 0 else 0.0,
        },
    }


def print_longmemeval_report(results: dict) -> None:
    """Print LongMemEval results as a rich table."""
    console.print()
    console.rule('[bold]LongMemEval Benchmark Results[/bold]')
    console.print()

    table = Table(show_header=True, header_style='bold cyan')
    table.add_column('Category', style='white')
    table.add_column('Description', ratio=2)
    table.add_column('Correct', justify='right')
    table.add_column('Total', justify='right')
    table.add_column('Accuracy', justify='right')

    for cat in CATEGORIES:
        data = results['categories'].get(cat, {'correct': 0, 'total': 0})
        total = data['total']
        correct = data['correct']
        acc = correct / total if total > 0 else 0.0
        style = 'green' if acc >= 0.7 else ('yellow' if acc >= 0.4 else 'red')
        table.add_row(
            cat,
            CATEGORY_NAMES.get(cat, cat),
            str(correct),
            str(total),
            f'[{style}]{acc:.1%}[/{style}]',
        )

    overall = results['overall']
    acc = overall['accuracy']
    style = 'green' if acc >= 0.7 else ('yellow' if acc >= 0.4 else 'red')
    table.add_row(
        '[bold]Overall[/bold]',
        '',
        f'[bold]{overall["correct"]}[/bold]',
        f'[bold]{overall["total"]}[/bold]',
        f'[bold {style}]{acc:.1%}[/bold {style}]',
        end_section=True,
    )

    console.print(table)
    console.print()
