"""Shared vault resolution utilities."""

from uuid import UUID

ALL_VAULTS_WILDCARD = '*'  # Pass "*" as a vault identifier to match all vaults.


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


def expand_vault_scope(
    named_ids: list[UUID],
    content_vault_ids: list[UUID],
    system_vault_ids: list[UUID],
    *,
    has_wildcard: bool,
    include_system_vaults: bool,
) -> list[UUID]:
    """Pure scope-expansion logic shared by the service and MCP resolvers.

    Contract — the single source of truth for read-scope expansion:
    - ``named_ids``: identifiers already resolved to UUIDs by the caller
      (via ``resolve_vault_identifier`` or its API equivalent).
    - ``content_vault_ids`` / ``system_vault_ids``: full membership lists
      fetched by the caller (the service hits the DB; MCP queries
      ``api.list_vaults(include_system=True)`` and partitions).
    - ``has_wildcard``: True if the caller's identifiers contained ``*``.
    - ``include_system_vaults``: per-call opt-in. **Unconditional union:**
      when True, every system vault is added regardless of whether the
      caller used ``*`` or named specific vaults.

    Returns a deduplicated, order-preserving list of UUIDs. The caller
    is responsible for raising ``VaultNotFoundError`` for unresolvable
    named identifiers — this helper does not validate membership.
    """
    ids: list[UUID] = []
    seen: set[UUID] = set()

    def _add(uid: UUID) -> None:
        if uid not in seen:
            seen.add(uid)
            ids.append(uid)

    for uid in named_ids:
        _add(uid)

    if has_wildcard or not named_ids:
        for uid in content_vault_ids:
            _add(uid)

    if include_system_vaults:
        for uid in system_vault_ids:
            _add(uid)

    return ids
