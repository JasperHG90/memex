import datetime as dt
import hashlib
import logging
import re
import asyncio
from typing import cast

import dspy
import regex as regex_lib
from langchain_text_splitters import RecursiveCharacterTextSplitter


from memex_core.memory.extraction.models import (
    BlockSummary,
    DetectedHeader,
    ExtractedOutput,
    PageIndexBlock,
    PageIndexOutput,
    RawFact,
    CausalRelation,
    StableBlock,
    StructureQuality,
    ProtectedZone,
    TOCNode,
    content_hash_md5,
    estimate_token_count,
)
from memex_core.memory.extraction.signatures import (
    ScanChunk,
    OrganizeStructure,
    SummarizeSection,
    SummarizeParentSection,
    SummarizeBlock,
)
from memex_core.memory.extraction.utils import (
    assess_structure_quality,
    build_tree_from_regex_headers,
    collect_node_summaries_for_block,
    compute_coverage,
    deduplicate_and_sort,
    detect_markdown_headers_regex,
    filter_valid_nodes,
    generate_blocks_and_assign_ids,
    hydrate_tree,
    merge_header_lists,
    sanitize_text,
    verify_headers,
)
from memex_core.memory.extraction.exceptions import ExtractionError, OutputTooLongException
from memex_core.llm import run_dspy_operation

BLOCK_HARD_LIMIT = 50_000  # chars — safety cap for pathological input

logger = logging.getLogger('memex.core.memory.extraction.core')


class ExtractSemanticFacts(dspy.Signature):
    """
    Extract semantic facts from text chunks.
    LANGUAGE RULE: Output extracted facts in ENGLISH. Translate if necessary.
    TEMPORAL HANDLING: Use 'event_date_ref' as the anchor for relative dates.
    """

    chunk_text: str = dspy.InputField(
        description='The specific text segment to analyze',
    )
    context: str = dspy.InputField(description='Surrounding context of the conversation')
    event_date_ref: str = dspy.InputField(
        description='Reference date',
        examples=['Monday, June 5th, 2023', 'Saturday, December 25th, 2021'],
    )
    memory_context: str = dspy.InputField(description='Agent identity and background information')
    special_instructions: str = dspy.InputField(
        description='Specific rules on what types of facts to include/exclude'
    )
    extracted_facts: ExtractedOutput = dspy.OutputField(
        description='The structured list of extracted facts'
    )


class ExtractFrontmatterMetadata(dspy.Signature):
    """Extract structured metadata from YAML frontmatter of a document.

    This is YAML frontmatter from a document header. Extract:
    1. Author/creator as a Person entity (from fields like created_by, author, creator)
    2. Any dates found (created_date, publish_date, date, etc.)
    3. Any other structured metadata as factual statements

    LANGUAGE RULE: Output extracted facts in ENGLISH.
    """

    frontmatter_text: str = dspy.InputField(
        description='The YAML frontmatter block from a document header'
    )
    event_date_ref: str = dspy.InputField(description='Reference date for temporal context')
    extracted_facts: ExtractedOutput = dspy.OutputField(
        description='Structured list of extracted facts from the frontmatter'
    )


async def extract_facts_from_frontmatter(
    frontmatter_text: str,
    event_date: dt.datetime,
    lm: dspy.LM,
    semaphore: asyncio.Semaphore | None = None,
) -> list[RawFact]:
    """Extract structured metadata facts from YAML frontmatter using LLM."""
    predictor = dspy.ChainOfThought(ExtractFrontmatterMetadata)
    try:
        result = await run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={
                'frontmatter_text': sanitize_text(frontmatter_text),
                'event_date_ref': event_date.strftime('%A, %B %d, %Y'),
            },
            semaphore=semaphore,
            operation_name='extraction.frontmatter',
        )
        return result.extracted_facts.extracted_facts
    except Exception as e:
        logger.error(f'Frontmatter extraction failed: {e}')
        return []


async def _extract_facts_from_chunk(
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    event_date: dt.datetime,
    context: str,
    lm: dspy.LM,
    predictor: dspy.Predict,
    agent_name: str | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> list[RawFact]:
    """Extracts facts from a single chunk using DSPy."""

    rules = (
        "1. Extract 'world' and 'event' facts.\n"
        '2. CLASSIFICATION RULE: Classify facts describing "what something is", "how it works", "system states", or "outcomes" as WORLD. '
        'Classify facts describing "what happened", "narrative events", or "specific interactions" as EVENT. '
        'IMPORTANT: If a fact defines a state (e.g., "The system is push-based"), classify as WORLD even if described with past-tense verbs like "established" or "implemented".\n'
        '3. Consolidate related statements into single facts.\n'
        "4. Resolve coreferences (e.g. 'he' -> 'John').\n"
        '5. LANGUAGE: All output MUST be in English. Translate if source is not English.\n'
        '6. CAUSAL RELATIONS: Check for causal links between facts.\n'
        "   - If Fact B happens BECAUSE OF Fact A, add a 'causal_relation' to Fact B.\n"
        "   - 'target_index' must point to a PREVIOUS fact (target_index < current_index).\n"
        "   - Types: 'caused_by', 'enabled_by', 'prevented_by'."
    )

    try:
        result = await run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={
                'chunk_text': sanitize_text(chunk),
                'context': sanitize_text(context) if context else 'None',
                'event_date_ref': event_date.strftime('%A, %B %d, %Y'),
                'memory_context': f'Your name: {agent_name}' if agent_name else '',
                'special_instructions': rules,
            },
            semaphore=semaphore,
            operation_name='extraction.facts',
        )

        return result.extracted_facts.extracted_facts

    except OutputTooLongException:
        raise
    except (ValueError, RuntimeError, OSError, KeyError) as e:
        error_msg = str(e).lower()
        if (
            'context_length_exceeded' in error_msg
            or 'too long' in error_msg
            or 'maximum context' in error_msg
        ):
            logger.error(f'DSPy Context Limit Hit: {e}')
            raise OutputTooLongException() from e

        logger.error(f'DSPy Extraction Failed for chunk {chunk_index}: {e}')
        raise ExtractionError(f'Extraction failed for chunk {chunk_index}: {e}') from e


