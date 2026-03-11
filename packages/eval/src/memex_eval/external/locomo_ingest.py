"""LoCoMo Phase 0: Ingest conversation sessions into Memex.

Loads the LoCoMo dataset, formats each conversation session as a Markdown note,
and ingests them into a dedicated vault via the Memex REST API.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import CreateVaultRequest, NoteCreateDTO

from memex_eval.external.locomo_common import VAULT_NAME, load_dataset

logger = logging.getLogger('memex_eval.locomo_ingest')

POLL_INTERVAL = 3.0
POLL_TIMEOUT = 300.0


def _format_session(
    session: list[dict],
    session_num: int,
    date_time: str,
    speaker_a: str,
    speaker_b: str,
    sample_id: str,
) -> str:
    """Format a single conversation session as Markdown."""
    lines = [
        f'# Conversation Session {session_num}',
        f'**Date:** {date_time}',
        f'**Participants:** {speaker_a}, {speaker_b}',
        f'**Conversation ID:** {sample_id}',
        '',
        '## Dialogue',
        '',
    ]
    for turn in session:
        speaker = turn.get('speaker', 'Unknown')
        text = turn.get('text', '')
        lines.append(f'**{speaker}:** {text}')
        lines.append('')
    return '\n'.join(lines)


async def ingest_locomo(
    server_url: str,
    dataset_path: str,
    conversation_index: int = 0,
    clean: bool = False,
) -> int:
    """Ingest LoCoMo conversation sessions into a Memex vault.

    Skips ingestion if the vault already has notes (unless clean=True).
    Returns the number of sessions ingested.
    """
    conversations = load_dataset(dataset_path)

    if conversation_index >= len(conversations):
        raise ValueError(
            f'Conversation index {conversation_index} out of range (0-{len(conversations) - 1})'
        )

    conv = conversations[conversation_index]
    conv_data = conv.get('conversation', {})
    sample_id = conv.get('sample_id', f'conv-{conversation_index}')
    speaker_a = conv_data.get('speaker_a', 'Speaker A')
    speaker_b = conv_data.get('speaker_b', 'Speaker B')

    # Extract sessions (session_1, session_2, ...)
    sessions: list[tuple[int, str, list[dict]]] = []
    for key in sorted(conv_data.keys()):
        if key.startswith('session_') and not key.endswith('_date_time'):
            num = int(key.split('_')[1])
            date_key = f'{key}_date_time'
            date_time = conv_data.get(date_key, 'Unknown date')
            session_data = conv_data[key]
            if isinstance(session_data, list) and session_data:
                sessions.append((num, date_time, session_data))

    logger.info(
        'Conversation %s: %d sessions, speakers: %s & %s',
        sample_id,
        len(sessions),
        speaker_a,
        speaker_b,
    )

    async with httpx.AsyncClient(base_url=server_url, timeout=180.0) as client:
        api = RemoteMemexAPI(client)

        vault_id = await _setup_vault(api, VAULT_NAME, clean=clean)
        logger.info('Using vault "%s" (id=%s)', VAULT_NAME, vault_id)

        # Skip if vault already has notes
        existing_notes = await api.list_notes(vault_id=vault_id, limit=1)
        if existing_notes and not clean:
            logger.info(
                'Vault "%s" already has notes. Skipping ingestion (use --clean to re-ingest).',
                VAULT_NAME,
            )
            return 0

        ingested = 0
        for session_num, date_time, session_data in sessions:
            md = _format_session(
                session_data,
                session_num,
                date_time,
                speaker_a,
                speaker_b,
                sample_id,
            )

            note = NoteCreateDTO(
                name=f'{sample_id} — Session {session_num}',
                description=(
                    f'Conversation session {session_num} between '
                    f'{speaker_a} and {speaker_b} on {date_time}.'
                ),
                content=base64.b64encode(md.encode('utf-8')),
                tags=['locomo', sample_id, f'session-{session_num}'],
                vault_id=str(vault_id),
                note_key=f'locomo-{sample_id}-s{session_num}',
            )

            logger.info(
                '  [%d/%d] Ingesting session %d (%s)',
                ingested + 1,
                len(sessions),
                session_num,
                date_time,
            )
            await api.ingest(note)
            ingested += 1

        # Wait for extraction to complete
        await _wait_for_extraction(api, vault_id)

    logger.info('Ingested %d sessions into vault "%s".', ingested, VAULT_NAME)
    return ingested


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
        CreateVaultRequest(name=name, description='LoCoMo benchmark vault.')
    )
    return vault.id


async def _wait_for_extraction(api, vault_id) -> None:
    """Poll stats until extraction stabilizes."""
    logger.info('  Waiting for extraction to complete...')
    prev_count = -1
    stable_ticks = 0
    start = time.monotonic()

    while time.monotonic() - start < POLL_TIMEOUT:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            stats = await api.get_stats_counts(vault_id=vault_id)
        except Exception as e:
            logger.warning('  Poll error: %s', e)
            continue
        current = stats.memories

        if current == prev_count and current > 0:
            stable_ticks += 1
            if stable_ticks >= 3:
                logger.info('  Extraction stable at %d memories.', current)
                return
        else:
            stable_ticks = 0
        prev_count = current

    logger.warning('  Extraction poll timed out after %.0fs.', POLL_TIMEOUT)
