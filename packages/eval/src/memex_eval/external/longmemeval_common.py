"""Shared utilities for the LongMemEval benchmark.

Types, dataset loaders, and JSONL helpers used across the
`longmemeval_*.py` module family.

Upstream dataset: https://github.com/xiaowu0162/longmemeval (v1 release,
500 evaluation questions across six cognitive-ability categories + `_abs`
abstention variants). The loaders do not redistribute dataset files —
callers pass a local path.

Checksum policy: ``DATASET_SHA256`` pins per-variant SHA-256 digests.
Loading a variant whose pin is ``None`` raises
``DatasetChecksumUnpinnedError`` by default. Operators who explicitly
pass ``allow_unpinned=True`` (CLI flag ``--allow-unpinned-checksum``)
bypass the check with a loud warning. A mismatched pin always raises
``DatasetChecksumMismatchError`` — there is no override for that.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger('memex_eval.longmemeval_common')

VAULT_NAME_PREFIX = 'longmemeval'

# ---------------------------------------------------------------------------
# Dataset checksums (v1 release). Pinned to catch silent upstream drift.
# ---------------------------------------------------------------------------

# Per-variant SHA-256 pins. ``None`` means "not yet pinned" — the loader
# REFUSES to proceed in that state unless ``allow_unpinned=True`` is
# explicitly passed. On first run against the upstream release, operators
# should pass the override once, copy the hash the loader logs, then paste
# it here; subsequent runs will fail loudly if upstream silently re-releases.
#
# The ``'s'`` pin is the canonical one — LongMemEval-S is the default variant
# because it matches agentmemory's baseline (same 500 questions, ~40 sessions
# each, ~115k tokens/question). ``'oracle'`` and ``'m'`` remain available
# for ablation studies.
DATASET_SHA256: dict[str, str | None] = {
    'oracle': None,
    's': None,
    'm': None,
}


class DatasetChecksumError(RuntimeError):
    """Base class for dataset-checksum failures."""


class DatasetChecksumUnpinnedError(DatasetChecksumError):
    """Raised when a variant's SHA-256 pin is None and no override was passed."""


class DatasetChecksumMismatchError(DatasetChecksumError):
    """Raised when the computed SHA-256 does not match the pinned value."""


# Canonical filenames as distributed by upstream (see upstream release page).
DATASET_FILENAMES: dict[str, str] = {
    'oracle': 'longmemeval_oracle.json',
    's': 'longmemeval_s.json',
    'm': 'longmemeval_m.json',
}


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class LongMemEvalCategory(StrEnum):
    """The six cognitive-ability categories in LongMemEval v1."""

    SINGLE_SESSION_USER = 'single-session-user'
    SINGLE_SESSION_ASSISTANT = 'single-session-assistant'
    SINGLE_SESSION_PREFERENCE = 'single-session-preference'
    TEMPORAL_REASONING = 'temporal-reasoning'
    KNOWLEDGE_UPDATE = 'knowledge-update'
    MULTI_SESSION = 'multi-session'


CATEGORY_NAMES: dict[LongMemEvalCategory, str] = {
    LongMemEvalCategory.SINGLE_SESSION_USER: 'Single-Session (User)',
    LongMemEvalCategory.SINGLE_SESSION_ASSISTANT: 'Single-Session (Assistant)',
    LongMemEvalCategory.SINGLE_SESSION_PREFERENCE: 'Single-Session (Preference)',
    LongMemEvalCategory.TEMPORAL_REASONING: 'Temporal Reasoning',
    LongMemEvalCategory.KNOWLEDGE_UPDATE: 'Knowledge Update',
    LongMemEvalCategory.MULTI_SESSION: 'Multi-Session',
}


# ---------------------------------------------------------------------------
# Pydantic types
# ---------------------------------------------------------------------------


class LongMemEvalTurn(BaseModel):
    """A single chat turn within a LongMemEval session."""

    role: str = Field(description='"user" or "assistant".')
    content: str
    timestamp: datetime = Field(description='Turn timestamp, timezone-aware when possible.')


class LongMemEvalSession(BaseModel):
    """A conversation session (list of turns) with a session-level date."""

    session_id: str
    session_date: datetime
    turns: list[LongMemEvalTurn] = Field(default_factory=list)


