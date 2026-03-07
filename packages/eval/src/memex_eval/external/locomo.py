"""LoCoMo benchmark runner.

Evaluates Memex against the LoCoMo multi-session conversation benchmark.
Ingests a single conversation (multiple sessions as markdown notes), then
evaluates QA pairs using LLM-as-a-judge.

Dataset: https://github.com/snap-research/locomo
Format: locomo10.json — 10 conversations, each with:
  - conversation: dict with session_N keys (list of {speaker, text} turns)
  - qa: list of {question, answer, evidence, category} where category is int:
    1=single-hop, 2=multi-hop, 3=open-domain, 4=temporal, 5=adversarial
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
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


def _load_dataset(dataset_path: str) -> list[dict]:
    """Load the LoCoMo dataset."""
    path = Path(dataset_path)
    for candidate in ['locomo.json', 'locomo10.json']:
        f = path / candidate
        if f.exists():
            return json.loads(f.read_text())
    if path.is_file() and path.suffix == '.json':
        return json.loads(path.read_text())
    raise FileNotFoundError(f'No LoCoMo dataset found in {path}.')


def _session_to_markdown(
    turns: list[dict],
    session_num: int,
    date_time: str,
    speaker_a: str,
    speaker_b: str,
) -> str:
    """Convert a LoCoMo session (list of turns) to clean markdown."""
    lines = [
        f'# Conversation — Session {session_num}',
        '',
        f'**Date:** {date_time}' if date_time else '',
        f'**Participants:** {speaker_a}, {speaker_b}',
        '',
        '---',
        '',
    ]

    for turn in turns:
        speaker = turn.get('speaker', 'Unknown')
        text = turn.get('text', '')
        lines.append(f'**{speaker}:** {text}')
        lines.append('')

    return '\n'.join(lines)


async def _ingest_conversation(
    api: RemoteMemexAPI,
    vault_id: str,
    conversation_data: dict,
) -> int:
    """Ingest all sessions from one conversation as markdown notes."""
    conv = conversation_data['conversation']
    sample_id = conversation_data.get('sample_id', 'unknown')
    speaker_a = conv.get('speaker_a', 'Speaker A')
    speaker_b = conv.get('speaker_b', 'Speaker B')

    ingested = 0
    session_num = 1
    while f'session_{session_num}' in conv:
        key = f'session_{session_num}'
        date_key = f'session_{session_num}_date_time'
        turns = conv[key]
        date_time = conv.get(date_key, '')

        markdown = _session_to_markdown(turns, session_num, date_time, speaker_a, speaker_b)

        title = f'{speaker_a} & {speaker_b} — Session {session_num}'
        description = (
            f'Conversation between {speaker_a} and {speaker_b}, session {session_num}. {date_time}'
        )

        note = NoteCreateDTO(
            name=title,
            description=description,
            content=base64.b64encode(markdown.encode('utf-8')),
            tags=['locomo', 'conversation', f'conv-{sample_id}'],
            vault_id=vault_id,
            note_key=f'locomo-{sample_id}-session-{session_num}',
        )

        try:
            response = await api.ingest(note)
            if hasattr(response, 'status') and response.status == 'skipped':
                logger.debug('  Session %d skipped (idempotent).', session_num)
            else:
                ingested += 1
                logger.info('  Ingested session %d/%s.', session_num, key)
        except Exception as e:
            logger.warning('  Failed to ingest session %d: %s', session_num, e)

        session_num += 1

    return ingested


async def _wait_for_extraction(api: RemoteMemexAPI, vault_id: str, timeout: int = 120) -> None:
    """Poll stats until memory count stabilizes."""
    last_count = -1
    stable_rounds = 0
    start = time.time()

    while time.time() - start < timeout:
        await asyncio.sleep(5)
        try:
            stats = await api.get_stats_counts(vault_ids=[vault_id])
            current = stats.memories
        except Exception:
            current = 0

        if current == last_count and current > 0:
            stable_rounds += 1
            if stable_rounds >= 2:
                logger.info('  Extraction stable at %d memories.', current)
                return
        else:
            stable_rounds = 0
            last_count = current

    logger.warning('  Extraction did not stabilize within %ds.', timeout)


async def _evaluate_questions(
    api: RemoteMemexAPI,
    vault_id: str,
    qa_items: list[dict],
    judge: Judge,
) -> dict[str, dict]:
    """Evaluate QA pairs using LLM judge."""
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
    seed: int = 42,
    conversation_index: int = 0,
) -> dict:
    """Run the LoCoMo benchmark on a single conversation.

    Args:
        dataset_path: Path to directory containing locomo.json.
        server_url: Memex API base URL.
        judge_model: Override the LLM judge model.
        limit: Randomly sample this many QA pairs (with seed).
        seed: Random seed for reproducible sampling.
        conversation_index: Which conversation to use (0-9).
    """
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
        'Using conversation %s (%d sessions, %d QA pairs).',
        sample_id,
        len(
            [
                k
                for k in conv_data['conversation']
                if k.startswith('session_') and not k.endswith('_date_time')
            ]
        ),
        total_qa,
    )

    if limit and limit < total_qa:
        rng = random.Random(seed)
        all_qa = rng.sample(all_qa, limit)
        logger.info('Sampled %d QA pairs (seed=%d).', limit, seed)

    async with httpx.AsyncClient(base_url=server_url, timeout=300.0) as client:
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

        # Ingest
        logger.info('Ingesting conversation sessions as markdown...')
        t0 = time.time()
        ingested = await _ingest_conversation(api, vault_id, conv_data)
        ingest_time = time.time() - t0
        logger.info('Ingested %d sessions in %.1fs.', ingested, ingest_time)

        # Wait for extraction
        if ingested > 0:
            await _wait_for_extraction(api, vault_id, timeout=max(120, ingested * 10))

        # Evaluate
        logger.info('Evaluating %d questions with LLM judge...', len(all_qa))
        t1 = time.time()
        results = await _evaluate_questions(api, vault_id, all_qa, judge)
        eval_time = time.time() - t1
        logger.info('Evaluation completed in %.1fs.', eval_time)

    total_correct = sum(r['correct'] for r in results.values())
    total_questions = sum(r['total'] for r in results.values())

    return {
        'benchmark': 'LoCoMo',
        'conversation': sample_id,
        'total_qa_in_conversation': total_qa,
        'sampled': len(all_qa),
        'seed': seed,
        'ingest_time_s': round(ingest_time, 1),
        'eval_time_s': round(eval_time, 1),
        'question_types': results,
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

    console.print(
        f'[dim]Conversation: {results.get("conversation", "?")} | '
        f'{results["sampled"]}/{results["total_qa_in_conversation"]} QA pairs '
        f'(seed={results["seed"]}) | '
        f'Ingest: {results.get("ingest_time_s", "?")}s | '
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
