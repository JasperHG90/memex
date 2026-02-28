"""Block and node diffing for incremental extraction.

Computes diffs between new content blocks and existing blocks stored in
the database. Two modes:

1. **Simple block diff** (`diff_blocks`): hash-based diffing for
   ``SimpleTextSplitting`` — classifies blocks as retained, added, or removed.
2. **Page-index block diff** (`diff_page_index_blocks`): enhanced diffing for
   ``PageIndexTextSplitting`` — adds *boundary-shift* vs *content-changed*
   classification by inspecting node-level hashes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from memex_core.memory.extraction.models import (
    PageIndexOutput,
    StableBlock,
    TOCNode,
    content_hash_md5,
)
from memex_core.memory.sql_models import ContentStatus

logger = logging.getLogger('memex.core.memory.extraction.pipeline.diffing')


@dataclass
class BlockDiffResult:
    """Result of diffing new blocks against existing blocks."""

    retained_hashes: set[str]
    """Block hashes present in both old and new versions."""

    added_blocks: list[StableBlock]
    """New blocks not present in existing set (simple diff only)."""

    removed_hashes: set[str]
    """Hashes present in old version but absent from new version."""


@dataclass
class PageIndexDiffResult:
    """Result of diffing page-index blocks with node-level classification."""

    retained_hashes: set[str]
    """Block hashes present in both old and new versions."""

    boundary_shift_hashes: set[str]
    """New block hashes whose constituent nodes all existed before
    (block boundaries moved but content unchanged)."""

    content_changed_hashes: set[str]
    """New block hashes containing new or changed nodes."""

    removed_hashes: set[str]
    """Hashes present in old version but absent from new version."""

    block_node_hashes: dict[str, set[str]]
    """Mapping from block hash to constituent node content hashes.
    Used downstream for fact migration and stale-node detection."""


def diff_blocks(
    new_blocks: list[StableBlock],
    existing_blocks: list[dict[str, object]],
) -> BlockDiffResult:
    """Compute a hash-based block diff for simple text splitting.

    Args:
        new_blocks: Blocks produced by ``stable_chunk_text`` on the new content.
        existing_blocks: Rows from ``storage.get_note_blocks`` (each has
            a ``content_hash`` key).

    Returns:
        A ``BlockDiffResult`` classifying blocks as retained, added, or removed.
    """
    existing_hash_map: dict[str, dict[str, object]] = {}
    for eb in existing_blocks:
        h = str(eb['content_hash'])
        existing_hash_map[h] = eb

    new_hash_set = {b.content_hash for b in new_blocks}
    existing_hash_set = set(existing_hash_map.keys())

    retained_hashes = new_hash_set & existing_hash_set
    added_blocks = [b for b in new_blocks if b.content_hash not in existing_hash_set]
    removed_hashes = existing_hash_set - new_hash_set

    return BlockDiffResult(
        retained_hashes=retained_hashes,
        added_blocks=added_blocks,
        removed_hashes=removed_hashes,
    )


def _walk_nodes(
    nodes: list[TOCNode],
    node_to_block_map: dict[str, str],
    block_node_hashes: dict[str, set[str]],
) -> None:
    """Recursively collect node content hashes grouped by their parent block."""
    for n in nodes:
        if n.content_hash:
            block_hash = node_to_block_map.get(n.id)
            if block_hash:
                block_node_hashes[block_hash].add(n.content_hash)
        _walk_nodes(n.children, node_to_block_map, block_node_hashes)


def diff_page_index_blocks(
    page_index_output: PageIndexOutput,
    existing_blocks: list[dict[str, object]],
    prev_node_hashes: set[str],
) -> PageIndexDiffResult:
    """Compute a node-aware block diff for page-index text splitting.

    Blocks are first classified by hash (retained / new / removed).  New
    blocks are then sub-classified:

    * **boundary_shift** — the block hash is new but *all* constituent
      TOC-node hashes existed in the previous version.  This means the
      block boundaries moved without any content change.
    * **content_changed** — the block contains at least one node whose
      content hash is new.

    Args:
        page_index_output: The ``PageIndexOutput`` from ``index_document``.
        existing_blocks: Rows from ``storage.get_note_blocks``.
        prev_node_hashes: Set of ``node_hash`` values from
            ``storage.get_note_nodes`` for the previous version.

    Returns:
        A ``PageIndexDiffResult`` with full classification and the
        ``block_node_hashes`` map needed downstream.
    """
    existing_hash_map: dict[str, dict[str, object]] = {
        str(b['content_hash']): b for b in existing_blocks
    }
    existing_hash_set = set(existing_hash_map.keys())
    new_hash_set = {b.id for b in page_index_output.blocks}

    retained_hashes = new_hash_set & existing_hash_set
    new_block_hashes = new_hash_set - existing_hash_set
    removed_hashes = existing_hash_set - new_hash_set

    # Build block_hash -> constituent node hashes from the TOC tree
    block_node_hashes: dict[str, set[str]] = defaultdict(set)
    _walk_nodes(
        page_index_output.toc,
        page_index_output.node_to_block_map,
        block_node_hashes,
    )

    # Sub-classify new blocks
    boundary_shift_hashes: set[str] = set()
    content_changed_hashes: set[str] = set()
    for block in page_index_output.blocks:
        if block.id not in new_block_hashes:
            continue  # retained
        node_hashes = block_node_hashes.get(block.id, set())
        if node_hashes and node_hashes.issubset(prev_node_hashes):
            boundary_shift_hashes.add(block.id)
        else:
            content_changed_hashes.add(block.id)

    return PageIndexDiffResult(
        retained_hashes=retained_hashes,
        boundary_shift_hashes=boundary_shift_hashes,
        content_changed_hashes=content_changed_hashes,
        removed_hashes=removed_hashes,
        block_node_hashes=block_node_hashes,
    )


# ---------------------------------------------------------------------------
# Thin-tree construction helpers
# ---------------------------------------------------------------------------


def collect_toc_hashes(toc: list[TOCNode]) -> dict[str, str]:
    """Build a mapping from TOC node ID to its content hash.

    For each node, uses ``content_hash`` if available, otherwise
    computes ``content_hash_md5`` from ``content`` (or ``title``
    as fallback).

    Args:
        toc: The root-level TOC nodes from a ``PageIndexOutput``.

    Returns:
        Dict mapping node ID to its content hash string.
    """
    result: dict[str, str] = {}

    def _collect(node: TOCNode) -> None:
        h = node.content_hash or content_hash_md5(node.content or node.title)
        result[node.id] = h
        for child in node.children:
            _collect(child)

    for n in toc:
        _collect(n)
    return result


def replace_tree_ids(
    tree_dict: dict[str, Any],
    id_map: dict[str, str],
) -> dict[str, Any]:
    """Recursively replace ``id`` fields in a thin-tree dict using *id_map*.

    Args:
        tree_dict: A dict produced by ``TOCNode.tree_without_text()``.
        id_map: Mapping from old IDs to new IDs (typically content hashes).

    Returns:
        The same dict, mutated in-place, with ``id`` fields replaced.
    """
    old_id = tree_dict.get('id', '')
    tree_dict['id'] = id_map.get(old_id, old_id)
    tree_dict['children'] = [replace_tree_ids(c, id_map) for c in tree_dict.get('children', [])]
    return tree_dict


def build_thin_tree(
    toc: list[TOCNode],
    min_node_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Build a hash-stable thin tree from TOC nodes.

    Combines ``collect_toc_hashes`` and ``replace_tree_ids`` to produce
    a list of thin-tree dicts with ``id`` fields replaced by content
    hashes. This is the format stored by
    ``storage.update_note_page_index``.

    Args:
        toc: Root-level TOC nodes from a ``PageIndexOutput``.
        min_node_tokens: Minimum token count for a node to be included.

    Returns:
        List of thin-tree dicts ready for storage.
    """
    id_map = collect_toc_hashes(toc)
    return [
        replace_tree_ids(n.tree_without_text(min_node_tokens=min_node_tokens), id_map)
        for n in toc
        if min_node_tokens <= 0 or (n.token_estimate or 0) > min_node_tokens
    ]