class LongMemEvalQuestion(BaseModel):
    """A LongMemEval evaluation question plus its evidence sessions."""

    question_id: str
    category: LongMemEvalCategory
    is_abstention: bool = Field(
        description='True if question_id ends with "_abs" — correct answer is "I do not know".',
    )
    question_text: str
    answer: str | None = Field(
        default=None,
        description='Ground-truth answer. May be None for abstention questions.',
    )
    answer_session_ids: list[str] = Field(
        default_factory=list,
        description='Session IDs containing the evidence needed to answer this question.',
    )
    question_date: datetime | None = Field(
        default=None,
        description='When the question was asked. Used as reference_date for temporal queries.',
    )
    sessions: list[LongMemEvalSession] = Field(default_factory=list)


class LongMemEvalToolCall(BaseModel):
    """A single MCP tool invocation recorded from the subagent."""

    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class LongMemEvalHypothesis(BaseModel):
    """A single answering-LM output for one question."""

    question_id: str
    hypothesis: str
    retrieved_unit_ids: list[str] = Field(default_factory=list)
    answer_model_fingerprint: str
    latency_ms: float = 0.0
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    num_turns: int = 0
    tool_calls: list[LongMemEvalToolCall] = Field(default_factory=list)
    session_id: str | None = None
    trace_file: str | None = None


class LongMemEvalJudgment(BaseModel):
    """A single judge output for one hypothesis."""

    question_id: str
    category: LongMemEvalCategory
    is_abstention: bool
    hypothesis: str
    expected: str | None
    correct: bool
    judge_reasoning: str
    judge_model_fingerprint: str
    is_abstention_hypothesis: bool = Field(
        default=False,
        description=(
            'True when the LM judge classified this hypothesis as an abstention. '
            'Populated for every question — not just ground-truth abstention ones — '
            'so abstention precision can be computed without re-using the judge '
            'correctness signal as its own denominator.'
        ),
    )
    retrieval_contains_answer: bool = Field(
        default=True,
        description='Whether the memex retrieval output contained sufficient evidence to answer.',
    )
    retrieval_containment_reasoning: str = Field(
        default='',
        description='LLM judge reasoning for retrieval containment.',
    )
    interpretation: str = Field(
        default='correct',
        description=(
            'Error analysis label derived from the 2x2 matrix of retrieval containment '
            'and answer correctness. One of: correct, model_error, correct_abstention, '
            'hallucination, lucky_guess.'
        ),
    )


class LongMemEvalCategoryResult(BaseModel):
    """Aggregate accuracy for a single question category."""

    category: LongMemEvalCategory
    n_questions: int
    n_correct: int
    accuracy: float


class LongMemEvalReport(BaseModel):
    """Full aggregated report for one LongMemEval run."""

    run_id: str
    variant: str  # 's' | 'm' | 'oracle'
    total_questions: int
    overall_accuracy: float
    per_category: list[LongMemEvalCategoryResult] = Field(default_factory=list)
    abstention_precision: float = 0.0
    abstention_recall: float = 0.0
    answer_model_fingerprint: str = ''
    judge_model_fingerprint: str = ''
    total_cost_usd: float | None = None
    dataset_sha256: str = ''


