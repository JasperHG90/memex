"""LongMemEval Phase 0: Ingest sessions into Memex.

Loads one variant of the LongMemEval dataset, formats each session's chat
turns as a Markdown note with YAML frontmatter (so the ingest pipeline
preserves the session timestamp as ``publish_date``), and ingests one note
per session through ``RemoteMemexAPI.ingest``.

Each run writes to a dedicated vault named
``longmemeval_<variant>_<run_id>`` so benchmark data does not pollute the
user's active vault.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import CreateVaultRequest, NoteCreateDTO

from memex_eval.external.longmemeval_common import (
    LongMemEvalQuestion,
    LongMemEvalSession,
    _load_variant,
    vault_name_for,
)
from memex_eval.helpers import wait_for_extraction

logger = logging.getLogger('memex_eval.longmemeval_ingest')

POLL_INTERVAL = 3.0
POLL_TIMEOUT = 600.0


def _format_turn(role: str, content: str, timestamp: str) -> str:
    speaker = 'User' if role == 'user' else 'Assistant'
    return f'**{speaker}** ({timestamp}): {content}'


def _format_session(
    session: LongMemEvalSession,
    question_id: str,
    variant: str,
) -> str:
    """Render a session as Markdown with YAML frontmatter.

    The ``publish_date`` key in the frontmatter is parsed by the core
    ingestion service and propagates to ``MemoryUnit.event_date`` for every
    extracted fact — critical for the temporal-reasoning subset.
    """
    session_iso = session.session_date.isoformat()
    lines = [
        '---',
        f'publish_date: {session_iso}',
        f'session_id: {session.session_id}',
        f'question_id: {question_id}',
        f'variant: {variant}',
        '---',
        '',
        f'# LongMemEval Session {session.session_id}',
        f'**Date:** {session_iso}',
        f'**Question:** {question_id}',
        '',
        '## Dialogue',
        '',
    ]
    for turn in session.turns:
        lines.append(_format_turn(turn.role, turn.content, turn.timestamp.isoformat()))
        lines.append('')
    return '\n'.join(lines)


async def _setup_vault(api: RemoteMemexAPI, name: str, clean: bool = False):
    """Get or create a vault by name. Only cleans if explicitly requested."""
    vaults = await api.list_vaults()
    for vault in vaults:
        if vault.name == name:
            if clean:
                logger.info('Cleaning existing vault "%s"...', name)
                notes = await api.list_notes(vault_id=vault.id, limit=500)
                for note in notes:
                    await api.delete_note(note.id)
            else:
                logger.info('Found existing vault "%s" (id=%s)', name, vault.id)
            return vault.id

    logger.info('Creating vault "%s"...', name)
    vault = await api.create_vault(
        CreateVaultRequest(name=name, description=f'LongMemEval benchmark vault ({name}).')
    )
    return vault.id


async def _wait_for_extraction(api: RemoteMemexAPI, vault_id) -> None:
    await wait_for_extraction(
        api,
        vault_id,
        poll_interval=POLL_INTERVAL,
        poll_timeout=POLL_TIMEOUT,
        stable_ticks_required=3,
        max_consecutive_errors=0,
    )


def build_note_payloads(
    question: LongMemEvalQuestion,
    variant: str,
    vault_id: str,
) -> list[NoteCreateDTO]:
    """Build one ``NoteCreateDTO`` per session for a single question.

    Pure function — exposed for unit testing the session-to-note adapter
    without requiring a live Memex server.
    """
    payloads: list[NoteCreateDTO] = []
    for session in question.sessions:
        md = _format_session(session, question.question_id, variant)
        payload = NoteCreateDTO(
            name=f'{question.question_id} — {session.session_id}',
            description=(
                f'LongMemEval {variant} session {session.session_id} '
                f'for question {question.question_id} on {session.session_date.isoformat()}.'
            ),
            content=base64.b64encode(md.encode('utf-8')),
            tags=[
                'longmemeval',
                f'variant:{variant}',
                f'question:{question.question_id}',
                f'category:{question.category.value}',
            ],
            vault_id=str(vault_id),
            note_key=f'longmemeval-{variant}-{question.question_id}-{session.session_id}',
            author='longmemeval',
        )
        payloads.append(payload)
    return payloads


async def ingest_longmemeval(
    server_url: str,
    dataset_path: str,
    variant: str,
    run_id: str,
    question_limit: int | None = None,
    clean: bool = False,
    allow_unpinned_checksum: bool = False,
) -> int:
    """Ingest sessions for a LongMemEval variant into a dedicated Memex vault.

    Returns the number of sessions ingested (across all selected questions).
    """
    questions = _load_variant(Path(dataset_path), variant, allow_unpinned=allow_unpinned_checksum)
    if question_limit is not None:
        questions = questions[:question_limit]

    vault_name = vault_name_for(variant, run_id)
    ingested = 0

    async with httpx.AsyncClient(base_url=server_url, timeout=60.0) as client:
        api = RemoteMemexAPI(client)

        vault_id = await _setup_vault(api, vault_name, clean=clean)
        logger.info('Using vault "%s" (id=%s)', vault_name, vault_id)

        existing_notes = await api.list_notes(vault_id=vault_id, limit=1)
        if existing_notes and not clean:
            logger.info(
                'Vault "%s" already has notes. Skipping ingestion (use --clean to re-ingest).',
                vault_name,
            )
            return 0

        for i, question in enumerate(questions):
            payloads = build_note_payloads(question, variant, vault_id)
            logger.info(
                '[%d/%d] %s (%s): submitting %d session(s) as background batch',
                i + 1,
                len(questions),
                question.question_id,
                question.category.value,
                len(payloads),
            )
            job = await api.ingest_batch(payloads, vault_id=str(vault_id))
            logger.info(
                '[%d/%d] batch accepted (job_id=%s, status=%s)',
                i + 1,
                len(questions),
                job.job_id,
                job.status,
            )
            ingested += len(payloads)

        logger.info('All %d sessions submitted. Waiting for extraction to complete...', ingested)
        await _wait_for_extraction(api, vault_id)

    logger.info('Ingested %d sessions into vault "%s".', ingested, vault_name)
    return ingested
