from datetime import timezone, datetime
import logging
import re
from typing import cast

import regex

from dateutil import parser as date_parser

from memex_core.memory.extraction.models import (
    DetectedHeader,
    PageIndexBlock,
    StructureQuality,
    TOCNode,
    content_hash_md5,
    estimate_token_count,
)

logger = logging.getLogger('memex_core.memory.extraction.utils')


def sanitize_text(text: str) -> str:
    """Remove surrogate characters from text."""
    if not text:
        return text
    return re.sub(r'[\ud800-\udfff]', '', text)


def parse_datetime(date_str: str) -> datetime | None:
    """Parse a datetime string into a datetime object."""
    try:
        dt = date_parser.isoparse(date_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def normalize_timestamp(dt_val: datetime | None) -> datetime:
    """Normalize datetime to UTC, treating None as min datetime."""
    if dt_val is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt_val.tzinfo is None:
        return dt_val.replace(tzinfo=timezone.utc)
    return dt_val


# ---------------------------------------------------------------------------
# Header detection (ported from prototype header_detection.py)
# ---------------------------------------------------------------------------

MIN_HEADERS_FOR_STRUCTURED = 2
MIN_COVERAGE_FOR_STRUCTURED = 0.5
MAX_GAP_RATIO_FOR_STRUCTURED = 0.4
MAX_AVG_SECTION_TOKENS = 4000


def detect_markdown_headers_regex(full_text: str) -> list[DetectedHeader]:
    """Detect markdown headers using regex pattern matching."""
    headers: list[DetectedHeader] = []
    pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

    for match in pattern.finditer(full_text):
        level_markers = match.group(1)
        title_text = match.group(2).strip()
        exact = match.group(0)
        start_idx = match.start()

        headers.append(
            DetectedHeader(
                reasoning=f"Regex match: '{level_markers}' markdown header syntax.",
                exact_text=exact,
                clean_title=title_text,
                level_hint=f'h{len(level_markers)}',
                id=len(headers),
                start_index=start_idx,
                verified=True,
            )
        )

    return headers


def assess_structure_quality(headers: list[DetectedHeader], doc_length: int) -> StructureQuality:
    """Assess how well-structured a document is based on detected headers."""
    if not headers or doc_length == 0:
        return StructureQuality(
            is_well_structured=False,
            header_count=0,
            has_hierarchy=False,
            coverage_ratio=0.0,
            avg_section_tokens=0.0,
            max_gap_chars=doc_length,
            reason='No markdown headers detected.',
        )

    header_count = len(headers)
    levels = {h.level_hint for h in headers}
    has_hierarchy = len(levels) > 1

    sorted_headers = sorted(headers, key=lambda h: h.start_index or 0)
    first_start = sorted_headers[0].start_index or 0
    covered_span = doc_length - first_start
    coverage_ratio = covered_span / doc_length if doc_length > 0 else 0.0

    max_gap = first_start
    section_lengths: list[int] = []
    for i, h in enumerate(sorted_headers):
        h_start = h.start_index or 0
        if i + 1 < len(sorted_headers):
            next_start = sorted_headers[i + 1].start_index or 0
            section_len = next_start - h_start
        else:
            section_len = doc_length - h_start
        section_lengths.append(section_len)
        if section_len > max_gap:
            max_gap = section_len

    avg_section_chars = sum(section_lengths) / max(len(section_lengths), 1)
    avg_section_tokens = float(estimate_token_count('x' * int(avg_section_chars)))

    max_gap_ratio = max_gap / doc_length if doc_length > 0 else 0.0

    reasons: list[str] = []
    is_good = True

    if header_count < MIN_HEADERS_FOR_STRUCTURED:
        is_good = False
        reasons.append(f'Too few headers ({header_count} < {MIN_HEADERS_FOR_STRUCTURED}).')

    if coverage_ratio < MIN_COVERAGE_FOR_STRUCTURED:
        is_good = False
        reasons.append(f'Low coverage ({coverage_ratio:.0%} < {MIN_COVERAGE_FOR_STRUCTURED:.0%}).')

    if max_gap_ratio > MAX_GAP_RATIO_FOR_STRUCTURED:
        is_good = False
        reasons.append(f'Large gap ({max_gap} chars, {max_gap_ratio:.0%} of doc).')

    if avg_section_tokens > MAX_AVG_SECTION_TOKENS:
        is_good = False
        reasons.append(
            f'Sections too large on average ({avg_section_tokens:.0f} tokens '
            f'> {MAX_AVG_SECTION_TOKENS}).'
        )

    if is_good:
        reason = (
            f'Well-structured: {header_count} headers, '
            f'{len(levels)} levels, {coverage_ratio:.0%} coverage.'
        )
    else:
        reason = ' '.join(reasons)

    return StructureQuality(
        is_well_structured=is_good,
        header_count=header_count,
        has_hierarchy=has_hierarchy,
        coverage_ratio=coverage_ratio,
        avg_section_tokens=avg_section_tokens,
        max_gap_chars=max_gap,
        reason=reason,
    )


def verify_headers(headers: list[DetectedHeader], full_text: str) -> list[DetectedHeader]:
    """Verify LLM-detected headers against the source text using fuzzy matching."""
    for h in headers:
        if h.verified:
            continue
        if h.start_index is None:
            h.verified = False
            continue

        end = h.start_index + len(h.exact_text)
        if end <= len(full_text) and full_text[h.start_index : end] == h.exact_text:
            h.verified = True
            continue

        window_start = max(0, h.start_index - 20)
        window_end = min(len(full_text), h.start_index + len(h.exact_text) + 20)
        window = full_text[window_start:window_end]

        try:
            tolerance = max(2, int(len(h.exact_text) * 0.1))
            pattern = f'({regex.escape(h.exact_text)}){{e<={tolerance}}}'
            match = regex.search(pattern, window)
            if match:
                h.start_index = window_start + match.start()
                h.exact_text = match.group(0)
                h.verified = True
            else:
                h.verified = False
        except Exception:
            h.verified = False

    return headers


def merge_header_lists(
    llm_headers: list[DetectedHeader], regex_headers: list[DetectedHeader]
) -> list[DetectedHeader]:
    """Merge LLM-detected headers with regex-detected headers, deduplicating by position."""
    combined = list(llm_headers)
    existing_positions = {h.start_index for h in llm_headers if h.start_index is not None}

    for rh in regex_headers:
        if rh.start_index is None:
            continue
        is_dup = any(abs(rh.start_index - pos) < 10 for pos in existing_positions)
        if not is_dup:
            combined.append(rh)
            existing_positions.add(rh.start_index)

    combined.sort(key=lambda h: h.start_index or 0)
    for i, h in enumerate(combined):
        h.id = i
    return combined


def deduplicate_and_sort(results: list[list[DetectedHeader]]) -> list[DetectedHeader]:
    """Flatten, deduplicate, and sort header results from parallel scanning."""
    all_headers: list[DetectedHeader] = []
    seen_indices: set[int] = set()
    flat_results = [item for sublist in results for item in sublist]
    flat_results.sort(key=lambda h: cast(int, h.start_index))

    for header in flat_results:
        idx = header.start_index or 0
        is_dup = any(abs(seen - idx) < 10 for seen in seen_indices)

        if not is_dup:
            header.id = len(all_headers)
            all_headers.append(header)
            seen_indices.add(idx)

    for i, h in enumerate(all_headers):
        h.id = i

    return all_headers


# ---------------------------------------------------------------------------
# Tree builder (ported from prototype tree_builder.py)
# ---------------------------------------------------------------------------


def build_tree_from_regex_headers(headers: list[DetectedHeader]) -> list[TOCNode]:
    """Build a hierarchical tree from regex-detected headers using a stack-based approach."""

    def header_level(h: DetectedHeader) -> int:
        try:
            return int(h.level_hint[1:])
        except (ValueError, IndexError):
            return 1

    nodes = [
        TOCNode(
            original_header_id=cast(int, h.id),
            title=h.clean_title,
            level=header_level(h),
            reasoning=f'Regex: {h.level_hint} header.',
        )
        for h in headers
    ]

    root_nodes: list[TOCNode] = []
    stack: list[TOCNode] = []

    for node in nodes:
        while stack and stack[-1].level >= node.level:
            stack.pop()

        if stack:
            stack[-1].children.append(node)
        else:
            root_nodes.append(node)

        stack.append(node)

    return root_nodes


def hydrate_tree(
    nodes: list[TOCNode],
    flat_headers: list[DetectedHeader],
    full_text: str,
    parent_end_index: int | None = None,
) -> None:
    """Populate TOCNode content and boundaries from flat headers and source text."""
    parent_end_index = parent_end_index or len(full_text)

    for i, node in enumerate(nodes):
        if node.original_header_id >= len(flat_headers):
            logger.warning(
                f"Skipping node '{node.title}': header ID {node.original_header_id} out of range."
            )
            continue

        header_data = flat_headers[node.original_header_id]
        node.start_index = header_data.start_index

        if i + 1 < len(nodes):
            next_sibling_id = nodes[i + 1].original_header_id
            if next_sibling_id < len(flat_headers):
                node.end_index = flat_headers[next_sibling_id].start_index
            else:
                node.end_index = parent_end_index
        else:
            node.end_index = parent_end_index

        if (
            node.end_index is not None
            and node.start_index is not None
            and node.end_index < node.start_index
        ):
            node.end_index = len(full_text)

        content_end_limit = node.end_index

        if node.children:
            first_child_id = node.children[0].original_header_id
            if first_child_id < len(flat_headers):
                content_end_limit = flat_headers[first_child_id].start_index

        if node.start_index is not None:
            node.content = full_text[node.start_index : content_end_limit]
            node.token_estimate = estimate_token_count(node.content)

        if node.children:
            hydrate_tree(node.children, flat_headers, full_text, parent_end_index=node.end_index)


def filter_valid_nodes(nodes: list[TOCNode], max_id: int) -> list[TOCNode]:
    """Filter out nodes with invalid header IDs."""
    valid: list[TOCNode] = []
    for node in nodes:
        if 0 <= node.original_header_id <= max_id:
            node.children = filter_valid_nodes(node.children, max_id)
            valid.append(node)
        else:
            logger.warning(
                f"Dropping node '{node.title}' with invalid header ID "
                f'{node.original_header_id} (max: {max_id})'
            )
    return valid


def compute_coverage(nodes: list[TOCNode], doc_length: int) -> float:
    """Compute the fraction of a document covered by detected sections."""
    if doc_length == 0:
        return 1.0

    spans: list[tuple[int, int]] = []
    for n in nodes:
        if n.start_index is not None and n.end_index is not None:
            spans.append((n.start_index, n.end_index))

    if not spans:
        return 0.0

    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    covered = sum(end - start for start, end in merged)
    return min(1.0, covered / doc_length)


def strip_header_from_content(content: str, title: str) -> str:
    """Strip the markdown header line from the beginning of section content."""
    if not content:
        return content

    pattern = re.compile(r'^\s*#{1,6}\s+' + re.escape(title) + r'\s*\n*', re.MULTILINE)
    stripped = pattern.sub('', content, count=1)
    return stripped.lstrip('\n')


def generate_blocks_and_assign_ids(
    toc_tree: list[TOCNode],
    block_size: int,
) -> tuple[list[PageIndexBlock], dict[str, str]]:
    """Generate blocks from the TOC tree by merging nodes up to a token budget.

    Returns the block list and a mapping of node IDs to block IDs.
    """
    atomic_units: list[dict[str, TOCNode | int]] = []

    def traverse(node: TOCNode, depth: int) -> None:
        text_len = len(node.content) if node.content else 0
        if text_len > 0:
            atomic_units.append(
                {
                    'node_ref': node,
                    'tokens': node.token_estimate or 0,
                    'depth': depth,
                }
            )
        for child in node.children:
            traverse(child, depth + 1)

    for root in toc_tree:
        traverse(root, 1)

    blocks_list: list[PageIndexBlock] = []
    node_to_block_map: dict[str, str] = {}

    current_block_content: list[str] = []
    current_block_titles: list[str] = []
    current_block_tokens = 0
    current_block_start = -1
    current_block_node_ids: list[str] = []

    block_counter = 0

    def flush_block(end_index: int) -> None:
        nonlocal current_block_content, current_block_tokens, current_block_titles
        nonlocal current_block_start, current_block_node_ids, block_counter

        if not current_block_content:
            return

        full_text = '\n\n'.join(current_block_content)
        block_hash = content_hash_md5(full_text)

        new_block = PageIndexBlock(
            id=block_hash,
            seq=block_counter,
            content=full_text,
            token_count=current_block_tokens,
            titles_included=current_block_titles[:],
            start_index=current_block_start,
            end_index=end_index,
        )
        blocks_list.append(new_block)

        for nid in current_block_node_ids:
            node_to_block_map[nid] = block_hash

        block_counter += 1
        current_block_content = []
        current_block_tokens = 0
        current_block_titles = []
        current_block_start = -1
        current_block_node_ids = []

    for unit in atomic_units:
        node = cast(TOCNode, unit['node_ref'])
        node_tokens = cast(int, unit['tokens'])
        depth = cast(int, unit['depth'])

        if current_block_start == -1:
            current_block_start = cast(int, node.start_index)

        if current_block_tokens > 0 and current_block_tokens + node_tokens > block_size:
            flush_block(end_index=cast(int, node.start_index))
            current_block_start = cast(int, node.start_index)

        current_block_node_ids.append(node.id)

        body = strip_header_from_content(node.content or '', node.title)
        header_marker = '#' * depth
        formatted_text = (
            f'{header_marker} {node.title}\n{body}' if body else f'{header_marker} {node.title}'
        )

        unit_tokens = estimate_token_count(formatted_text)
        current_block_content.append(formatted_text)
        current_block_tokens += unit_tokens
        current_block_titles.append(node.title)

    if atomic_units:
        last_node = cast(TOCNode, atomic_units[-1]['node_ref'])
        final_end = cast(int, last_node.start_index) + len(last_node.content or '')
        flush_block(end_index=final_end)

    # Back-fill node.block_id from the map
    def backfill(nodes: list[TOCNode]) -> None:
        for n in nodes:
            if n.id in node_to_block_map:
                n.block_id = node_to_block_map[n.id]
            backfill(n.children)

    backfill(toc_tree)

    return blocks_list, node_to_block_map


def collect_node_summaries_for_block(
    block_id: str,
    toc_tree: list[TOCNode],
    node_to_block_map: dict[str, str],
) -> list[tuple[str, str]]:
    """Collect formatted summaries for all nodes that belong to a given block."""
    results: list[tuple[str, str]] = []

    def walk(nodes: list[TOCNode]) -> None:
        for n in nodes:
            if node_to_block_map.get(n.id) == block_id and n.summary:
                results.append((n.title, n.summary.formatted))
            walk(n.children)

    walk(toc_tree)
    return results
