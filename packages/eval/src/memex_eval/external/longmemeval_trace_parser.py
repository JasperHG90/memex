"""LongMemEval trace parser: extract retrieval recall from Claude Code session traces.

Parses session trace JSONL files to find memex retrieval tool calls, extract
returned IDs, and compute recall against gold ``answer_session_ids`` from the
upstream dataset.

The ``recall_any@3`` metric caps each retrieval tool type at the **first 3
calls** and checks whether any gold session appears among the returned results.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger('memex_eval.longmemeval_trace_parser')

_MEMEX_TOOL_PREFIX = 'mcp__memex__'

# Retrieval tools whose results we parse for recall.
_RETRIEVAL_TOOLS = {
    'mcp__memex__memex_memory_search',
    'mcp__memex__memex_note_search',
    'mcp__memex__memex_survey',
}

# Cap per tool type for recall@3.
_RECALL_CAP = 3


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A single retrieval tool call with its results."""

    tool_name: str
    tool_use_id: str
    input_params: dict[str, Any] = field(default_factory=dict)
    result_note_ids: list[str] = field(default_factory=list)
    result_note_titles: list[str] = field(default_factory=list)
    result_unit_count: int = 0


@dataclass
class TraceAnalysis:
    """Parsed trace with per-tool-type call lists."""

    question_id: str
    calls_by_tool: dict[str, list[ToolCall]] = field(default_factory=dict)

    def capped_calls(self, tool_name: str) -> list[ToolCall]:
        """Return the first ``_RECALL_CAP`` calls for a tool type."""
        return self.calls_by_tool.get(tool_name, [])[:_RECALL_CAP]

    def all_capped_note_ids(self) -> set[str]:
        """All note IDs across capped calls for all retrieval tools."""
        ids: set[str] = set()
        for tool_name in _RETRIEVAL_TOOLS:
            for call in self.capped_calls(tool_name):
                ids.update(call.result_note_ids)
        return ids

    def all_capped_note_titles(self) -> set[str]:
        """All note titles across capped calls for all retrieval tools."""
        titles: set[str] = set()
        for tool_name in _RETRIEVAL_TOOLS:
            for call in self.capped_calls(tool_name):
                titles.update(call.result_note_titles)
        return titles


@dataclass
class RecallMetrics:
    """Recall results for a single question."""

    question_id: str
    category: str
    gold_session_ids: list[str]
    found_session_ids: list[str] = field(default_factory=list)
    recall: float = 0.0
    per_tool: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Trace parsing
# ---------------------------------------------------------------------------


def _extract_ids_from_result(content: str | list[Any]) -> tuple[list[str], list[str], int]:
    """Extract note IDs, note titles, and unit count from a tool result block.

    Returns (note_ids, note_titles, unit_count).
    """
    text = ''
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'text':
                parts.append(part.get('text', ''))
        text = '\n'.join(parts)

    note_ids: list[str] = []
    note_titles: list[str] = []
    unit_count = 0

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return note_ids, note_titles, unit_count

    results = data.get('result', data.get('results', []))
    if not isinstance(results, list):
        return note_ids, note_titles, unit_count

    seen_note_ids: set[str] = set()
    seen_note_titles: set[str] = set()

    for r in results:
        if not isinstance(r, dict):
            continue
        unit_count += 1

        # note_id can be top-level or nested
        nid = r.get('note_id') or r.get('id', '')
        ntitle = r.get('note_title') or r.get('title', '')

        if nid and nid not in seen_note_ids:
            note_ids.append(str(nid))
            seen_note_ids.add(nid)
        if ntitle and ntitle not in seen_note_titles:
            note_titles.append(str(ntitle))
            seen_note_titles.add(ntitle)

    return note_ids, note_titles, unit_count


def parse_trace(trace_path: Path, question_id: str | None = None) -> TraceAnalysis:
    """Parse a Claude Code session trace JSONL and extract retrieval tool calls.

    Args:
        trace_path: Path to a ``.jsonl`` trace file.
        question_id: Optional question ID (defaults to stem of filename).

    Returns:
        A ``TraceAnalysis`` with per-tool-type call lists.
    """
    qid = question_id or trace_path.stem
    analysis = TraceAnalysis(question_id=qid)

    try:
        lines = trace_path.read_text().strip().splitlines()
    except Exception:
        logger.warning('Failed to read trace %s', trace_path)
        return analysis

    # Pass 1: collect tool_use blocks (id -> name, input)
    tool_use_map: dict[str, tuple[str, dict[str, Any]]] = {}
    # Maintain ordering by collecting tool_use_ids in order
    ordered_tool_ids: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get('type') == 'assistant':
            for block in obj.get('message', {}).get('content', []):
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    name = block.get('name', '')
                    tid = block.get('id', '')
                    if tid and name in _RETRIEVAL_TOOLS:
                        tool_use_map[tid] = (name, block.get('input', {}))
                        ordered_tool_ids.append(tid)

    # Pass 2: match tool_result blocks to tool_use IDs
    tool_results: dict[str, str | list[Any]] = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get('type') == 'user':
            for block in obj.get('message', {}).get('content', []):
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    tid = block.get('tool_use_id', '')
                    if tid in tool_use_map:
                        tool_results[tid] = block.get('content', '')

    # Build ToolCall records in order
    for tid in ordered_tool_ids:
        name, input_params = tool_use_map[tid]
        content = tool_results.get(tid, '')
        note_ids, note_titles, unit_count = _extract_ids_from_result(content)
        call = ToolCall(
            tool_name=name,
            tool_use_id=tid,
            input_params=input_params,
            result_note_ids=note_ids,
            result_note_titles=note_titles,
            result_unit_count=unit_count,
        )
        analysis.calls_by_tool.setdefault(name, []).append(call)

    return analysis


