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

from memex_core.memory.extraction.models import (
    PageIndexOutput,
    StableBlock,
    TOCNode,
)

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
