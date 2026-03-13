"""Orchestrates the internal benchmark end-to-end."""

from __future__ import annotations

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

from memex_eval.helpers import wait_for_extraction
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

        vault_id = await _setup_vault(api, VAULT_NAME, 'Automated quality benchmark vault.')
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


async def _setup_vault(api: RemoteMemexAPI, name: str, description: str) -> UUID:
    """Create or clean a vault by name."""
    vaults = await api.list_vaults()
    for vault in vaults:
        if vault.name == name:
            logger.info('Cleaning existing vault "%s"...', name)
            notes = await api.list_notes(vault_id=vault.id, limit=500)
            for note in notes:
                await api.delete_note(note.id)
            return vault.id

    logger.info('Creating vault "%s"...', name)
    vault = await api.create_vault(CreateVaultRequest(name=name, description=description))
    return vault.id


async def _run_group(
    api: RemoteMemexAPI,
    vault_id: UUID,
    group: ScenarioGroup,
    judge: Judge | None,
) -> GroupResult:
    """Run all checks for a scenario group."""
    group_result = GroupResult(name=group.name, description=group.description)

    # Build vault map: vault_name -> vault_id (None = default vault)
    vault_map: dict[str | None, UUID] = {None: vault_id}
    extra_vault_names = {doc.vault_name for doc in group.docs if doc.vault_name}
    extra_vault_names |= {c.vault_name for c in group.checks if c.vault_name}
    for name in extra_vault_names:
        vault_map[name] = await _setup_vault(api, name, f'Benchmark vault: {name}')
        logger.info('  Extra vault "%s" (id=%s)', name, vault_map[name])

    # Phase 1: Ingest documents
    if group.docs:
        ingest_start = time.monotonic()
        await _ingest_docs(api, vault_map, group)
        group_result.ingest_duration_ms = (time.monotonic() - ingest_start) * 1000

    # Phase 2: Handle reflection group specially
    if group.name == 'reflection':
        reflection_start = time.monotonic()
        await _trigger_reflections(api, vault_id)
        group_result.reflection_duration_ms = (time.monotonic() - reflection_start) * 1000

    # Phase 3: Run checks
    for check in group.checks:
        check_vault_id = vault_map.get(check.vault_name, vault_id)
        check_result = await _execute_check(api, check_vault_id, group.name, check, judge)
        group_result.checks.append(check_result)

    return group_result


async def _ingest_docs(
    api: RemoteMemexAPI,
    vault_map: dict[str | None, UUID],
    group: ScenarioGroup,
) -> None:
    """Ingest scenario documents into their respective vaults."""
    default_vault_id = vault_map[None]
    vaults_with_docs: set[UUID] = set()

    for doc in group.docs:
        doc_vault_id = vault_map.get(doc.vault_name, default_vault_id)
        vaults_with_docs.add(doc_vault_id)

        note = NoteCreateDTO(
            name=doc.title,
            description=doc.description,
            content=doc.content_b64,
            files=doc.files_b64 if doc.files else {},
            tags=doc.tags,
            vault_id=str(doc_vault_id),
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
            await _wait_for_extraction(api, doc_vault_id)

    # Wait for all extraction to complete
    if not group.sequential_ingest:
        for vid in vaults_with_docs:
            await _wait_for_extraction(api, vid)


async def _wait_for_extraction(api: RemoteMemexAPI, vault_id: UUID) -> None:
    """Poll stats until extraction stabilizes."""
    await wait_for_extraction(
        api,
        vault_id,
        poll_interval=POLL_INTERVAL,
        poll_timeout=POLL_TIMEOUT,
        stable_ticks_required=2,
        max_consecutive_errors=5,
    )


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
    full_start = time.monotonic()

    memory_results = None
    note_results = None
    entity_names: list[str] = []

    entities_list = None
    cooccurrences = None
    mentions = None

    try:
        if check.check_type == 'entity_type_check':
            entities_list = await api.search_entities(
                query=check.query, limit=check.top_k, vault_id=vault_id
            )
        elif check.check_type == 'entity_cooccurrence_check':
            found = await api.search_entities(query=check.query, limit=1, vault_id=vault_id)
            if not found:
                return CheckResult(
                    name=check.name,
                    group=group_name,
                    status=CheckStatus.FAIL,
                    description=check.description,
                    query=check.query,
                    expected=check.expected,
                    actual='Entity not found',
                )
            cooccurrences = await api.get_entity_cooccurrences(found[0].id, vault_id=vault_id)
        elif check.check_type == 'entity_mention_check':
            found = await api.search_entities(query=check.query, limit=1, vault_id=vault_id)
            if not found:
                return CheckResult(
                    name=check.name,
                    group=group_name,
                    status=CheckStatus.FAIL,
                    description=check.description,
                    query=check.query,
                    expected=check.expected,
                    actual='Entity not found',
                )
            mentions = await api.get_entity_mentions(found[0].id, vault_id=vault_id)
        elif check.check_type == 'entity_exists':
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
            memory_results = await api.search(**kwargs)
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

    result = run_check(
        check=check,
        group_name=group_name,
        memory_results=memory_results,
        note_results=note_results,
        entity_names=entity_names,
        judge=judge,
        entities=entities_list,
        cooccurrences=cooccurrences,
        mentions=mentions,
    )

    # Override duration with full time (including API call)
    result.duration_ms = (time.monotonic() - full_start) * 1000

    # Timing assertion
    if (
        check.max_duration_ms is not None
        and result.status == CheckStatus.PASS
        and result.duration_ms > check.max_duration_ms
    ):
        result.status = CheckStatus.FAIL
        result.actual = (
            f'Exceeded time limit: {result.duration_ms:.0f}ms > '
            f'{check.max_duration_ms:.0f}ms. {result.actual}'
        )

    return result