# ---------------------------------------------------------------------------
# Session ID extraction from note titles
# ---------------------------------------------------------------------------

# Note titles follow the pattern: ``{question_id} — {session_id}``
# (set by longmemeval_ingest: note_key=f'longmemeval-{variant}-{qid}-{sid}')
_NOTE_TITLE_RE = re.compile(r'^.+?\s+—\s+(.+)$')


def _session_id_from_note_title(title: str) -> str | None:
    """Extract the session_id from a note title like 'e47becba — answer_280352e9'."""
    m = _NOTE_TITLE_RE.match(title)
    return m.group(1) if m else None


def _collect_session_ids_from_trace(trace: TraceAnalysis) -> set[str]:
    """Collect all session IDs from capped retrieval calls via note titles."""
    session_ids: set[str] = set()
    for title in trace.all_capped_note_titles():
        sid = _session_id_from_note_title(title)
        if sid:
            session_ids.add(sid)
    return session_ids


# ---------------------------------------------------------------------------
# Recall computation
# ---------------------------------------------------------------------------


def compute_recall(
    trace: TraceAnalysis,
    gold_session_ids: list[str],
    category: str = '',
) -> RecallMetrics:
    """Compute recall@3 for a single question.

    Checks whether gold ``answer_session_ids`` appear among the note titles
    returned by the first 3 calls per retrieval tool type.
    """
    found_sids = _collect_session_ids_from_trace(trace)
    gold_set = set(gold_session_ids)
    found_gold = sorted(gold_set & found_sids)

    recall = len(found_gold) / len(gold_set) if gold_set else 1.0

    # Per-tool breakdown
    per_tool: dict[str, dict[str, Any]] = {}
    for tool_name in _RETRIEVAL_TOOLS:
        capped = trace.capped_calls(tool_name)
        total_calls = len(trace.calls_by_tool.get(tool_name, []))
        if not capped:
            continue

        tool_sids: set[str] = set()
        total_results = 0
        total_notes = 0
        for call in capped:
            total_results += call.result_unit_count
            total_notes += len(call.result_note_ids)
            for title in call.result_note_titles:
                sid = _session_id_from_note_title(title)
                if sid:
                    tool_sids.add(sid)

        tool_found = sorted(gold_set & tool_sids)
        per_tool[tool_name] = {
            'capped_calls': len(capped),
            'total_calls': total_calls,
            'total_results': total_results,
            'total_notes': total_notes,
            'gold_found': tool_found,
            'recall': len(tool_found) / len(gold_set) if gold_set else 1.0,
        }

    return RecallMetrics(
        question_id=trace.question_id,
        category=category,
        gold_session_ids=gold_session_ids,
        found_session_ids=found_gold,
        recall=recall,
        per_tool=per_tool,
    )


# ---------------------------------------------------------------------------
# Human-readable formatting
# ---------------------------------------------------------------------------


def format_question_breakdown(trace: TraceAnalysis, metrics: RecallMetrics | None) -> str:
    """Format a human-readable breakdown for one question."""
    lines: list[str] = []
    cat_label = f' ({metrics.category})' if metrics and metrics.category else ''
    lines.append(f'Question {trace.question_id}{cat_label}:')

    for tool_name in sorted(_RETRIEVAL_TOOLS):
        all_calls = trace.calls_by_tool.get(tool_name, [])
        capped = trace.capped_calls(tool_name)
        if not all_calls:
            continue

        short = tool_name.replace('mcp__memex__memex_', '')
        lines.append(f'  {short} ({len(capped)}/{len(all_calls)} calls used for recall):')
        for i, call in enumerate(capped):
            note_ids_short = [nid[:8] for nid in call.result_note_ids[:5]]
            extra = f'... +{len(call.result_note_ids) - 5}' if len(call.result_note_ids) > 5 else ''
            lines.append(
                f'    Call {i + 1}: returned {call.result_unit_count} units '
                f'from notes [{", ".join(note_ids_short)}{extra}]'
            )

    if metrics:
        lines.append(f'  Gold session IDs: {metrics.gold_session_ids}')
        n_gold = len(metrics.gold_session_ids)
        n_found = len(metrics.found_session_ids)
        lines.append(f'  Recall@3: {n_found}/{n_gold} gold sessions found ({metrics.recall:.3f})')
        if metrics.found_session_ids:
            lines.append(f'  Found: {metrics.found_session_ids}')
    else:
        lines.append('  (no gold session IDs available)')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Batch analysis
# ---------------------------------------------------------------------------


def parse_traces_dir(
    traces_dir: Path,
) -> list[TraceAnalysis]:
    """Parse all trace files in a directory."""
    traces: list[TraceAnalysis] = []
    for trace_file in sorted(traces_dir.glob('*.jsonl')):
        traces.append(parse_trace(trace_file))
    return traces


def compute_batch_recall(
    traces: list[TraceAnalysis],
    gold_map: dict[str, list[str]],
    category_map: dict[str, str] | None = None,
) -> list[RecallMetrics]:
    """Compute recall for all traces against a gold mapping.

    Args:
        traces: Parsed trace analyses.
        gold_map: ``{question_id: [answer_session_ids]}``.
        category_map: Optional ``{question_id: category}`` mapping.

    Returns:
        List of ``RecallMetrics``, one per trace.
    """
    results: list[RecallMetrics] = []
    for trace in traces:
        gold = gold_map.get(trace.question_id, [])
        cat = (category_map or {}).get(trace.question_id, '')
        results.append(compute_recall(trace, gold, category=cat))
    return results