# ---------------------------------------------------------------------------
# JSONL helpers.
# ---------------------------------------------------------------------------


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read all records from a JSONL file. Returns [] if the file is missing."""
    p = Path(path)
    if not p.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to a JSONL file (creating it if needed)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a') as f:
        f.write(json.dumps(record, default=str) + '\n')


def read_completed_ids(path: str | Path) -> set[str]:
    """Read the set of already-answered question IDs from a JSONL file."""
    records = read_jsonl(path)
    return {r['question_id'] for r in records if 'question_id' in r}


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _verify_checksum(path: Path, variant: str, *, allow_unpinned: bool = False) -> str:
    """Compute and verify the SHA-256 of ``path`` against the pinned value.

    Returns the actual hash (for embedding in reports). Raises
    ``DatasetChecksumUnpinnedError`` when the pin is ``None`` and
    ``allow_unpinned`` is False. Raises ``DatasetChecksumMismatchError``
    when the pin is set and does not match.
    """
    actual = _sha256_file(path)
    pinned = DATASET_SHA256.get(variant)
    if pinned is None:
        if not allow_unpinned:
            raise DatasetChecksumUnpinnedError(
                f'Dataset checksum not pinned for longmemeval variant {variant!r}; '
                f'refusing to proceed. Run once against the upstream release with '
                f'--allow-unpinned-checksum, capture the hash from the log '
                f'(actual={actual}), then pin it in '
                f'memex_eval.external.longmemeval_common.DATASET_SHA256.'
            )
        logger.warning(
            'UNPINNED checksum for longmemeval variant %r. Computed hash: %s. '
            'Running with --allow-unpinned-checksum — pin this value in '
            'DATASET_SHA256 for reproducible runs.',
            variant,
            actual,
        )
        return actual
    if actual != pinned:
        raise DatasetChecksumMismatchError(
            f'Dataset checksum mismatch for {path} (variant={variant!r}): '
            f'expected {pinned}, got {actual}. Upstream may have re-released '
            f'without a version bump.'
        )
    return actual


def _parse_turn(raw: dict[str, Any], *, fallback_date: datetime | None = None) -> LongMemEvalTurn:
    """Normalise an upstream turn record.

    ``fallback_date`` should be the session-level date so that turns
    without per-turn timestamps inherit the session date instead of
    falling back to epoch (which causes ``power(2, -687)`` underflow
    in the graph strategy's temporal decay).
    """
    ts = raw.get('timestamp') or raw.get('time') or raw.get('date')
    if ts is None:
        ts_parsed = fallback_date or datetime.now()
    elif isinstance(ts, datetime):
        ts_parsed = ts
    else:
        ts_parsed = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    return LongMemEvalTurn(
        role=raw.get('role', 'user'),
        content=raw.get('content', ''),
        timestamp=ts_parsed,
    )


_UPSTREAM_DATE_FORMATS = (
    '%Y/%m/%d (%a) %H:%M',  # canonical LongMemEval upstream format, e.g. '2023/05/20 (Sat) 02:21'
    '%Y/%m/%d %H:%M',
    '%Y-%m-%d %H:%M:%S',
)


def _parse_upstream_date(value: Any) -> datetime:
    """Parse a session/turn timestamp in any of the shapes upstream uses."""
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    s = str(value)
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        pass
    for fmt in _UPSTREAM_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f'Unrecognised LongMemEval date format: {s!r}')


def _parse_session(
    raw: dict[str, Any] | list[dict[str, Any]],
    fallback_idx: int = 0,
    *,
    session_id: str | None = None,
    session_date: Any = None,
) -> LongMemEvalSession:
    """Normalise an upstream session record.

    Upstream LongMemEval ships sessions as a parallel-array layout: each
    question carries `haystack_session_ids[i]`, `haystack_dates[i]`, and
    `haystack_sessions[i]` (a list of turn dicts). The caller passes the
    three pieces explicitly via `session_id` / `session_date` / `raw=turns`.

    For backwards compatibility with self-contained fixture rows (and any
    legacy upstream variant), `raw` may also be a dict with `session_id`,
    `session_date`, and `turns` keys — in that case the `session_id` and
    `session_date` kwargs are ignored.
    """
    if isinstance(raw, dict):
        sid = str(raw.get('session_id') or raw.get('id') or f'session-{fallback_idx}')
        date_value = raw.get('session_date') or raw.get('date') or raw.get('timestamp')
        turns_raw = raw.get('turns') or raw.get('messages') or []
    else:
        sid = str(session_id or f'session-{fallback_idx}')
        date_value = session_date
        turns_raw = raw
    parsed_date = _parse_upstream_date(date_value)
    return LongMemEvalSession(
        session_id=sid,
        session_date=parsed_date,
        turns=[_parse_turn(t, fallback_date=parsed_date) for t in turns_raw],
    )


def _parse_question(raw: dict[str, Any]) -> LongMemEvalQuestion:
    """Normalise an upstream question record."""
    qid = str(raw.get('question_id') or raw.get('id'))
    category_raw = raw.get('question_type') or raw.get('category')
    if category_raw is None:
        raise ValueError(f'Question {qid!r} is missing a category/question_type field.')
    # Strip a trailing "_abs" suffix when mapping to the enum — upstream
    # encodes abstention as a category suffix on both the ID and the type.
    is_abstention = qid.endswith('_abs') or str(category_raw).endswith('_abs')
    category_clean = str(category_raw).removesuffix('_abs')
    try:
        category = LongMemEvalCategory(category_clean)
    except ValueError as exc:
        raise ValueError(
            f'Unknown LongMemEval category {category_clean!r} for question {qid!r}'
        ) from exc

    sessions_raw = raw.get('haystack_sessions') or raw.get('sessions') or []
    session_ids = raw.get('haystack_session_ids') or []
    session_dates = raw.get('haystack_dates') or []
    sessions = [
        _parse_session(
            s,
            i,
            session_id=session_ids[i] if i < len(session_ids) else None,
            session_date=session_dates[i] if i < len(session_dates) else None,
        )
        for i, s in enumerate(sessions_raw)
    ]

    answer_raw = raw.get('answer')
    answer = None if answer_raw is None else str(answer_raw)
    answer_session_ids = raw.get('answer_session_ids', [])
    if not isinstance(answer_session_ids, list):
        answer_session_ids = []
    # Parse question_date — upstream format is '2023/05/30 (Tue) 23:40'
    question_date_raw = raw.get('question_date')
    question_date = None
    if question_date_raw:
        try:
            question_date = _parse_upstream_date(question_date_raw)
        except (ValueError, TypeError):
            pass

    return LongMemEvalQuestion(
        question_id=qid,
        category=category,
        is_abstention=is_abstention,
        question_text=raw.get('question') or raw.get('question_text') or '',
        answer=answer,
        answer_session_ids=[str(sid) for sid in answer_session_ids],
        question_date=question_date,
        sessions=sessions,
    )


def _load_variant(
    path: str | Path, variant: str, *, allow_unpinned: bool = False
) -> list[LongMemEvalQuestion]:
    """Load one LongMemEval variant. Verifies the dataset checksum on read."""
    p = Path(path)
    if p.is_dir():
        p = p / DATASET_FILENAMES[variant]
    if not p.exists():
        raise FileNotFoundError(
            f'LongMemEval {variant!r} dataset not found at {p}. '
            f'Download from https://github.com/xiaowu0162/longmemeval and pass '
            f'the containing directory or JSON file as --dataset-path.'
        )
    _verify_checksum(p, variant, allow_unpinned=allow_unpinned)
    raw_data = json.loads(p.read_text())
    if not isinstance(raw_data, list):
        raise ValueError(f'Expected a top-level list in {p}, got {type(raw_data).__name__}.')
    return [_parse_question(r) for r in raw_data]


def load_longmemeval_oracle(
    path: str | Path, *, allow_unpinned: bool = False
) -> list[LongMemEvalQuestion]:
    """Load the LongMemEval_Oracle variant (ground-truth evidence only)."""
    return _load_variant(path, 'oracle', allow_unpinned=allow_unpinned)


def load_longmemeval_s(
    path: str | Path, *, allow_unpinned: bool = False
) -> list[LongMemEvalQuestion]:
    """Load the LongMemEval_S variant (~40 sessions / ~115k tokens per q)."""
    return _load_variant(path, 's', allow_unpinned=allow_unpinned)


def load_longmemeval_m(
    path: str | Path, *, allow_unpinned: bool = False
) -> list[LongMemEvalQuestion]:
    """Load the LongMemEval_M variant (~500 sessions per question)."""
    return _load_variant(path, 'm', allow_unpinned=allow_unpinned)


def dataset_sha256(path: str | Path, variant: str) -> str:
    """Compute the SHA-256 hash of a dataset file (for report provenance)."""
    p = Path(path)
    if p.is_dir():
        p = p / DATASET_FILENAMES[variant]
    return _sha256_file(p)


def vault_name_for(variant: str, run_id: str) -> str:
    """Build a dedicated vault name for a LongMemEval run."""
    return f'{VAULT_NAME_PREFIX}_{variant}_{run_id}'
