from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from memex_core.memory.reflect.prompts import ReflectMemoryContext
from memex_core.memory.sql_models import MemoryUnit


def build_memory_context(
    memories: list[MemoryUnit],
    *,
    index_map: dict[UUID, int] | None = None,
) -> list[ReflectMemoryContext]:
    """Convert MemoryUnits to ReflectMemoryContext.

    Args:
        memories: List of MemoryUnit objects.
        index_map: Optional mapping from unit ID to display index.
            If None, uses sequential 0-based indexing.
    """
    contexts = []
    for i, unit in enumerate(memories):
        idx = index_map[unit.id] if index_map else i
        contexts.append(
            ReflectMemoryContext(
                index_id=idx,
                content=unit.formatted_fact_text,
                occurred=(unit.event_date or datetime.now(timezone.utc)).isoformat(),
            )
        )
    return contexts


def create_citation_map(uuids: list[str]) -> tuple[dict[str, int], dict[int, str]]:
    """
    Create a mapping between UUIDs and simple integer IDs for LLM consumption.
    Returns (uuid_str -> int_id, int_id -> uuid_str).

    Zero-indexed, dense sequential integers (0, 1, 2...).
    """
    uuid_to_int = {}
    int_to_uuid = {}
    current_idx = 0
    for u in uuids:
        u_str = str(u)
        if u_str not in uuid_to_int:
            uuid_to_int[u_str] = current_idx
            int_to_uuid[current_idx] = u_str
            current_idx += 1
    return uuid_to_int, int_to_uuid


def parse_timestamp(ts: Any) -> datetime:
    """
    Safely parse a timestamp into a UTC datetime object.
    Accepts string (ISO) or datetime objects.
    Defaults to datetime.now(timezone.utc) on failure.
    """
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    return datetime.now(timezone.utc)
