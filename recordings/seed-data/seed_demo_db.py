#!/usr/bin/env python3
"""
Seed script for the Memex demo database.

Creates a "demo-recordings" vault and ingests demo notes with
cross-referencing concepts to produce entities with meaningful
co-occurrences. Idempotent: safe to run multiple times.

Usage:
    python seed_demo_db.py [http://localhost:8000/api/v1/]
"""

import asyncio
import base64
import logging
import sys
from pathlib import Path
from uuid import UUID

import httpx

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import (
    CreateVaultRequest,
    NoteCreateDTO,
    ReflectionRequest,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger(__name__)

VAULT_NAME = 'demo-recordings'
VAULT_DESCRIPTION = 'Demo vault for recording walkthroughs and documentation.'

DEMO_NOTES_DIR = Path(__file__).parent / 'demo_notes'

# Mapping of filename -> (display name, description, tags)
NOTE_MANIFEST: dict[str, tuple[str, str, list[str]]] = {
    'python-memory-management.md': (
        'Python Memory Management',
        'Deep dive into Python garbage collection, reference counting, '
        'memory pools, __slots__, and optimization strategies.',
        ['python', 'memory', 'garbage-collection', 'performance', 'optimization'],
    ),
    'distributed-systems.md': (
        'Distributed Systems Fundamentals',
        'Overview of CAP theorem, consensus protocols (Raft, Paxos), '
        'replication strategies, and distributed storage patterns.',
        ['distributed-systems', 'consensus', 'raft', 'postgresql', 'architecture'],
    ),
    'machine-learning-basics.md': (
        'Machine Learning Basics',
        'Introduction to supervised and unsupervised learning, neural networks, '
        'gradient descent, embeddings, and retrieval-augmented generation.',
        ['machine-learning', 'neural-networks', 'embeddings', 'python', 'retrieval'],
    ),
    'memex-architecture.md': (
        'Memex Architecture',
        'Architecture of the Memex long-term memory system: extraction, '
        'retrieval (TEMPR), reflection, and the storage layer.',
        ['memex', 'architecture', 'postgresql', 'memory', 'python', 'retrieval'],
    ),
    'duckdb-performance.md': (
        'DuckDB Performance and Architecture',
        'DuckDB columnar storage, vectorized execution, query optimization, '
        'Python integration, and comparison with PostgreSQL.',
        ['duckdb', 'analytics', 'columnar-storage', 'python', 'postgresql', 'performance'],
    ),
}


async def get_or_create_vault(api: RemoteMemexAPI) -> UUID:
    """Return the demo vault UUID, creating it if it does not exist."""
    vaults = await api.list_vaults()
    for vault in vaults:
        if vault.name == VAULT_NAME:
            logger.info(
                'Vault "%s" already exists (id=%s), skipping creation.', VAULT_NAME, vault.id
            )
            return vault.id

    logger.info('Creating vault "%s"...', VAULT_NAME)
    vault = await api.create_vault(
        CreateVaultRequest(name=VAULT_NAME, description=VAULT_DESCRIPTION)
    )
    logger.info('Created vault "%s" (id=%s).', vault.name, vault.id)
    return vault.id


async def ingest_notes(api: RemoteMemexAPI, vault_id: UUID) -> list[str]:
    """Ingest all demo notes into the vault. Returns list of note IDs."""
    note_ids: list[str] = []

    for filename, (name, description, tags) in NOTE_MANIFEST.items():
        filepath = DEMO_NOTES_DIR / filename
        if not filepath.exists():
            logger.warning('Demo note not found: %s', filepath)
            continue

        raw_content = filepath.read_bytes()
        b64_content = base64.b64encode(raw_content)

        note = NoteCreateDTO(
            name=name,
            description=description,
            content=b64_content,
            tags=tags,
            vault_id=str(vault_id),
            note_key=f'demo-{filepath.stem}',
        )

        logger.info('Ingesting "%s"...', name)
        response = await api.ingest(note)

        if hasattr(response, 'status'):
            if response.status == 'skipped':
                logger.info('  Skipped (reason: %s).', response.reason)
            else:
                logger.info('  Status: %s, note_id: %s', response.status, response.note_id)
                if response.note_id:
                    note_ids.append(response.note_id)
        else:
            # Background job response
            logger.info('  Queued as background job: %s', response.job_id)

    return note_ids


async def trigger_reflection(api: RemoteMemexAPI, vault_id: UUID) -> None:
    """Trigger reflection on the top entities in the vault."""
    logger.info('Fetching top entities for reflection...')
    entities = await api.get_top_entities(limit=10, vault_id=vault_id)

    if not entities:
        logger.info('No entities found yet; skipping reflection.')
        return

    logger.info('Triggering reflection on %d entities...', len(entities))
    for entity in entities:
        try:
            request = ReflectionRequest(
                entity_id=entity.id,
                vault_id=str(vault_id),
            )
            result = await api.reflect(request)
            logger.info(
                '  Reflected on "%s": %d new observations.',
                entity.name,
                len(result.new_observations),
            )
        except httpx.HTTPStatusError as exc:
            logger.warning(
                '  Reflection failed for "%s": %s',
                entity.name,
                exc.response.text[:200],
            )
        except Exception as exc:
            logger.warning('  Reflection failed for "%s": %s', entity.name, exc)


async def main(base_url: str = 'http://localhost:8000/api/v1/') -> None:
    """Run the seed script."""
    logger.info('Seeding Memex demo database at %s', base_url)

    async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
        api = RemoteMemexAPI(client)

        vault_id = await get_or_create_vault(api)
        note_ids = await ingest_notes(api, vault_id)

        logger.info('Ingested %d notes.', len(note_ids))

        if note_ids:
            # Brief pause to allow extraction to complete
            logger.info('Waiting for extraction to complete...')
            await asyncio.sleep(5)
            await trigger_reflection(api, vault_id)

    logger.info('Seed complete.')


if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:8000/api/v1/'
    asyncio.run(main(url))