# ---------------------------------------------------------------------------
# TOC tree flattening and lookup helpers
# ---------------------------------------------------------------------------


def flatten_toc_to_node_rows(
    toc: list[TOCNode],
    page_index_output: PageIndexOutput,
    vault_id: UUID,
    note_id: UUID,
    min_node_tokens: int = 0,
) -> list[dict[str, object]]:
    """Flatten a TOC tree into a list of node row dicts for DB insertion.

    Walks the tree depth-first, skipping nodes below *min_node_tokens*
    (promoting their children).  Deduplicates by ``node_hash`` to avoid
    PostgreSQL ``CardinalityViolationError`` on batch upsert.

    Args:
        toc: Root-level TOC nodes.
        page_index_output: PageIndexOutput (for ``node_to_block_map``).
        vault_id: Vault scope UUID.
        note_id: Document UUID.
        min_node_tokens: Skip nodes with fewer tokens than this.

    Returns:
        Deduplicated list of node row dicts ready for
        ``storage.insert_nodes_batch``.
    """
    node_rows: list[dict[str, object]] = []
    seq_counter = 0

    def _flatten(nodes: list[TOCNode], parent_block_id: str | None = None) -> None:
        nonlocal seq_counter
        for node in nodes:
            if min_node_tokens > 0 and (node.token_estimate or 0) <= min_node_tokens:
                if node.children:
                    _flatten(node.children, parent_block_id)
                continue
            block_id_str = page_index_output.node_to_block_map.get(node.id)
            node_hash = node.content_hash or content_hash_md5(node.content or node.title)

            summary_dict = None
            summary_fmt = None
            if node.summary:
                summary_dict = node.summary.model_dump()
                summary_fmt = node.summary.formatted

            node_rows.append(
                {
                    'vault_id': vault_id,
                    'note_id': note_id,
                    'node_hash': node_hash,
                    'title': node.title,
                    'text': node.content or '',
                    'summary': summary_dict,
                    'summary_formatted': summary_fmt,
                    'level': node.level,
                    'seq': seq_counter,
                    'token_estimate': node.token_estimate or 0,
                    'status': ContentStatus.ACTIVE,
                }
            )
            seq_counter += 1

            if node.children:
                _flatten(node.children, block_id_str)

    _flatten(toc)

    # Deduplicate by node_hash
    seen_hashes: set[str] = set()
    deduped: list[dict[str, object]] = []
    for row in node_rows:
        h = str(row['node_hash'])
        if h not in seen_hashes:
            seen_hashes.add(h)
            deduped.append(row)
    return deduped


def find_node_hash(toc: list[TOCNode], target_id: str) -> str | None:
    """Recursively find a TOC node by ID and return its content hash.

    Args:
        toc: Root-level TOC nodes to search.
        target_id: The node ID to look up.

    Returns:
        The content hash of the matching node, or ``None`` if not found.
    """
    for node in toc:
        if node.id == target_id:
            return node.content_hash or content_hash_md5(node.content or node.title)
        result = find_node_hash(node.children, target_id)
        if result is not None:
            return result
    return None
