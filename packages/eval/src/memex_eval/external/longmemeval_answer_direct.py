"""LongMemEval direct-retrieval answer modes.

Provides ``note-only`` and ``memory-only`` modes that bypass the
Claude Code subagent entirely. A single tool call (``note_search`` or
``memory_search``) is made with ``limit=10``, then an LLM synthesizes
an answer from the top-10 results. If the LLM abstains, we retry once
with ``expand_query=true`` before declaring final abstention.

These modes isolate retrieval quality from agent-orchestration quality,
so accuracy/efficiency can be compared head-to-head against the full
agent mode.
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Any

import dspy
import httpx
import tiktoken

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import MemoryUnitDTO, NoteSearchResult

from memex_eval.external.longmemeval_common import LongMemEvalQuestion

logger = logging.getLogger('memex_eval.longmemeval_answer_direct')


ABSTENTION_MARKER = 'I do not know based on the available memory.'
DEFAULT_ANSWER_MODEL = 'claude-sonnet-4-6'
DEFAULT_LIMIT = 10


class DirectMode(str, enum.Enum):
    """Non-agent answer modes that call a single retrieval tool directly."""

    NOTE_ONLY = 'note-only'
    MEMORY_ONLY = 'memory-only'


class DirectAnswerSignature(dspy.Signature):
    """Answer the question using ONLY the provided memory context.

    If the context does NOT contain the answer, reply with exactly the
    abstention marker. Do NOT invent, hedge, or guess.
    """

    question: str = dspy.InputField(desc='The user question to answer.')
    context: str = dspy.InputField(desc='Retrieved memory context (numbered).')
    abstention_marker: str = dspy.InputField(
        desc='The EXACT string to reply when context does not contain the answer.'
    )
    answer: str = dspy.OutputField(
        desc='Concise plain-text answer, or the abstention marker verbatim.'
    )


def _format_memory_results(units: list[MemoryUnitDTO]) -> str:
    """Render MemoryUnits as a numbered context block for the LLM."""
    lines: list[str] = []
    for i, u in enumerate(units, start=1):
        src = getattr(u, 'note_title', None) or getattr(u, 'note_id', '?')
        lines.append(f'[{i}] ({src}) {u.text}')
    return '\n'.join(lines)


def _format_note_results(notes: list[NoteSearchResult]) -> str:
    """Render note search results as a numbered context block for the LLM."""
    lines: list[str] = []
    for i, n in enumerate(notes, start=1):
        title = getattr(n, 'title', None) or str(getattr(n, 'note_id', '?'))
        desc = getattr(n, 'description', '') or ''
        # Prefer summaries when available for richer context
        summaries = getattr(n, 'summaries', None) or []
        body_parts: list[str] = []
        if desc:
            body_parts.append(desc.strip())
        for s in summaries:
            if isinstance(s, str) and s.strip():
                body_parts.append(s.strip())
            elif isinstance(s, dict):
                txt = s.get('summary') or s.get('text') or ''
                if txt.strip():
                    body_parts.append(txt.strip())
        body = '\n  '.join(body_parts) or '(no description)'
        lines.append(f'[{i}] ({title})\n  {body}')
    return '\n'.join(lines)


async def _retrieve(
    mode: DirectMode,
    api: RemoteMemexAPI,
    query: str,
    vault_ids: list[str],
    *,
    limit: int,
    expand_query: bool,
    reference_date: Any = None,
) -> tuple[list[Any], str, list[str]]:
    """Call the single retrieval tool for this mode.

    Returns (raw_results, formatted_context, retrieved_ids).
    """
    if mode is DirectMode.MEMORY_ONLY:
        units = await api.search(
            query=query,
            limit=limit,
            vault_ids=list(vault_ids),
            expand_query=expand_query,
            reference_date=reference_date,
        )
        return units, _format_memory_results(units), [str(u.id) for u in units]

    # DirectMode.NOTE_ONLY
    notes = await api.search_notes(
        query=query,
        limit=limit,
        vault_ids=list(vault_ids),
        expand_query=expand_query,
        reference_date=reference_date,
    )
    return notes, _format_note_results(notes), [str(n.note_id) for n in notes]


async def _synthesize(
    question: str,
    context: str,
    lm: dspy.LM,
) -> tuple[str, int, int]:
    """Call the answer LLM and return (answer, input_tokens, output_tokens)."""
    predictor = dspy.Predict(DirectAnswerSignature)
    lm_call = lm.copy()

    with dspy.context(lm=lm_call):
        if hasattr(predictor, 'acall'):
            result = await predictor.acall(
                question=question,
                context=context,
                abstention_marker=ABSTENTION_MARKER,
            )
        else:
            import asyncio as _asyncio

            result = await _asyncio.to_thread(
                predictor,
                question=question,
                context=context,
                abstention_marker=ABSTENTION_MARKER,
            )

    answer = (result.answer if hasattr(result, 'answer') else '').strip()

    input_tokens = 0
    output_tokens = 0
    history = getattr(lm_call, 'history', None) or []
    for entry in history:
        usage = (entry or {}).get('usage', {}) or {}
        input_tokens += int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
        output_tokens += int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)

    if hasattr(lm_call, 'history'):
        try:
            lm_call.history.clear()
        except Exception:
            pass

    return answer, input_tokens, output_tokens


def _is_abstention(answer: str) -> bool:
    """Check whether an LLM answer should be treated as abstention."""
    a = (answer or '').strip().lower()
    if not a:
        return True
    return (
        ABSTENTION_MARKER.lower() in a
        or a.startswith('i do not know')
        or a.startswith("i don't know")
    )


async def answer_direct(
    question: LongMemEvalQuestion,
    mode: DirectMode,
    vault_name: str,
    server_url: str,
    answer_lm: dspy.LM,
    *,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Answer a single question by direct tool call + LLM synthesis.

    Implements the two-try retry policy:
      1. First attempt: tool call with ``expand_query=False``.
      2. If the LLM abstains, retry once with ``expand_query=True``.
      3. If still abstaining, return the abstention marker as final.

    Returns a dict with the same shape ``_run_claude_subagent`` uses,
    so the outer loop can serialize without branching.
    """
    t0 = time.time()
    tool_name = 'memex_memory_search' if mode is DirectMode.MEMORY_ONLY else 'memex_note_search'
    enc = tiktoken.get_encoding('cl100k_base')

    total_input_tokens = 0
    total_output_tokens = 0
    tool_calls: list[dict[str, Any]] = []
    retrieval_tokens = 0
    retrieved_ids: list[str] = []
    final_answer = ''

    attempts: list[bool] = [False]  # first attempt without expansion
    # Thread the question's reference date through so temporal expressions resolve
    reference_date = question.question_date

    async with httpx.AsyncClient(base_url=server_url, timeout=120.0) as client:
        api = RemoteMemexAPI(client)
        try:
            vault_id = await api.resolve_vault_identifier(vault_name)
            vault_ids = [str(vault_id)]
        except Exception:
            # Let the server resolve by name; should not happen in practice
            vault_ids = [vault_name]

        for attempt_idx, expand_query in enumerate(attempts + [True]):
            # Stop if we already have a non-abstention answer
            if final_answer and not _is_abstention(final_answer):
                break

            try:
                _raw, context, ids = await _retrieve(
                    mode=mode,
                    api=api,
                    query=question.question_text,
                    vault_ids=vault_ids,
                    limit=limit,
                    expand_query=expand_query,
                    reference_date=reference_date,
                )
            except (httpx.HTTPError, OSError) as e:
                logger.warning('%s retrieval failed (expand=%s): %s', tool_name, expand_query, e)
                context, ids = '', []

            retrieval_tokens += len(enc.encode(context))
            retrieved_ids = ids  # keep the most recent attempt's IDs
            tool_calls.append(
                {
                    'name': tool_name,
                    'input': {
                        'query': question.question_text,
                        'limit': limit,
                        'expand_query': expand_query,
                    },
                }
            )

            if not context.strip():
                final_answer = ABSTENTION_MARKER
                continue

            try:
                answer, in_tok, out_tok = await _synthesize(
                    question.question_text, context, answer_lm
                )
            except (RuntimeError, ValueError, OSError) as e:
                logger.warning('Answer synthesis failed (attempt %d): %s', attempt_idx + 1, e)
                answer, in_tok, out_tok = ABSTENTION_MARKER, 0, 0

            total_input_tokens += in_tok
            total_output_tokens += out_tok
            final_answer = answer

    duration = time.time() - t0

    return {
        'answer': final_answer or ABSTENTION_MARKER,
        'tool_calls': tool_calls,
        'tokens': {
            'input': total_input_tokens + retrieval_tokens,
            'output': total_output_tokens,
        },
        'num_turns': len(tool_calls),
        'cost_usd': 0.0,
        'duration_s': round(duration, 2),
        'model': DEFAULT_ANSWER_MODEL,
        'error': None,
        'retrieved_ids': retrieved_ids,
        'retrieval_tokens': retrieval_tokens,
    }
