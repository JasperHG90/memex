"""LoCoMo Events & Observations benchmark.

Evaluates Memex's extraction quality against LoCoMo's human-annotated
event summaries and observations. Measures recall: what fraction of
ground-truth events/observations does Memex successfully capture?

Events: high-level happenings per session per speaker (e.g., "Caroline
attends an LGBTQ support group for the first time.")

Observations: specific factual claims per session per speaker, each with
a citation to a dialog turn (e.g., ["Caroline is planning to continue
her education...", "D1:9"]).
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from memex_common.client import RemoteMemexAPI

from memex_eval.judge import Judge

logger = logging.getLogger('memex_eval.locomo_eo')
console = Console()

VAULT_NAME = 'locomo-bench'


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


def _extract_events(conv_data: dict) -> list[dict]:
    """Extract all events from a conversation's event_summary field.

    Returns list of {speaker, text, session, date}.
    """
    event_summary = conv_data.get('event_summary', {})
    events = []
    session_num = 1
    while f'events_session_{session_num}' in event_summary:
        entry = event_summary[f'events_session_{session_num}']
        date = entry.get('date', '')
        for speaker in ['Caroline', 'Melanie']:
            # Generalize: use whatever speaker keys exist
            pass
        # Get all speaker keys except 'date'
        for key, items in entry.items():
            if key == 'date':
                continue
            if isinstance(items, list):
                for text in items:
                    events.append(
                        {
                            'speaker': key,
                            'text': text,
                            'session': session_num,
                            'date': date,
                        }
                    )
        session_num += 1
    return events


def _extract_observations(conv_data: dict) -> list[dict]:
    """Extract all observations from a conversation's observation field.

    Returns list of {speaker, text, citation, session}.
    """
    observation_data = conv_data.get('observation', {})
    observations = []
    session_num = 1
    while f'session_{session_num}_observation' in observation_data:
        entry = observation_data[f'session_{session_num}_observation']
        for speaker, items in entry.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, list) and len(item) >= 2:
                        observations.append(
                            {
                                'speaker': speaker,
                                'text': item[0],
                                'citation': item[1],
                                'session': session_num,
                            }
                        )
                    elif isinstance(item, str):
                        observations.append(
                            {
                                'speaker': speaker,
                                'text': item,
                                'citation': '',
                                'session': session_num,
                            }
                        )
        session_num += 1
    return observations


async def _evaluate_recall(
    api: RemoteMemexAPI,
    vault_id: str,
    items: list[dict],
    judge: Judge,
    item_type: str,
) -> dict:
    """Evaluate recall of ground-truth items against Memex.

    For each item, searches Memex and uses LLM judge to determine if
    the fact was captured.
    """
    captured = 0
    missed = 0
    total = 0
    details: list[dict] = []

    for i, item in enumerate(items):
        text = item['text']
        speaker = item['speaker']
        query = f'{speaker}: {text}'

        logger.info(
            '  [%d/%d] %s (session %d): %s',
            i + 1,
            len(items),
            speaker,
            item['session'],
            text[:60],
        )

        try:
            memories = await api.search(
                query=query,
                limit=10,
                vault_ids=[vault_id],
            )
            response_text = '\n'.join(m.text for m in memories)
        except Exception as e:
            logger.warning('    Search error: %s', e)
            response_text = ''

        if response_text:
            is_captured, reasoning = judge.judge_relevance(
                query=f'Does the system know this {item_type} about {speaker}?',
                expected=text,
                search_result=response_text,
            )
        else:
            is_captured = False
            reasoning = 'No results returned'

        total += 1
        if is_captured:
            captured += 1
        else:
            missed += 1

        details.append(
            {
                'speaker': speaker,
                'session': item['session'],
                'text': text,
                'captured': is_captured,
                'reasoning': reasoning,
            }
        )

    return {'captured': captured, 'missed': missed, 'total': total, 'details': details}


async def run_locomo_eo(
    dataset_path: str,
    server_url: str,
    judge_model: str | None = None,
    event_limit: int | None = None,
    obs_limit: int | None = None,
    seed: int = 42,
    conversation_index: int = 0,
) -> dict:
    """Run the LoCoMo Events & Observations benchmark.

    Assumes the conversation is already ingested in the locomo-bench vault.

    Args:
        dataset_path: Path to directory containing locomo.json.
        server_url: Memex API base URL.
        judge_model: Override the LLM judge model.
        event_limit: Sample this many events (None = all).
        obs_limit: Sample this many observations (None = all).
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

    events = _extract_events(conv_data)
    observations = _extract_observations(conv_data)
    total_events = len(events)
    total_obs = len(observations)

    logger.info(
        'Conversation %s: %d events, %d observations.',
        sample_id,
        total_events,
        total_obs,
    )

    rng = random.Random(seed)
    if event_limit and event_limit < total_events:
        events = rng.sample(events, event_limit)
        logger.info('Sampled %d events (seed=%d).', event_limit, seed)

    if obs_limit and obs_limit < total_obs:
        observations = rng.sample(observations, obs_limit)
        logger.info('Sampled %d observations (seed=%d).', obs_limit, seed)

    async with httpx.AsyncClient(base_url=server_url, timeout=300.0) as client:
        api = RemoteMemexAPI(client)

        # Find vault
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

        # Evaluate events
        logger.info('Evaluating %d events...', len(events))
        t0 = time.time()
        event_results = await _evaluate_recall(api, vault_id, events, judge, 'event')
        event_time = time.time() - t0

        # Evaluate observations
        logger.info('Evaluating %d observations...', len(observations))
        t1 = time.time()
        obs_results = await _evaluate_recall(api, vault_id, observations, judge, 'observation')
        obs_time = time.time() - t1

    total_captured = event_results['captured'] + obs_results['captured']
    total_items = event_results['total'] + obs_results['total']

    return {
        'benchmark': 'LoCoMo-EO',
        'conversation': sample_id,
        'seed': seed,
        'events': {
            'total_in_dataset': total_events,
            'evaluated': event_results['total'],
            'captured': event_results['captured'],
            'missed': event_results['missed'],
            'recall': (
                event_results['captured'] / event_results['total']
                if event_results['total'] > 0
                else 0.0
            ),
            'eval_time_s': round(event_time, 1),
            'details': event_results['details'],
        },
        'observations': {
            'total_in_dataset': total_obs,
            'evaluated': obs_results['total'],
            'captured': obs_results['captured'],
            'missed': obs_results['missed'],
            'recall': (
                obs_results['captured'] / obs_results['total'] if obs_results['total'] > 0 else 0.0
            ),
            'eval_time_s': round(obs_time, 1),
            'details': obs_results['details'],
        },
        'overall': {
            'captured': total_captured,
            'total': total_items,
            'recall': total_captured / total_items if total_items > 0 else 0.0,
        },
    }


