"""Document tracking and reflection queue enqueue pipeline stage.

Handles two post-extraction responsibilities:

1. **Document tracking** — updates the document tracking record in the
   metastore (via ``storage.handle_document_tracking``).
2. **Reflection queue enqueue** — notifies the reflection subsystem about
   entities that were touched during extraction so they can be reflected upon.
"""

from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.extraction import storage
from memex_core.memory.extraction.models import RetainContent
from memex_core.memory.reflect.queue_service import ReflectionQueueService

logger = logging.getLogger('memex.core.memory.extraction.pipeline.tracking')


async def track_document(
    session: AsyncSession,
    note_id: str,
    contents: list[RetainContent],
    is_first_batch: bool,
    vault_id: UUID = GLOBAL_VAULT_ID,
) -> None:
    """Update the document tracking record for a note.

    Assembles payload, tags, and assets from the first content item
    and delegates to ``storage.handle_document_tracking``.

    Args:
        session: Active DB session.
        note_id: Stable document identifier.
        contents: Content items being processed.
        is_first_batch: Whether this is the first extraction for this note.
        vault_id: Vault scope.
    """
    combined_content = '\n'.join([c.content for c in contents])
    retain_params: dict = {}
    tags: list[str] = []
    assets: list[str] = []

    if contents:
        first = contents[0]
        retain_params.update(first.payload)
        tags = cast(list[str], first.payload.get('tags', []))

        # Extract assets list if present in payload
        if 'assets' in first.payload and isinstance(first.payload['assets'], list):
            assets = first.payload['assets']

    publish_date = contents[0].event_date if contents else None
    description = retain_params.get('note_description') if retain_params else None

    await storage.handle_document_tracking(
        session,
        note_id,
        combined_content,
        is_first_batch,
        retain_params,
        tags,
        vault_id=vault_id,
        assets=assets,
        content_fingerprint=retain_params.get('content_fingerprint'),
        publish_date=publish_date,
        description=description,
    )


async def enqueue_for_reflection(
    session: AsyncSession,
    touched_entity_ids: set[UUID],
    vault_id: UUID,
    queue_service: ReflectionQueueService | None,
) -> None:
    """Enqueue touched entities for reflection.

    A no-op if ``queue_service`` is ``None`` or ``touched_entity_ids`` is empty.

    Args:
        session: Active DB session.
        touched_entity_ids: Entity IDs modified during extraction.
        vault_id: Vault scope.
        queue_service: The reflection queue service, or ``None`` if reflection
            is disabled.
    """
    if queue_service and touched_entity_ids:
        await queue_service.handle_extraction_event(session, touched_entity_ids, vault_id=vault_id)
