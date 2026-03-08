"""LoCoMo Phase 1: Export questions to JSONL.

Loads the LoCoMo dataset and exports questions as a JSONL file
for consumption by the answer phase.
"""

from __future__ import annotations

import logging
import random

from rich.console import Console

from memex_eval.external.locomo_common import (
    CATEGORY_MAP,
    CATEGORY_NAMES,
    QUESTION_TYPES,
    load_dataset,
    append_jsonl,
)

logger = logging.getLogger('memex_eval.locomo_export')
console = Console()


def export_questions(
    dataset_path: str,
    output: str,
    limit: int | None,
    seed: int,
    conversation_index: int,
) -> int:
    """Export LoCoMo questions to JSONL.

    Returns the number of questions exported.
    """
    conversations = load_dataset(dataset_path)

    if conversation_index >= len(conversations):
        raise ValueError(
            f'Conversation index {conversation_index} out of range (0-{len(conversations) - 1})'
        )

    conv_data = conversations[conversation_index]
    all_qa = conv_data.get('qa', [])
    total_qa = len(all_qa)

    logger.info(
        'Conversation %s: %d QA pairs available.',
        conv_data.get('sample_id', conversation_index),
        total_qa,
    )

    if limit and limit < total_qa:
        rng = random.Random(seed)
        all_qa = rng.sample(all_qa, limit)
        logger.info('Sampled %d QA pairs (seed=%d).', limit, seed)

    # Track counts by category
    counts: dict[str, int] = {qt: 0 for qt in QUESTION_TYPES}

    for i, q in enumerate(all_qa):
        cat_int = q.get('category', 1)
        category = CATEGORY_MAP.get(cat_int, 'single_hop')

        # For adversarial questions, use adversarial_answer
        if cat_int == 5:
            expected = q.get('adversarial_answer', q.get('answer', ''))
        else:
            expected = q.get('answer', '')

        evidence = q.get('evidence', [])

        record = {
            'id': f'q-{i + 1:03d}',
            'question': q['question'],
            'expected': expected,
            'category': category,
            'category_id': cat_int,
            'evidence': evidence,
        }

        append_jsonl(output, record)
        counts[category] += 1

    # Print summary
    console.print()
    console.print(f'[bold]Exported {len(all_qa)} questions to {output}[/bold]')
    for qt in QUESTION_TYPES:
        if counts[qt] > 0:
            console.print(f'  {CATEGORY_NAMES[qt]}: {counts[qt]}')
    console.print()

    return len(all_qa)
