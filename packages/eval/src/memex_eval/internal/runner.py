"""Orchestrates the internal benchmark end-to-end."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from uuid import UUID

import httpx

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import (
    CreateVaultRequest,
    NoteCreateDTO,
    ReflectionRequest,
)

from memex_eval.internal.checks import run_check
from memex_eval.internal.scenarios import (
    ALL_GROUPS,
    GroundTruthCheck,
    ScenarioGroup,
    get_group,
)
from memex_eval.judge import Judge
from memex_eval.metrics import (
    BenchmarkResult,
    CheckStatus,
    CheckResult,
    GroupResult,
)

logger = logging.getLogger('memex_eval.runner')

VAULT_NAME = 'benchmark-eval'
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 120.0


async def run_benchmark(
    server_url: str,
    group_filter: str | None = None,
    use_llm_judge: bool = True,
    judge_model: str | None = None,
) -> BenchmarkResult:
    """Run the full internal benchmark suite.

    Args:
        server_url: Base URL for the Memex API server.
        group_filter: If set, only run the named group.
        use_llm_judge: Whether to use LLM-as-a-judge for semantic checks.
        judge_model: Override the default judge model.

    Returns:
        BenchmarkResult with all group and check results.
    """
    result = BenchmarkResult(vault_name=VAULT_NAME)

    judge: Judge | None = None
    if use_llm_judge:
        try:
            judge = Judge(model=judge_model)
        except ValueError as e:
            logger.warning('LLM judge unavailable: %s', e)

    groups = _select_groups(group_filter)
    if not groups:
        logger.error('No matching groups found for filter: %s', group_filter)
        return result

    async with httpx.AsyncClient(base_url=server_url, timeout=180.0) as client:
        api = RemoteMemexAPI(client)

        vault_id = await _setup_vault(api)
        logger.info('Using vault %s (id=%s)', VAULT_NAME, vault_id)

        for group in groups:
            logger.info('Running group: %s', group.name)
            group_result = await _run_group(api, vault_id, group, judge)
            result.groups.append(group_result)

    result.finished_at = dt.datetime.now(dt.timezone.utc)
    return result


def _select_groups(group_filter: str | None) -> list[ScenarioGroup]:
    """Select groups to run based on filter."""
    if group_filter is None:
        return ALL_GROUPS
    group = get_group(group_filter)
    if group is None:
        return []
    return [group]


async def _setup_vault(api: RemoteMemexAPI) -> UUID:
    """Create or clean the benchmark vault."""
    vaults = await api.list_vaults()
    for vault in vaults:
        if vault.name == VAULT_NAME:
            logger.info('Cleaning existing benchmark vault...')
            # Delete all notes in the vault
            notes = await api.list_notes(vault_id=vault.id, limit=500)
            for note in notes:
                await api.delete_note(note.id)
            await api.set_writer_vault(str(vault.id))
            return vault.id

    logger.info('Creating benchmark vault...')
    vault = await api.create_vault(
        CreateVaultRequest(name=VAULT_NAME, description='Automated quality benchmark vault.')
    )
    await api.set_writer_vault(str(vault.id))
    return vault.id


async def _run_group(
    api: RemoteMemexAPI,
    vault_id: UUID,
    group: ScenarioGroup,
    judge: Judge | None,
) -> GroupResult:
    """Run all checks for a scenario group."""
    group_result = GroupResult(name=group.name, description=group.description)

    # Phase 1: Ingest documents
    if group.docs:
        ingest_start = time.monotonic()
        await _ingest_docs(api, vault_id, group)
        group_result.ingest_duration_ms = (time.monotonic() - ingest_start) * 1000

    # Phase 2: Handle reflection group specially
    if group.name == 'reflection':
        reflection_start = time.monotonic()
        await _trigger_reflections(api, vault_id)
        group_result.reflection_duration_ms = (time.monotonic() - reflection_start) * 1000

    # Phase 3: Run checks
    for check in group.checks:
        check_result = await _execute_check(api, vault_id, group.name, check, judge)
        group_result.checks.append(check_result)

    return group_result


async def _ingest_docs(
    api: RemoteMemexAPI,
    vault_id: UUID,
    group: ScenarioGroup,
) -> None:
    """Ingest scenario documents into the vault."""
    for doc in group.docs:
        note = NoteCreateDTO(
            name=doc.title,
            description=doc.description,
            content=doc.content_b64,
            tags=doc.tags,
            vault_id=str(vault_id),
            note_key=f'bench-{doc.filename}',
        )
        logger.info('  Ingesting: %s', doc.title)
        response = await api.ingest(note)

        if hasattr(response, 'status') and response.status == 'skipped':
            logger.info('    Skipped: %s', response.reason)
        elif hasattr(response, 'note_id'):
            logger.info('    OK: note_id=%s', response.note_id)

        # For sequential ingest, wait between docs to allow extraction
        if group.sequential_ingest:
            await _wait_for_extraction(api, vault_id)

    # Wait for all extraction to complete
    if not group.sequential_ingest:
        await _wait_for_extraction(api, vault_id)


async def _wait_for_extraction(api: RemoteMemexAPI, vault_id: UUID) -> None:
    """Poll stats until extraction stabilizes."""
    logger.info('  Waiting for extraction to complete...')
    prev_count = -1
    stable_ticks = 0
    start = time.monotonic()

    while time.monotonic() - start < POLL_TIMEOUT:
        await asyncio.sleep(POLL_INTERVAL)
        stats = await api.get_stats_counts(vault_id=vault_id)
        current = stats.memories

        if current == prev_count and current > 0:
            stable_ticks += 1
            if stable_ticks >= 2:
                logger.info('  Extraction stable at %d memories.', current)
                return
        else:
            stable_ticks = 0
        prev_count = current

    logger.warning('  Extraction poll timed out after %.0fs.', POLL_TIMEOUT)


async def _trigger_reflections(api: RemoteMemexAPI, vault_id: UUID) -> None:
    """Trigger reflection on top entities."""
    logger.info('  Triggering reflections...')
    entities = await api.get_top_entities(limit=5, vault_id=vault_id)

    for entity in entities:
        try:
            request = ReflectionRequest(entity_id=entity.id, vault_id=str(vault_id))
            result = await api.reflect(request)
            logger.info(
                '    Reflected on "%s": %d observations.',
                entity.name,
                len(result.new_observations),
            )
        except Exception as e:
            logger.warning('    Reflection failed for "%s": %s', entity.name, e)


async def _execute_check(
    api: RemoteMemexAPI,
    vault_id: UUID,
    group_name: str,
    check: GroundTruthCheck,
    judge: Judge | None,
) -> CheckResult:
    """Execute a single check by querying Memex and evaluating the result."""
    memory_results = None
    note_results = None
    entity_names: list[str] = []

    try:
        if check.check_type == 'entity_exists':
            # Search for entities by name
            entities = await api.search_entities(
                query=check.query, limit=check.top_k, vault_id=vault_id
            )
            entity_names = [e.name for e in entities]
        elif check.search_type == 'note':
            note_results = await api.search_notes(
                query=check.query,
                limit=check.top_k,
                vault_ids=[vault_id],
            )
        else:
            kwargs: dict = {
                'query': check.query,
                'limit': check.top_k,
                'vault_ids': [vault_id],
            }
            if check.strategies:
                kwargs['strategies'] = check.strategies
            if check.include_superseded is not None:
                kwargs['include_superseded'] = check.include_superseded
            memory_results, _ = await api.search(**kwargs)
    except Exception as e:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.ERROR,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Query error: {e}',
        )

    return run_check(
        check=check,
        group_name=group_name,
        memory_results=memory_results,
        note_results=note_results,
        entity_names=entity_names,
        judge=judge,
    )
