"""Shared vault resolution utilities."""

from uuid import UUID


def resolve_vault_list(
    vault_id: UUID | None = None,
    vault_ids: list[UUID | str] | None = None,
) -> list[UUID | str] | None:
    """Merge a single vault_id and a vault_ids list into one deduplicated list.

    Used by client methods that accept both ``vault_id`` (singular) and
    ``vault_ids`` (plural) parameters to produce a single list suitable
    for passing as a query-parameter value.

    Returns ``None`` when no vault identifiers are supplied, allowing
    callers to omit the parameter entirely.
    """
    ids: list[UUID | str] = list(vault_ids) if vault_ids else []
    if vault_id and vault_id not in ids:
        ids.append(vault_id)
    return ids or None