def print_locomo_eo_report(results: dict) -> None:
    """Print LoCoMo Events & Observations results."""
    console.print()
    console.rule('[bold]LoCoMo Events & Observations Benchmark[/bold]')
    console.print()

    console.print(
        f'[dim]Conversation: {results.get("conversation", "?")} | seed={results["seed"]}[/dim]'
    )
    console.print()

    table = Table(show_header=True, header_style='bold cyan')
    table.add_column('Category', style='white')
    table.add_column('Captured', justify='right')
    table.add_column('Evaluated', justify='right')
    table.add_column('In Dataset', justify='right')
    table.add_column('Recall', justify='right')
    table.add_column('Time', justify='right')

    for cat_key, cat_name in [('events', 'Events'), ('observations', 'Observations')]:
        data = results[cat_key]
        if data['evaluated'] == 0:
            continue
        recall = data['recall']
        style = 'green' if recall >= 0.7 else ('yellow' if recall >= 0.4 else 'red')
        table.add_row(
            cat_name,
            str(data['captured']),
            str(data['evaluated']),
            str(data['total_in_dataset']),
            f'[{style}]{recall:.1%}[/{style}]',
            f'{data["eval_time_s"]}s',
        )

    overall = results['overall']
    recall = overall['recall']
    style = 'green' if recall >= 0.7 else ('yellow' if recall >= 0.4 else 'red')
    table.add_row(
        '[bold]Overall[/bold]',
        f'[bold]{overall["captured"]}[/bold]',
        f'[bold]{overall["total"]}[/bold]',
        '',
        f'[bold {style}]{recall:.1%}[/bold {style}]',
        '',
        end_section=True,
    )

    console.print(table)
    console.print()

    # Print missed items summary
    for cat_key, cat_name in [('events', 'Events'), ('observations', 'Observations')]:
        missed = [d for d in results[cat_key]['details'] if not d['captured']]
        if missed:
            console.print(f'[bold red]Missed {cat_name} ({len(missed)}):[/bold red]')
            for m in missed[:10]:
                console.print(
                    f'  [dim]Session {m["session"]}, {m["speaker"]}:[/dim] {m["text"][:80]}'
                )
            if len(missed) > 10:
                console.print(f'  [dim]... and {len(missed) - 10} more[/dim]')
            console.print()
