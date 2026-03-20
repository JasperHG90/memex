"""Shared helpers used across the eval framework."""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from memex_common.client import RemoteMemexAPI

logger = logging.getLogger('memex_eval.helpers')


async def wait_for_extraction(
    api: RemoteMemexAPI,
    vault_id: UUID,
    *,
    poll_interval: float = 2.0,
    poll_timeout: float = 120.0,
    stable_ticks_required: int = 2,
    max_consecutive_errors: int = 5,
) -> None:
    """Poll vault stats until extraction memory count stabilises.

    Args:
        api: Memex API client.
        vault_id: Vault to poll.
        poll_interval: Seconds between polls.
        poll_timeout: Maximum seconds to wait before giving up.
        stable_ticks_required: Number of consecutive unchanged polls before
            declaring stable.
        max_consecutive_errors: Number of consecutive poll errors before raising.
            Set to 0 to never raise on errors.
    """
    logger.info('  Waiting for extraction to complete...')
    prev_count = -1
    stable_ticks = 0
    start = time.monotonic()
    consecutive_errors = 0

    while time.monotonic() - start < poll_timeout:
        await asyncio.sleep(poll_interval)
        try:
            stats = await api.get_stats_counts(vault_id=vault_id)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            logger.warning('  Poll error (%d): %s', consecutive_errors, e)
            if max_consecutive_errors > 0 and consecutive_errors >= max_consecutive_errors:
                raise
            continue
        current = stats.memories

        if current == prev_count and current > 0:
            stable_ticks += 1
            if stable_ticks >= stable_ticks_required:
                logger.info('  Extraction stable at %d memories.', current)
                return
        else:
            stable_ticks = 0
        prev_count = current

    logger.warning('  Extraction poll timed out after %.0fs.', poll_timeout)