def content_hash(text: str) -> str:
    """SHA-256 of whitespace-normalized text.

    Resilient to trailing whitespace and minor formatting changes.
    Detects all substantive content changes.
    """
    normalized = re.sub(r'[ \t]+', ' ', text.strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


# Markdown-aware chunking patterns (compiled at module load)
FENCED_CODE_PATTERN = re.compile(r'^(?P<fence>`{3,}|~{3,})(?P<lang>\w*)?\s*$', re.MULTILINE)
FRONTMATTER_PATTERN = re.compile(r'\A---\s*\n(?P<yaml>.*?)\n---\s*\n', re.DOTALL)
LIST_ITEM_PATTERN = re.compile(r'^(?P<indent>\s*)(?P<marker>[-*+]|\d+\.)\s+', re.MULTILINE)
SENTENCE_END_PATTERN = re.compile(r'[.!?](?=\s+[A-Z]|\s*\n|\s*$)')

ABBREVIATIONS: set[str] = {
    'Dr',
    'Mr',
    'Mrs',
    'Ms',
    'Jr',
    'Sr',
    'Prof',
    'e.g',
    'i.e',
    'etc',
    'vs',
    'Fig',
    'No',
    'Inc',
    'Ltd',
    'Co',
    'Corp',
    'St',
    'Ave',
    'Blvd',
    'Rd',
    'apt',
    'cf',
    'al',
    'et',
}


def _is_abbreviation(text: str, pos: int) -> bool:
    """Check if the period at pos is part of an abbreviation."""
    start = pos
    while start > 0 and text[start - 1].isalpha():
        start -= 1
    word = text[start:pos]
    return word in ABBREVIATIONS


def _is_sentence_boundary(text: str, pos: int) -> bool:
    """Check if position (pointing to . ! or ?) is a valid sentence boundary."""
    if pos >= len(text):
        return False
    if text[pos] not in '.!?':
        return False
    if _is_abbreviation(text, pos):
        return False
    rest = text[pos + 1 :]
    if not rest:
        return True
    if rest[0] == '\n':
        return True
    if rest[0].isspace():
        stripped = rest.lstrip()
        if stripped and stripped[0].isupper():
            return True
        if not stripped:
            return True
    return False


def _snap_to_sentence(text: str, cdc_pos: int, window: int = 100) -> int:
    """Find nearest sentence boundary forward from CDC position within window."""
    start = min(cdc_pos, len(text))
    end = min(start + window, len(text))
    for i in range(start, end):
        if text[i] in '.!?' and _is_sentence_boundary(text, i):
            return i + 1
    return cdc_pos


def _detect_fenced_code_blocks(text: str) -> list[tuple[int, int, str]]:
    """Find all fenced code blocks. Returns list of (start, end, fence_char)."""
    blocks: list[tuple[int, int, str]] = []
    pos = 0
    while pos < len(text):
        match = FENCED_CODE_PATTERN.match(text, pos)
        if match:
            fence = match.group('fence')
            fence_char = fence[0]
            fence_len = len(fence)
            start = match.start()
            pos = match.end()
            while pos < len(text):
                close_match = FENCED_CODE_PATTERN.match(text, pos)
                if close_match and close_match.group('fence')[0] == fence_char:
                    if len(close_match.group('fence')) >= fence_len:
                        blocks.append((start, close_match.end(), fence_char))
                        pos = close_match.end()
                        break
                pos += 1
            else:
                blocks.append((start, len(text), fence_char))
        else:
            pos += 1
    return blocks


def _detect_lists(text: str, exclude_ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Find list blocks, excluding ranges (e.g., code blocks). Returns (start, end) tuples."""
    lists: list[tuple[int, int]] = []
    in_list = False
    list_start = 0
    i = 0
    while i < len(text):
        for ex_start, ex_end in exclude_ranges:
            if ex_start <= i < ex_end:
                if in_list:
                    lists.append((list_start, i))
                    in_list = False
                i = ex_end
                break
        else:
            match = LIST_ITEM_PATTERN.match(text, i)
            if match:
                if not in_list:
                    list_start = match.start()
                    in_list = True
                i = match.end()
                while i < len(text) and text[i] != '\n':
                    i += 1
                if i < len(text):
                    i += 1
            else:
                if in_list:
                    if i < len(text) and text[i] == '\n':
                        i += 1
                        continue
                    lists.append((list_start, i))
                    in_list = False
                i += 1
    if in_list:
        lists.append((list_start, len(text)))
    return lists


def _detect_frontmatter(text: str) -> tuple[str | None, int]:
    """Extract frontmatter and return (frontmatter_text, end_position) or (None, 0)."""
    match = FRONTMATTER_PATTERN.match(text)
    if match:
        return (match.group(0), match.end())
    return (None, 0)


def _find_split_points_in_code(text: str, start: int, end: int) -> list[int]:
    """Find blank lines within a code block for potential splitting."""
    points: list[int] = []
    i = start
    first_line = True
    while i < end:
        line_end = text.find('\n', i)
        if line_end == -1 or line_end >= end:
            break
        line = text[i:line_end]
        if not first_line and not line.strip():
            points.append(line_end + 1)
        first_line = False
        i = line_end + 1
    return points


def _find_split_points_in_list(text: str, start: int, end: int) -> list[int]:
    """Find positions between top-level list items for splitting."""
    points: list[int] = []
    i = start
    last_top_level_end = start
    while i < end:
        match = LIST_ITEM_PATTERN.match(text, i)
        if match:
            indent_len = len(match.group('indent'))
            if indent_len == 0:
                if last_top_level_end > start:
                    points.append(last_top_level_end)
                line_end = text.find('\n', i)
                if line_end == -1 or line_end >= end:
                    last_top_level_end = end
                else:
                    last_top_level_end = line_end + 1
                i = last_top_level_end
            else:
                line_end = text.find('\n', i)
                i = (line_end + 1) if line_end != -1 and line_end < end else end
        else:
            i += 1
    return points


def _detect_protected_zones(
    text: str, block_size: int, hard_limit: int
) -> tuple[list[ProtectedZone], str | None, int]:
    """Detect all protected zones in text.

    Returns:
        (zones, frontmatter_text, frontmatter_end_offset)
    """
    zones: list[ProtectedZone] = []
    frontmatter_text, fm_end = _detect_frontmatter(text)
    if frontmatter_text:
        zones.append(
            ProtectedZone(
                start=0,
                end=fm_end,
                zone_type='frontmatter',
                can_split=False,
                split_points=[],
            )
        )
    code_blocks = _detect_fenced_code_blocks(text)
    code_ranges = [(s, e) for s, e, _ in code_blocks]
    for start, end, _ in code_blocks:
        size = end - start
        if size > hard_limit:
            split_points = _find_split_points_in_code(text, start, end)
            zones.append(
                ProtectedZone(
                    start=start,
                    end=end,
                    zone_type='code_fenced',
                    can_split=True,
                    split_points=split_points,
                )
            )
        else:
            zones.append(
                ProtectedZone(
                    start=start,
                    end=end,
                    zone_type='code_fenced',
                    can_split=False,
                    split_points=[],
                )
            )
    lists = _detect_lists(text, code_ranges)
    for start, end in lists:
        size = end - start
        if size > block_size:
            split_points = _find_split_points_in_list(text, start, end)
            zones.append(
                ProtectedZone(
                    start=start,
                    end=end,
                    zone_type='list',
                    can_split=True,
                    split_points=split_points,
                )
            )
        else:
            zones.append(
                ProtectedZone(
                    start=start,
                    end=end,
                    zone_type='list',
                    can_split=False,
                    split_points=[],
                )
            )
    zones.sort(key=lambda z: z.start)
    return zones, frontmatter_text, fm_end


def _find_zone_at(zones: list[ProtectedZone], pos: int) -> ProtectedZone | None:
    """Find the zone containing position, if any."""
    for zone in zones:
        if zone.start <= pos < zone.end:
            return zone
    return None


def _stable_chunk_text_legacy(text: str, hard_limit: int, block_size: int) -> list[StableBlock]:
    """Original CDC algorithm (backward compatible)."""
    raw_blocks = text.splitlines(keepends=True)
    result: list[StableBlock] = []
    block_index = 0

    buffer_text: list[str] = []
    buffer_len = 0

    avg_line_size = 80
    modulus = max(2, block_size // avg_line_size)
    min_chunk_size = block_size // 4

    def flush_buffer():
        nonlocal buffer_text, buffer_len, block_index
        if not buffer_text:
            return
        merged_text = ''.join(buffer_text)
        result.append(
            StableBlock(
                text=merged_text,
                content_hash=content_hash(merged_text),
                block_index=block_index,
            )
        )
        block_index += 1
        buffer_text = []
        buffer_len = 0

    for raw_block in raw_blocks:
        stripped = raw_block.strip()
        if not stripped:
            buffer_text.append(raw_block)
            buffer_len += len(raw_block)
            continue

        block_len = len(raw_block)

        if block_len > hard_limit:
            flush_buffer()
            sub_blocks = chunk_text(raw_block, max_chars=hard_limit, chunk_overlap=0)
            for sub_text in sub_blocks:
                result.append(
                    StableBlock(
                        text=sub_text,
                        content_hash=content_hash(sub_text),
                        block_index=block_index,
                    )
                )
                block_index += 1
            continue

        if buffer_len + block_len > hard_limit:
            flush_buffer()
            buffer_text.append(raw_block)
            buffer_len = block_len
            continue

        buffer_text.append(raw_block)
        buffer_len += block_len

        h_val = int(content_hash(stripped)[:8], 16)
        is_boundary = h_val % modulus == 0

        if buffer_len > min_chunk_size and is_boundary:
            flush_buffer()
        elif buffer_len > block_size:
            flush_buffer()

    flush_buffer()
    return result


def stable_chunk_text(
    text: str,
    hard_limit: int = BLOCK_HARD_LIMIT,
    block_size: int = 4000,
    snap_window: int = 100,
    markdown_aware: bool = True,
) -> list[StableBlock]:
    """Split text into content-addressed blocks with optional Markdown awareness.

    When markdown_aware=True:
    - Frontmatter is merged with first content chunk
    - Code blocks and lists are kept atomic when possible
    - Boundaries snap forward to sentence endings

    When markdown_aware=False:
    - Behaves like original implementation (backward compatible)

    Args:
        text: The text to chunk.
        hard_limit: Maximum chunk size before forcing split.
        block_size: Target chunk size for CDC algorithm.
        snap_window: Max chars to scan forward for sentence boundary.
        markdown_aware: Enable Markdown-aware chunking.

    Returns:
        List of StableBlock objects with content-addressed hashes.
    """
    if not markdown_aware:
        return _stable_chunk_text_legacy(text, hard_limit, block_size)

    zones, frontmatter_text, fm_end = _detect_protected_zones(text, block_size, hard_limit)
    working_text = text[fm_end:] if frontmatter_text else text
    working_zones = [
        ProtectedZone(
            start=z.start - fm_end,
            end=z.end - fm_end,
            zone_type=z.zone_type,
            can_split=z.can_split,
            split_points=[p - fm_end for p in z.split_points],
        )
        for z in zones
        if z.zone_type != 'frontmatter'
    ]

    result: list[StableBlock] = []
    block_index = 0
    buffer_text: list[str] = []
    buffer_len = 0
    current_pos = 0

    avg_line_size = 80
    modulus = max(2, block_size // avg_line_size)
    min_chunk_size = block_size // 4

    if frontmatter_text and not working_text.strip():
        return [
            StableBlock(
                text=frontmatter_text,
                content_hash=content_hash(frontmatter_text),
                block_index=0,
            )
        ]

    def flush_buffer(frontmatter: str | None = None):
        nonlocal buffer_text, buffer_len, block_index
        if not buffer_text:
            return
        merged_text = ''.join(buffer_text)
        if frontmatter and block_index == 0:
            merged_text = frontmatter + merged_text
        result.append(
            StableBlock(
                text=merged_text,
                content_hash=content_hash(merged_text),
                block_index=block_index,
            )
        )
        block_index += 1
        buffer_text = []
        buffer_len = 0

    lines = working_text.splitlines(keepends=True)
    line_idx = 0
    while line_idx < len(lines):
        line = lines[line_idx]
        line_start = current_pos
        line_end = current_pos + len(line)
        zone = _find_zone_at(working_zones, line_start)

        if zone and zone.can_split and buffer_len > block_size:
            for split_point in zone.split_points:
                if line_start <= split_point <= line_end:
                    flush_buffer(frontmatter_text)
                    frontmatter_text = None
                    break

        buffer_text.append(line)
        buffer_len += len(line)
        current_pos = line_end

        if zone and not zone.can_split:
            line_idx += 1
            continue

        if zone and zone.can_split:
            line_idx += 1
            continue

        if buffer_len < min_chunk_size:
            line_idx += 1
            continue

        stripped = line.strip()
        h_val = int(content_hash(stripped)[:8], 16)
        is_boundary = h_val % modulus == 0

        if is_boundary:
            snap_pos = _snap_to_sentence(working_text, current_pos, snap_window)
            if snap_pos > current_pos and snap_pos <= current_pos + snap_window:
                extra_lines: list[str] = []
                extra_len = 0
                temp_pos = current_pos
                lines_to_skip = 0
                for next_line in lines[line_idx + 1 :]:
                    next_line_end = temp_pos + len(next_line)
                    if next_line_end > snap_pos:
                        break
                    extra_lines.append(next_line)
                    extra_len += len(next_line)
                    temp_pos = next_line_end
                    lines_to_skip += 1
                if extra_lines:
                    buffer_text.extend(extra_lines)
                    buffer_len += extra_len
                    current_pos = temp_pos
                    line_idx += lines_to_skip
            flush_buffer(frontmatter_text)
            frontmatter_text = None
        elif buffer_len > block_size:
            flush_buffer(frontmatter_text)
            frontmatter_text = None

        line_idx += 1

    flush_buffer(frontmatter_text)
    return result


def chunk_text(text: str, max_chars: int, chunk_overlap: int = 0) -> list[str]:
    """Simple text chunker."""
    if len(text) <= max_chars:
        return [text]
    splitter = RecursiveCharacterTextSplitter(chunk_size=max_chars, chunk_overlap=chunk_overlap)
    return splitter.split_text(text)


async def _extract_facts_with_auto_split(
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    event_date: dt.datetime,
    context: str,
    lm: dspy.LM,
    predictor: dspy.Predict,
    agent_name: str | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> list[RawFact]:
    """
    Wrapper that handles OutputTooLongError by splitting the chunk.
    """
    try:
        return await _extract_facts_from_chunk(
            chunk=chunk,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            event_date=event_date,
            context=context,
            lm=lm,
            predictor=predictor,
            agent_name=agent_name,
            semaphore=semaphore,
        )
    except OutputTooLongException:
        logger.warning(f'Output too long for chunk {chunk_index + 1}. Splitting...')

        mid_point = len(chunk) // 2
        # Simple split logic
        first_half = chunk[:mid_point].strip()
        second_half = chunk[mid_point:].strip()

        sub_tasks = [
            _extract_facts_with_auto_split(
                first_half,
                chunk_index,
                total_chunks,
                event_date,
                context,
                lm=lm,
                predictor=predictor,
                agent_name=agent_name,
                semaphore=semaphore,
            ),
            _extract_facts_with_auto_split(
                second_half,
                chunk_index,
                total_chunks,
                event_date,
                context,
                lm=lm,
                predictor=predictor,
                agent_name=agent_name,
                semaphore=semaphore,
            ),
        ]

        sub_results = await asyncio.gather(*sub_tasks)

        all_facts: list[RawFact] = []
        for sub_facts in sub_results:
            all_facts.extend(sub_facts)

        return all_facts


def _convert_causal_relations(
    relations_from_llm: list[CausalRelation], fact_start_idx: int
) -> list[CausalRelation]:
    """Convert DSPy causal relations to App causal relations, adjusting indices."""
    causal_relations: list[CausalRelation] = []
    for rel in relations_from_llm:
        # Skip invalid references
        if rel.target_fact_index < 0:
            continue

        causal_relation = CausalRelation(
            relationship_type=rel.relationship_type,
            target_fact_index=fact_start_idx + rel.target_fact_index,
            strength=rel.strength,
        )
        causal_relations.append(causal_relation)
    return causal_relations


async def extract_facts_from_chunks(
    chunks: list[str],
    event_date: dt.datetime,
    lm: dspy.LM,
    predictor: dspy.Predict,
    agent_name: str,
    context: str = '',
    semaphore: asyncio.Semaphore | None = None,
) -> tuple[list[RawFact], list[tuple[str, int]]]:
    """Extract facts from pre-chunked text segments in parallel."""
    tasks = [
        _extract_facts_with_auto_split(
            chunk=chunk,
            chunk_index=i,
            total_chunks=len(chunks),
            event_date=event_date,
            context=context,
            lm=lm,
            predictor=predictor,
            agent_name=agent_name,
            semaphore=semaphore,
        )
        for i, chunk in enumerate(chunks)
    ]

    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    all_facts: list[RawFact] = []
    chunk_metadata: list[tuple[str, int]] = []
    errors: list[BaseException] = []

    for chunk, result in zip(chunks, chunk_results):
        if isinstance(result, BaseException):
            errors.append(result)
            chunk_metadata.append((chunk, 0))
        else:
            all_facts.extend(result)
            chunk_metadata.append((chunk, len(result)))

    if errors and not all_facts:
        raise ExtractionError(
            f'All {len(errors)}/{len(chunks)} chunk(s) failed extraction. First error: {errors[0]}'
        ) from errors[0]

    if errors:
        logger.warning(
            f'{len(errors)}/{len(chunks)} chunk(s) failed extraction, '
            f'proceeding with {len(all_facts)} facts from remaining chunks.'
        )

    return all_facts, chunk_metadata


async def extract_facts_from_text(
    text: str,
    event_date: dt.datetime,
    lm: dspy.LM,
    predictor: dspy.Predict,
    agent_name: str,
    chunk_max_chars: int,
    chunk_overlap: int,
    context: str = '',
    semaphore: asyncio.Semaphore | None = None,
) -> tuple[list[RawFact], list[tuple[str, int]]]:
    """Backward-compatible: chunks text internally, then extracts."""
    # We prefer stable chunking to ensure consistency with incremental updates.
    # Note: stable_chunk_text ignores chunk_overlap, which is acceptable.
    stable_blocks = await asyncio.to_thread(stable_chunk_text, text, block_size=chunk_max_chars)
    chunks = [b.text for b in stable_blocks]

    return await extract_facts_from_chunks(
        chunks=chunks,
        event_date=event_date,
        lm=lm,
        predictor=predictor,
        agent_name=agent_name,
        context=context,
        semaphore=semaphore,
    )


# ---------------------------------------------------------------------------
# PageIndex: AsyncMarkdownPageIndex (ported from prototype indexer.py)
# ---------------------------------------------------------------------------


class AsyncMarkdownPageIndex(dspy.Module):
    """Hierarchical document indexer producing a TOC tree with section summaries.

    Operates in two modes:
    - **Fast path** (regex): well-structured markdown with clear header hierarchy
    - **LLM path**: unstructured or poorly-structured documents requiring LLM scanning
    """

    def __init__(
        self,
        lm: dspy.LM,
        *,
        scan_max_concurrency: int = 5,
        gap_rescan_threshold_tokens: int = 2000,
    ) -> None:
        super().__init__()
        self.lm = lm
        self.scanner = dspy.ChainOfThought(ScanChunk)
        self.architect = dspy.ChainOfThought(OrganizeStructure)
        self.summarizer = dspy.ChainOfThought(SummarizeSection)
        self.parent_summarizer = dspy.ChainOfThought(SummarizeParentSection)
        self.block_summarizer = dspy.ChainOfThought(SummarizeBlock)
        self.scan_max_concurrency = scan_max_concurrency
        self.gap_rescan_threshold_tokens = gap_rescan_threshold_tokens
        # Lazily bound to the running event loop on first acquire.
        self._scan_semaphore = asyncio.Semaphore(scan_max_concurrency)
        self._logger = logging.getLogger('memex.core.memory.extraction.page_index')

    async def aforward(
        self,
        full_text: str,
        max_scan_tokens: int = 20_000,
        max_node_length: int = 5000,
        block_size: int = 1000,
    ) -> PageIndexOutput:
        doc_length = len(full_text)
        self._logger.info(f'Document length: {doc_length} chars')

        regex_headers = detect_markdown_headers_regex(full_text)
        quality = assess_structure_quality(regex_headers, doc_length)
        self._logger.info(f'[Assessment] {quality.reason}')

        if quality.is_well_structured:
            output = await self._fast_path(
                full_text, regex_headers, quality, max_node_length, block_size
            )
        else:
            output = await self._llm_path(
                full_text, regex_headers, quality, max_scan_tokens, max_node_length, block_size
            )
        return output

    async def _fast_path(
        self,
        full_text: str,
        regex_headers: list[DetectedHeader],
        quality: StructureQuality,
        max_node_length: int,
        block_size: int,
    ) -> PageIndexOutput:
        self._logger.info(
            f'[Fast Path] Using {quality.header_count} regex-detected headers directly.'
        )

        toc_tree = build_tree_from_regex_headers(regex_headers)

        self._logger.info('[Fast Path] Hydrating content')
        hydrate_tree(toc_tree, regex_headers, full_text)

        for node in toc_tree:
            node._assign_content_hash_ids()

        self._logger.info('[Fast Path] Recursive refinement')
        final_tree = await self._refine_tree_recursively(toc_tree, full_text, max_node_length)

        coverage = compute_coverage(final_tree, len(full_text))
        self._logger.info(f'[Fast Path] Coverage: {coverage:.1%}')

        self._logger.info('[Fast Path] Building blocks')
        blocks_list, node_map = generate_blocks_and_assign_ids(final_tree, block_size)

        self._logger.info('[Fast Path] Generating node summaries')
        await self._generate_summaries_parallel(final_tree)

        self._logger.info('[Fast Path] Generating block summaries')
        await self._generate_block_summaries(blocks_list, final_tree, node_map)

        return PageIndexOutput(
            toc=final_tree,
            blocks=blocks_list,
            node_to_block_map=node_map,
            coverage_ratio=coverage,
            path_used='regex_fast',
        )

    async def _llm_path(
        self,
        full_text: str,
        regex_headers: list[DetectedHeader],
        quality: StructureQuality,
        max_scan_tokens: int,
        max_node_length: int,
        block_size: int,
    ) -> PageIndexOutput:
        self._logger.info('[LLM Path] Document is not well-structured. Running full LLM scan.')

        flat_headers = await self._scan_document_parallel(full_text, max_scan_tokens)
        flat_headers = await self._detect_and_fill_gaps(
            flat_headers, full_text, max_scan_tokens=max_scan_tokens
        )

        if not flat_headers:
            if regex_headers:
                self._logger.warning(
                    '[LLM Path] LLM scan found nothing. Falling back to partial regex headers.'
                )
                return await self._fast_path(
                    full_text, regex_headers, quality, max_node_length, block_size
                )
            return PageIndexOutput(
                toc=[], blocks=[], node_to_block_map={}, coverage_ratio=0.0, path_used='llm_scan'
            )

        self._logger.info('[LLM Path] Verifying headers')
        flat_headers = verify_headers(flat_headers, full_text)

        verified_count = sum(1 for h in flat_headers if h.verified)
        total_count = len(flat_headers)
        accuracy = verified_count / total_count if total_count > 0 else 0
        self._logger.info(
            f'[LLM Path] Verification: {verified_count}/{total_count} ({accuracy:.0%})'
        )

        if accuracy < 0.6 and regex_headers:
            self._logger.warning('[LLM Path] Low accuracy. Merging regex headers as fallback.')
            flat_headers = merge_header_lists(flat_headers, regex_headers)
            flat_headers = verify_headers(flat_headers, full_text)

        flat_headers = [h for h in flat_headers if h.verified]
        for i, h in enumerate(flat_headers):
            h.id = i

        if not flat_headers:
            return PageIndexOutput(
                toc=[], blocks=[], node_to_block_map={}, coverage_ratio=0.0, path_used='llm_scan'
            )

        self._logger.info('[LLM Path] Building logical structure via LLM')
        toc_tree = await self._build_logical_tree(flat_headers)

        self._logger.info('[LLM Path] Hydrating content')
        hydrate_tree(toc_tree, flat_headers, full_text)

        for node in toc_tree:
            node._assign_content_hash_ids()

        self._logger.info('[LLM Path] Recursive refinement')
        final_tree = await self._refine_tree_recursively(toc_tree, full_text, max_node_length)

        coverage = compute_coverage(final_tree, len(full_text))
        self._logger.info(f'[LLM Path] Coverage: {coverage:.1%}')

        self._logger.info('[LLM Path] Building blocks')
        blocks_list, node_map = generate_blocks_and_assign_ids(final_tree, block_size)

        self._logger.info('[LLM Path] Generating node summaries')
        await self._generate_summaries_parallel(final_tree)

        self._logger.info('[LLM Path] Generating block summaries')
        await self._generate_block_summaries(blocks_list, final_tree, node_map)

        return PageIndexOutput(
            toc=final_tree,
            blocks=blocks_list,
            node_to_block_map=node_map,
            coverage_ratio=coverage,
            path_used='llm_scan',
        )

    # ---- LLM-dependent scanning ----

    def _build_scan_tasks(
        self,
        text: str,
        max_scan_tokens: int,
        *,
        offset_base: int = 0,
        context_prefix: str = '',
        overlap_chars: int = 200,
    ) -> list:
        """Split *text* into overlapping scan chunks and return scan coroutines.

        If *text* fits in ``max_scan_tokens``, returns a single coroutine.
        Otherwise, slices *text* into overlapping chunks of ``max_scan_tokens``
        tokens and returns one coroutine per chunk. ``offset_base`` lets callers
        translate chunk-local positions back to parent-document coordinates
        (used when rescanning a gap inside a larger document).
        """
        doc_tokens = estimate_token_count(text)

        if doc_tokens <= max_scan_tokens:
            return [self._process_single_chunk(text, context_prefix, offset_base)]

        chars_per_token = len(text) / doc_tokens if doc_tokens > 0 else 4.0
        chunk_chars = int(max_scan_tokens * chars_per_token)
        tasks: list = []

        for start in range(0, len(text), chunk_chars - overlap_chars):
            end = min(start + chunk_chars, len(text))
            chunk = text[start:end]
            if len(chunk) < 50:
                continue
            if start == 0:
                prev_context = context_prefix
            else:
                ctx_start = max(0, start - 200)
                prev_context = text[ctx_start:start]
            tasks.append(self._process_single_chunk(chunk, prev_context, offset_base + start))

        return tasks

    async def _scan_document_parallel(
        self, text: str, max_scan_tokens: int
    ) -> list[DetectedHeader]:
        doc_tokens = estimate_token_count(text)
        tasks = self._build_scan_tasks(text, max_scan_tokens)

        if len(tasks) <= 1:
            self._logger.info(
                f'[LLM Path] Scanning full document ({doc_tokens} tokens) in single call.'
            )
        else:
            self._logger.info(
                f'[LLM Path] Scanning document ({doc_tokens} tokens) in {len(tasks)} chunks '
                f'(max_concurrency={self.scan_max_concurrency}).'
            )
        results = await asyncio.gather(*tasks)
        return deduplicate_and_sort(results)

    async def _process_single_chunk(
        self, chunk: str, prev_context: str, offset: int
    ) -> list[DetectedHeader]:
        valid_headers: list[DetectedHeader] = []
        # Gate the LLM call on the per-extractor semaphore so concurrent scan
        # tasks (from document parallelism *and* gap refill) don't fan out past
        # scan_max_concurrency on memory-constrained hosts.
        async with self._scan_semaphore:
            try:
                pred = await run_dspy_operation(
                    lm=self.lm,
                    predictor=self.scanner,
                    input_kwargs={'chunk_text': chunk, 'previous_context': prev_context},
                    operation_name='extraction.header_scan',
                )

                for h in pred.detected_headers:
                    try:
                        rel_idx = chunk.index(h.exact_text)
                        h.start_index = offset + rel_idx
                        valid_headers.append(h)
                        continue
                    except ValueError:
                        pass

                    tolerance = max(2, int(len(h.exact_text) * 0.15))
                    try:
                        pattern = f'({regex_lib.escape(h.exact_text)}){{e<={tolerance}}}'
                        match = regex_lib.search(pattern, chunk)
                        if match:
                            h.exact_text = match.group(0)
                            h.start_index = offset + match.start()
                            valid_headers.append(h)
                    except (regex_lib.error, ValueError, RuntimeError) as e:
                        self._logger.debug('Fuzzy regex match failed for header: %s', e)
            except (ValueError, RuntimeError, OSError, KeyError) as e:
                self._logger.error(f'Scanner Error at offset {offset}: {e}')

        return valid_headers

    async def _detect_and_fill_gaps(
        self,
        flat_headers: list[DetectedHeader],
        full_text: str,
        *,
        max_scan_tokens: int,
    ) -> list[DetectedHeader]:
        new_headers_tasks: list = []
        current_idx = 0

        for header in flat_headers:
            if header.start_index is None:
                continue

            gap_text = full_text[current_idx : header.start_index]
            gap_tokens = estimate_token_count(gap_text)
            if gap_tokens > self.gap_rescan_threshold_tokens:
                self._logger.debug(
                    f'   > Omission Detected: {gap_tokens} tokens '
                    f'({len(gap_text)} chars). Re-scanning.'
                )
                gap_context = full_text[max(0, current_idx - 200) : current_idx]
                new_headers_tasks.extend(
                    self._build_scan_tasks(
                        gap_text,
                        max_scan_tokens,
                        offset_base=current_idx,
                        context_prefix=gap_context,
                    )
                )

            current_idx = header.start_index + len(header.exact_text)

        tail_text = full_text[current_idx:]
        tail_tokens = estimate_token_count(tail_text)
        if tail_tokens > self.gap_rescan_threshold_tokens:
            self._logger.debug(
                f'   > Tail Omission Detected: {tail_tokens} tokens '
                f'({len(tail_text)} chars). Re-scanning.'
            )
            tail_context = full_text[max(0, current_idx - 200) : current_idx]
            new_headers_tasks.extend(
                self._build_scan_tasks(
                    tail_text,
                    max_scan_tokens,
                    offset_base=current_idx,
                    context_prefix=tail_context,
                )
            )

        if not new_headers_tasks:
            return flat_headers

        recovered_batches = await asyncio.gather(*new_headers_tasks)
        combined = list(flat_headers)
        for batch in recovered_batches:
            combined.extend(batch)

        return deduplicate_and_sort([combined])

    # ---- LLM-dependent tree building ----

    async def _build_logical_tree(self, flat_headers: list[DetectedHeader]) -> list[TOCNode]:
        minified_input = [
            {
                'id': h.id,
                'title': h.clean_title,
                'hint': h.level_hint,
                'scan_reasoning': h.reasoning[:150],
            }
            for h in flat_headers
        ]
        try:
            pred = await run_dspy_operation(
                lm=self.lm,
                predictor=self.architect,
                input_kwargs={'flat_headers_json': str(minified_input)},
                operation_name='extraction.header_architect',
            )
            max_id = len(flat_headers) - 1
            return filter_valid_nodes(pred.toc_tree, max_id)
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            self._logger.error(f'Architect Logic Failed: {e}')
            return [
                TOCNode(
                    original_header_id=cast(int, h.id),
                    title=h.clean_title,
                    level=1,
                    reasoning='Fallback',
                )
                for h in flat_headers
            ]

    # ---- LLM-dependent refinement ----

    async def _refine_tree_recursively(
        self, nodes: list[TOCNode], full_text: str, max_len: int
    ) -> list[TOCNode]:
        tasks = [self._process_single_node_refinement(node, full_text, max_len) for node in nodes]
        refined = await asyncio.gather(*tasks)
        return list(refined)

    async def _process_single_node_refinement(
        self, node: TOCNode, full_text: str, max_len: int
    ) -> TOCNode:
        node_len = (node.end_index or 0) - (node.start_index or 0)

        if node_len > max_len and not node.children:
            self._logger.debug(f"   > Deep Dive: Refining '{node.title}' ({node_len} chars)")

            section_text = full_text[node.start_index : node.end_index]

            sub_headers = await self._process_single_chunk(
                section_text,
                prev_context=f'...Inside section: {node.title}...',
                offset=cast(int, node.start_index),
            )

            if sub_headers:
                for idx, h in enumerate(sub_headers):
                    h.id = idx
                sub_headers = [
                    h
                    for h in sub_headers
                    if cast(int, h.start_index) > cast(int, node.start_index) + 50
                ]

                sub_headers = verify_headers(sub_headers, full_text)
                sub_headers = [h for h in sub_headers if h.verified]

                if sub_headers:
                    for idx, h in enumerate(sub_headers):
                        h.id = idx

                    sub_tree = await self._build_logical_tree(sub_headers)
                    hydrate_tree(sub_tree, sub_headers, full_text, parent_end_index=node.end_index)

                    for child in sub_tree:
                        child._assign_content_hash_ids()

                    node.children = sub_tree

                    if sub_tree:
                        new_cutoff = sub_tree[0].start_index
                        node.content = full_text[node.start_index : new_cutoff]
                        node.token_estimate = estimate_token_count(node.content)
                        node.id = content_hash_md5(node.content)

        if node.children:
            node.children = await self._refine_tree_recursively(node.children, full_text, max_len)

        return node

    # ---- Summarization ----

    async def _generate_summaries_parallel(self, nodes: list[TOCNode]) -> None:
        nodes_to_process: list[TOCNode] = []
        parent_nodes_to_process: list[TOCNode] = []

        def collect(n_list: list[TOCNode]) -> None:
            for n in n_list:
                if n.children:
                    parent_nodes_to_process.append(n)
                elif n.content and len(n.content.strip()) > 50:
                    nodes_to_process.append(n)
                collect(n.children)

        collect(nodes)

        leaf_tasks = [self._summarize_single_node(n) for n in nodes_to_process]
        await asyncio.gather(*leaf_tasks)

        parent_tasks = [self._summarize_parent_node(n) for n in parent_nodes_to_process]
        await asyncio.gather(*parent_tasks)

    async def _summarize_single_node(self, node: TOCNode) -> None:
        try:
            safe_content = node.content if node.content else ''
            pred = await run_dspy_operation(
                lm=self.lm,
                predictor=self.summarizer,
                input_kwargs={'title': node.title, 'content': safe_content},
                operation_name='extraction.summarize',
            )
            node.summary = pred.summary
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            self._logger.warning(f"Summary failed for '{node.title}': {e}")

    async def _summarize_parent_node(self, node: TOCNode) -> None:
        try:
            safe_content = node.content if node.content else ''
            children_titles = ', '.join(c.title for c in node.children)

            if node.token_estimate and node.token_estimate < 30:
                safe_content = (
                    f"[Section header only: '{node.title}']\nChild sections: {children_titles}"
                )

            pred = await run_dspy_operation(
                lm=self.lm,
                predictor=self.parent_summarizer,
                input_kwargs={
                    'title': node.title,
                    'content': safe_content,
                    'children_titles': children_titles,
                },
                operation_name='extraction.summarize_parent',
            )
            node.summary = pred.summary
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            self._logger.warning(f"Parent summary failed for '{node.title}': {e}")

    async def _generate_block_summaries(
        self,
        blocks_list: list[PageIndexBlock],
        toc_tree: list[TOCNode],
        node_to_block_map: dict[str, str],
    ) -> None:
        tasks = []

        for block in blocks_list:
            section_pairs = collect_node_summaries_for_block(block.id, toc_tree, node_to_block_map)

            if not section_pairs:
                block.summary = BlockSummary(
                    topic=', '.join(block.titles_included[:3]) or 'Untitled block',
                    key_points=[],
                )
                continue

            lines = [f'- {title}: {fmt_summary}' for title, fmt_summary in section_pairs]
            section_summaries_text = '\n'.join(lines)

            tasks.append(self._summarize_single_block(block, section_summaries_text))

        if tasks:
            await asyncio.gather(*tasks)

    async def _summarize_single_block(
        self, block: PageIndexBlock, section_summaries_text: str
    ) -> None:
        try:
            pred = await run_dspy_operation(
                lm=self.lm,
                predictor=self.block_summarizer,
                input_kwargs={'section_summaries': section_summaries_text},
                operation_name='extraction.summarize_block',
            )
            summary = pred.block_summary
            summary.key_points = [kp.rstrip('.;:, ') for kp in summary.key_points]
            block.summary = summary
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            self._logger.warning(f'Block summary failed for block {block.id}: {e}')
            block.summary = BlockSummary(
                topic=', '.join(block.titles_included[:3]) or 'Untitled block',
                key_points=[],
            )


async def index_document(
    full_text: str,
    lm: dspy.LM,
    max_scan_tokens: int = 20_000,
    max_node_length: int = 5000,
    block_token_target: int = 1000,
    short_doc_threshold: int = 2000,
    *,
    scan_max_concurrency: int = 5,
    gap_rescan_threshold_tokens: int = 2000,
) -> PageIndexOutput:
    """Top-level function to index a document using PageIndex.

    Handles short-document bypass and delegates to AsyncMarkdownPageIndex
    for longer/structured documents.

    Args:
        full_text: The full document text to index.
        lm: DSPy language model for LLM calls.
        max_scan_tokens: Max tokens per LLM scan call. Small docs go in one call.
        max_node_length: Max characters per node before refinement.
        block_token_target: Target token count per block.
        short_doc_threshold: Documents below this with no headers bypass PageIndex.
        scan_max_concurrency: Cap on concurrent LLM scan calls during page_index
            extraction. Lower this on memory-constrained hosts.
        gap_rescan_threshold_tokens: Minimum gap size (tokens) between detected
            headers that triggers a secondary LLM re-scan.

    Returns:
        PageIndexOutput with TOC tree, blocks, and coverage information.
    """
    # Short-document bypass
    regex_headers = detect_markdown_headers_regex(full_text)
    if len(full_text) < short_doc_threshold and not regex_headers:
        node = TOCNode(
            original_header_id=0,
            title='Content',
            level=1,
            reasoning='Short document bypass — single node.',
            content=full_text,
            start_index=0,
            end_index=len(full_text),
            token_estimate=estimate_token_count(full_text),
        )
        node.id = content_hash_md5(full_text)

        block = PageIndexBlock(
            id=content_hash_md5(full_text),
            seq=0,
            content=full_text,
            token_count=node.token_estimate or 0,
            titles_included=['Content'],
            start_index=0,
            end_index=len(full_text),
        )

        return PageIndexOutput(
            toc=[node],
            blocks=[block],
            node_to_block_map={node.id: block.id},
            coverage_ratio=1.0,
            path_used='short_doc_bypass',
        )

    # Full PageIndex
    indexer = AsyncMarkdownPageIndex(
        lm=lm,
        scan_max_concurrency=scan_max_concurrency,
        gap_rescan_threshold_tokens=gap_rescan_threshold_tokens,
    )
    # Wrap in timeout — individual LLM calls have their own timeout via dspy.LM,
    # but the full page-index pipeline (multiple sequential LLM calls) can hang
    # if any single call stalls silently.
    return await asyncio.wait_for(
        indexer.aforward(
            full_text,
            max_scan_tokens=max_scan_tokens,
            max_node_length=max_node_length,
            block_size=block_token_target,
        ),
        timeout=600,  # 10 min max for full page index pipeline
    )
