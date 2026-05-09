"""Pydantic data model for the evaluation-suite framework.

Every ``ExpectedOutcome`` subclass scores against a uniform
``AgentAnswer`` produced by the active ``AnswerBackend`` (``api`` by
default; see ``memex_eval.suite.agents``). This decouples scoring from
how the answer was generated, enabling pluggable backends:
direct-API, Claude-Code-as-subagent, Hermes-via-plugin, or custom.

Outcomes are registered via ``@register_outcome('<type-name>')`` so
external packages can ship custom outcomes — e.g. delta-style
assertions like ``memory_worth_delta`` — without editing this file.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializeAsAny,
    model_validator,
)

from memex_eval.suite.agents import AgentAnswer
from memex_eval.suite.metrics import mrr, ndcg_at_k, recall_at_k
from memex_eval.suite.sources import NOTE_KEY_RE, SourceNote, SuiteSources

logger = logging.getLogger('memex_eval.suite.base')


# Registry-name validator (outcomes / setup-actions). Stricter than the
# scenario-id and note-key conventions: must start with a letter, no hyphens.
_REGISTRY_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')

# Word-character class for keyword-boundary detection. \w in Python 3 str
# patterns is Unicode-aware by default (no re.UNICODE flag needed).
_WORD_RE = re.compile(r'\w')


def _kw_pattern(keyword: str) -> re.Pattern[str]:
    """Word-boundary regex that handles keywords starting/ending with non-word chars.

    ``\\b`` is a transition between ``\\w`` and non-``\\w``. Two edge cases:

    1. **Non-word boundary char**: a keyword like ``$1000`` starts with ``$``
       (non-word). Prepending ``\\b`` would require the *previous* char to be
       a word char, which fails for "paid $1000" (space before `$`). We only
       emit a leading ``\\b`` when the keyword's first char is a word char.

    2. **Numeric suffix followed by a unit char**: a keyword like ``15.3``
       ends with digit ``3``. Naïvely emitting a trailing ``\\b`` requires
       the *next* char to be non-word — but in real text we see ``$15.3M``,
       ``50K``, ``18%`` where the unit char (``M``, ``K``) IS a word char
       and there is no boundary. For digit-ending keywords we replace the
       trailing ``\\b`` with a "must not be followed by another digit or
       letter that would make the number into a different number" lookahead:
       ``(?![0-9])`` — that way ``15.3`` matches inside ``$15.3M`` (next
       char is ``M``, not a digit) but does NOT match inside ``15.30`` (next
       char is ``0``, digit). For non-digit word-ending keywords we keep
       the trailing ``\\b``.
    """
    escaped = re.escape(keyword)
    prefix = r'\b' if keyword and _WORD_RE.match(keyword[0]) else ''
    if not keyword:
        suffix = ''
    elif keyword[-1].isdigit():
        # Digit-ending: prevent matching as a substring of a longer number.
        suffix = r'(?![0-9])'
    elif _WORD_RE.match(keyword[-1]):
        suffix = r'\b'
    else:
        suffix = ''
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


def _kw_present(keyword: str, text: str) -> bool:
    """True if ``keyword`` appears in ``text`` as a whole word (or punctuation-bounded)."""
    return _kw_pattern(keyword).search(text) is not None


def _kw_first_index(keyword: str, text: str) -> int:
    """Position of first whole-word occurrence of ``keyword`` in ``text``, or -1."""
    m = _kw_pattern(keyword).search(text)
    return m.start() if m else -1


# Note-key validator (re-exported from sources for the InlineNote model).
# Single source of truth — see sources.NOTE_KEY_RE.
_NOTE_KEY_RE = NOTE_KEY_RE


# ---------------------------------------------------------------------------
# Setup actions (runner-interpreted DSL)
# ---------------------------------------------------------------------------


class InlineNote(BaseModel):
    """A note ingested as part of one specific scenario.

    Use this when a scenario needs source content that should NOT be in
    the suite's shared sources — e.g. a contradiction-detection scenario
    that ingests note A as a shared source, then declares an inline note
    that contradicts a claim in A and asserts the contradiction surfaces.

    The runner ingests inline notes after the suite-level sources are
    loaded but before the scenario's setup_actions and query run. The
    note is materialized in the same vault under the prefixed key
    ``inline-<scenario_id>-<note_key>``; ``GoldUnitIds`` may reference
    either the short ``note_key`` (resolved within the scenario) or the
    fully-prefixed form.

    Inline notes persist in the suite's vault for the rest of the run
    (vault-level cleanup happens at the end of run_suite). The prefixed
    note_key prevents collisions across scenarios.

    Limitations:
    - **No binary assets.** ``InlineNote`` carries only markdown text. If
      you need an image or other asset, define the note as a regular
      ``SourceNote`` under ``sources/`` with a per-note ``assets/`` subdir.
    - **Replicates >1**: the note is ingested once and cached; replicate 2
      sees the post-setup-action state, not a fresh baseline. See the
      how-to guide §1.3 for the contract.
    """

    note_key: str
    content: str
    title: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def _validate_note_key(self) -> InlineNote:
        if not _NOTE_KEY_RE.match(self.note_key):
            raise ValueError(
                f'InlineNote.note_key {self.note_key!r} must match {_NOTE_KEY_RE.pattern!r} '
                f'(same convention as SourceNote filename stems)'
            )
        return self


class SetupAction(BaseModel):
    """Pre-query side effect for a scenario.

    ``kind`` matches a registered ``SetupActionHandler`` (see
    ``memex_eval.suite.setup_actions.register_setup_action``). The
    runner dispatches by name and passes ``model_dump()`` to the
    handler — extra fields beyond the documented set are fine, so
    custom actions can carry arbitrary parameters.

    The legacy fields below stay for ergonomic IDE support; new actions
    are free to ignore them and add their own.
    """

    model_config = ConfigDict(extra='allow')

    kind: str
    # Unit-id resolution sources (checked in this priority order by
    # ``_resolve_unit_ids``):
    # 1. ``unit_ids`` — explicit UUIDs (most precise; rare in practice
    #    because eval vaults are freshly ingested per run).
    # 2. ``note_key`` — name of a SourceNote / InlineNote in the suite;
    #    resolves to all memory units extracted from that note via the
    #    runner's ``note_key_to_unit_ids`` map. **Deterministic** — the
    #    only fragility is whether extraction produced the expected
    #    units, which is testable elsewhere.
    # 3. ``search_query`` — top-5 results of a memory search. Legacy
    #    pattern; brittle (semantic drift can deprioritize the wrong
    #    units). Use ``note_key`` for new scenarios; keep
    #    ``search_query`` only when you genuinely want to test
    #    "deprioritize whatever the search engine considers most
    #    relevant" — almost never the right semantic for OUTCOMES_MW or
    #    DEPRIORITIZATION scenarios.
    note_key: str | None = None
    search_query: str | None = None
    unit_ids: list[str] | None = None
    success: bool = True
    reason: str | None = None
    kv_key: str | None = None
    kv_value: str | None = None
    count: int = 1


# ---------------------------------------------------------------------------
# Expected outcomes — registry-driven; external outcomes plug in via
# ``@register_outcome('<type-name>')`` without editing this file.
# ---------------------------------------------------------------------------


class ExpectedOutcomeBase(BaseModel):
    """Base for every registered ExpectedOutcome.

    Subclasses MUST set ``type: Literal['<name>']`` (the registry key) and
    implement ``score()``; ``metric_keys()`` and ``referenced_note_keys()``
    are optional. Outcomes are backend-agnostic — they consume whichever
    ``AgentAnswer`` fields the active backend populated.

    Register external outcomes via ``@register_outcome('<name>')``; the
    framework dispatches by the ``type`` field at validation time, so
    nothing in core has to change to add a new outcome.
    """

    type: str
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def score(
        self,
        answer: AgentAnswer,
        scenario: 'Scenario',
        *,
        note_key_to_unit_ids: dict[str, list[str]] | None = None,
        judge: Any | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        raise NotImplementedError

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        raise NotImplementedError

    def referenced_note_keys(self) -> set[str]:
        return set()


# ---------------------------------------------------------------------------
# Outcome registry — open-ended set, lookup by ``type`` discriminator.
# ---------------------------------------------------------------------------


_OUTCOME_REGISTRY: dict[str, type[ExpectedOutcomeBase]] = {}


def register_outcome(type_name: str):
    """Register an ``ExpectedOutcomeBase`` subclass under its discriminator.

    The subclass MUST declare ``type: Literal['<type_name>']`` so the
    Pydantic validator can re-construct it from a JSON dict. Custom
    outcomes ship in any importable module — load them once before
    invoking ``load_suite`` (e.g. via your suite package's ``__init__``).

    Example::

        @register_outcome('memory_worth_delta')
        class MemoryWorthDelta(ExpectedOutcomeBase):
            type: Literal['memory_worth_delta']
            target_keywords: list[str]
            min_delta: float
            def score(self, answer, scenario, *, context=None, **_kw):
                ...
    """

    if not _REGISTRY_NAME_RE.match(type_name):
        raise ValueError(f'Outcome type {type_name!r} must match {_REGISTRY_NAME_RE.pattern!r}')

    def deco(cls: type[ExpectedOutcomeBase]) -> type[ExpectedOutcomeBase]:
        existing = _OUTCOME_REGISTRY.get(type_name)
        if existing is not None and existing is not cls:
            raise ValueError(
                f'Outcome type {type_name!r} already registered to '
                f'{existing.__qualname__}. Use replace_outcome() to override.'
            )
        _OUTCOME_REGISTRY[type_name] = cls
        return cls

    return deco


def replace_outcome(type_name: str):
    """Like ``register_outcome`` but allows overriding an existing registration.

    Reserved for tests and intentional overrides; production code should use
    ``register_outcome``, which fails fast on collision.
    """

    if not _REGISTRY_NAME_RE.match(type_name):
        raise ValueError(f'Outcome type {type_name!r} must match {_REGISTRY_NAME_RE.pattern!r}')

    def deco(cls: type[ExpectedOutcomeBase]) -> type[ExpectedOutcomeBase]:
        if type_name in _OUTCOME_REGISTRY:
            logger.warning(
                'Replacing outcome %r (was %s, now %s)',
                type_name,
                _OUTCOME_REGISTRY[type_name].__qualname__,
                cls.__qualname__,
            )
        _OUTCOME_REGISTRY[type_name] = cls
        return cls

    return deco


def unregister_outcome(type_name: str) -> None:
    """Remove a registered outcome. Idempotent."""
    _OUTCOME_REGISTRY.pop(type_name, None)


def get_outcome_class(type_name: str) -> type[ExpectedOutcomeBase]:
    if type_name not in _OUTCOME_REGISTRY:
        raise KeyError(
            f'Unknown outcome type {type_name!r}. Registered: {sorted(_OUTCOME_REGISTRY)}'
        )
    return _OUTCOME_REGISTRY[type_name]


def list_outcomes() -> list[str]:
    return sorted(_OUTCOME_REGISTRY)


def _coerce_outcome(v: Any) -> ExpectedOutcomeBase:
    """BeforeValidator: turn dicts into the right registered subclass."""
    if isinstance(v, ExpectedOutcomeBase):
        return v
    if isinstance(v, dict):
        type_name = v.get('type')
        if not type_name:
            raise ValueError("ExpectedOutcome dict requires a 'type' discriminator")
        cls = _OUTCOME_REGISTRY.get(type_name)
        if cls is None:
            raise ValueError(
                f'Unknown outcome type {type_name!r}; registered: {sorted(_OUTCOME_REGISTRY)}'
            )
        return cls.model_validate(v)
    raise TypeError(f'Expected dict or ExpectedOutcomeBase, got {type(v).__name__}')


# SerializeAsAny is required: without it, Pydantic dumps only the base class's
# fields (just `type: str`) and silently drops every subclass-specific field
# (e.g. ``keywords``, ``note_keys``, ``rubric``). With SerializeAsAny, the
# concrete subclass's serializer is honored. Round-trip integrity is asserted
# in tests/suite/test_extensibility.py::TestOutcomeRegistry::test_round_trip.
ExpectedOutcomeUnion = SerializeAsAny[
    Annotated[ExpectedOutcomeBase, BeforeValidator(_coerce_outcome)]
]


def _aggregate_text(answer: AgentAnswer) -> str:
    """Return the lowercased text we should keyword-match against.

    Agent backends populate ``answer_text``; the API backend populates
    ``units`` whose joined text serves the same purpose.

    ``units`` may contain ``MemoryUnitDTO`` (memory search) OR ``NoteDTO``
    (note search via ``search_type='note'``). MemoryUnitDTO has ``.text``;
    NoteDTO does NOT — its searchable surface is ``title`` / ``name`` /
    ``description`` / ``original_text``. Aggregate every populated string
    field so a ``KeywordsPresent`` outcome over note-search results
    actually has something to match against.
    """
    if answer.answer_text:
        return answer.answer_text.lower()
    parts: list[str] = []
    for u in answer.units:
        # MemoryUnitDTO / NoteDTO scalar text fields.
        for fld in ('text', 'title', 'name', 'description', 'original_text'):
            val = getattr(u, fld, None)
            if val:
                parts.append(str(val))
        # NoteSearchResult exposes topic strings via summaries[] (each
        # BlockSummaryDTO has ``topic`` + ``key_points``) and a free-form
        # ``metadata`` dict that typically carries ``title``/``name``.
        for summary in getattr(u, 'summaries', None) or []:
            topic = getattr(summary, 'topic', None) or ''
            if topic:
                parts.append(str(topic))
            for kp in getattr(summary, 'key_points', None) or []:
                if kp:
                    parts.append(str(kp))
        meta = getattr(u, 'metadata', None)
        if isinstance(meta, dict):
            for key in ('title', 'name', 'description', 'note_title'):
                val = meta.get(key)
                if val:
                    parts.append(str(val))
    return ' '.join(parts).lower()


def _aggregate_unit_ids(answer: AgentAnswer) -> list[str]:
    """Return the retrieved unit IDs, preferring the explicit list."""
    if answer.retrieved_unit_ids:
        return list(answer.retrieved_unit_ids)
    return [str(getattr(u, 'id', '')) for u in answer.units if getattr(u, 'id', None)]


def _absorb_judge_usage(answer: AgentAnswer, judge: Any) -> None:
    """Drain token/cost since the last judge call into the answer object.

    The runner copies these into ScenarioOutcome and they aggregate into
    suite metrics ``tokens.total_in/out`` and ``cost.total_usd``.
    """
    consume = getattr(judge, 'consume_usage', None)
    if not callable(consume):
        return
    try:
        usage = consume()
    except Exception:
        return
    answer.tokens_in += int(usage.get('tokens_in', 0) or 0)
    answer.tokens_out += int(usage.get('tokens_out', 0) or 0)
    answer.cost_usd += float(usage.get('cost_usd', 0.0) or 0.0)


@register_outcome('keywords_present')
class KeywordsPresent(ExpectedOutcomeBase):
    type: Literal['keywords_present']
    keywords: list[str]

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        text = _aggregate_text(answer)
        all_present = all(_kw_present(kw, text) for kw in self.keywords)
        return {'pass': 1.0 if all_present else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('keywords_absent')
class KeywordsAbsent(ExpectedOutcomeBase):
    type: Literal['keywords_absent']
    keywords: list[str]

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        text = _aggregate_text(answer)
        none_present = not any(_kw_present(kw, text) for kw in self.keywords)
        return {'pass': 1.0 if none_present else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('entity_resolves')
class EntityResolves(ExpectedOutcomeBase):
    type: Literal['entity_resolves']
    expected_names: list[str]
    expected_type: str | None = None

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        names = {(getattr(e, 'name', '') or '').lower() for e in answer.entities}
        all_found = all(n.lower() in names for n in self.expected_names)
        if not all_found:
            return {'pass': 0.0}
        if self.expected_type:
            # EntityDTO field is `entity_type` (schemas.py:661); legacy
            # `type` lookup always returned None and silently failed every
            # type check. Probe both for forward compatibility.
            types: set[str] = set()
            for e in answer.entities:
                t = getattr(e, 'entity_type', None) or getattr(e, 'type', None)
                if t is not None:
                    types.add(str(t).lower())
            if self.expected_type.lower() not in types:
                return {'pass': 0.0}
        return {'pass': 1.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('entity_cooccurs')
class EntityCooccurs(ExpectedOutcomeBase):
    type: Literal['entity_cooccurs']
    expected_neighbors: list[str]

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        if not answer.entities:
            return {'pass': 0.0}
        # Server returns dicts with entity_1_name/entity_2_name (see
        # core/server/entities.py:222). The neighbor is whichever endpoint
        # is NOT the queried entity; fall back to both names if the queried
        # entity id is not on either side (shouldn't happen but defensive).
        queried_id = str(getattr(answer.entities[0], 'id', '') or '')
        names: set[str] = set()
        for c in answer.cooccurrences:
            if isinstance(c, dict):
                id1, id2 = str(c.get('entity_id_1') or ''), str(c.get('entity_id_2') or '')
                n1 = (c.get('entity_1_name') or '').lower()
                n2 = (c.get('entity_2_name') or '').lower()
            else:
                id1 = str(getattr(c, 'entity_id_1', '') or '')
                id2 = str(getattr(c, 'entity_id_2', '') or '')
                n1 = (getattr(c, 'entity_1_name', '') or '').lower()
                n2 = (getattr(c, 'entity_2_name', '') or '').lower()
            if queried_id and id1 == queried_id:
                if n2:
                    names.add(n2)
            elif queried_id and id2 == queried_id:
                if n1:
                    names.add(n1)
            else:
                if n1:
                    names.add(n1)
                if n2:
                    names.add(n2)
        expected = {n.lower() for n in self.expected_neighbors}
        return {'pass': 1.0 if expected.issubset(names) else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('gold_unit_ids')
class GoldUnitIds(ExpectedOutcomeBase):
    type: Literal['gold_unit_ids']
    note_keys: list[str]
    metrics_to_compute: list[Literal['recall_at_k', 'mrr', 'ndcg_at_k']] = Field(
        default_factory=lambda: list[Literal['recall_at_k', 'mrr', 'ndcg_at_k']](
            ['recall_at_k', 'mrr']
        )
    )

    def score(
        self,
        answer: AgentAnswer,
        scenario,
        *,
        note_key_to_unit_ids: dict[str, list[str]] | None = None,
        **_kw,
    ) -> dict[str, float]:
        if note_key_to_unit_ids is None:
            note_key_to_unit_ids = {}
        gold: list[str] = []
        for nk in self.note_keys:
            gold.extend(note_key_to_unit_ids.get(nk, []))
        retrieved = _aggregate_unit_ids(answer)
        out: dict[str, float] = {}
        if 'recall_at_k' in self.metrics_to_compute:
            out[f'recall_at_{scenario.top_k}'] = recall_at_k(retrieved, gold, scenario.top_k)
        if 'mrr' in self.metrics_to_compute:
            out['mrr'] = mrr(retrieved, gold)
        if 'ndcg_at_k' in self.metrics_to_compute:
            out[f'ndcg_at_{scenario.top_k}'] = ndcg_at_k(retrieved, gold, scenario.top_k)
        return out

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        keys: list[str] = []
        k_suffix = f'at_{top_k}' if top_k is not None else 'at_k'
        if 'recall_at_k' in self.metrics_to_compute:
            keys.append(f'recall_{k_suffix}')
        if 'mrr' in self.metrics_to_compute:
            keys.append('mrr')
        if 'ndcg_at_k' in self.metrics_to_compute:
            keys.append(f'ndcg_{k_suffix}')
        return keys

    def referenced_note_keys(self) -> set[str]:
        return set(self.note_keys)


@register_outcome('ranking_order')
class RankingOrder(ExpectedOutcomeBase):
    type: Literal['ranking_order']
    expected_keyword_order: list[str]

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        first_idx: dict[str, int] = {}
        # Use whichever sequence is populated.
        if answer.answer_text:
            text = answer.answer_text
            for kw in self.expected_keyword_order:
                idx = _kw_first_index(kw, text)
                if idx >= 0 and kw not in first_idx:
                    first_idx[kw] = idx
        else:
            for i, u in enumerate(answer.units):
                t = getattr(u, 'text', '') or ''
                for kw in self.expected_keyword_order:
                    if _kw_present(kw, t) and kw not in first_idx:
                        first_idx[kw] = i
        if any(kw not in first_idx for kw in self.expected_keyword_order):
            return {'pass': 0.0}
        ordered = [first_idx[kw] for kw in self.expected_keyword_order]
        return {'pass': 1.0 if ordered == sorted(ordered) else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('excluded_by_default')
class ExcludedByDefault(ExpectedOutcomeBase):
    type: Literal['excluded_by_default']
    forbidden_keywords: list[str]

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        text = _aggregate_text(answer)
        none_present = not any(_kw_present(kw, text) for kw in self.forbidden_keywords)
        return {'pass': 1.0 if none_present else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('temporal_ordering')
class TemporalOrdering(ExpectedOutcomeBase):
    """Datetime-level ordering assertion: the first-ranked unit per declared
    note_key must have ``mentioned_at`` (or ``occurred_start`` fallback)
    strictly newer than the next note's first-ranked unit.

    Avoids the brittle keyword-position check used by RankingOrder for
    temporal queries — extraction paraphrases quarter labels like ``Q2 2025``
    into absolute dates ``April-June 2025``, so a literal substring check
    fails even when the temporal strategy correctly ranks the newer
    document first. This outcome compares the actual datetimes the system
    has worked to normalise.
    """

    type: Literal['temporal_ordering']
    expected_note_keys_newest_first: list[str]

    def score(self, answer: AgentAnswer, scenario, *, context=None, **_kw) -> dict[str, float]:
        """Return pass/fail plus a retrieval-coverage metric.

        ``pass=1`` requires:
        - At least 2 of the expected notes are retrieved AND
        - For every consecutive pair where both are retrieved, the earlier
          one in the list has a strictly newer timestamp than the later one.

        ``notes_retrieved`` reports the fraction of expected notes that
        appeared in ``answer.units`` — surfaces "Q1 wasn't even retrieved"
        as distinct from "Q2 ranked below Q1".
        """
        nk2nid: dict[str, str] = (context or {}).get('_note_id_by_key') or {}
        first_ts: dict[str, dt.datetime] = {}
        for nk in self.expected_note_keys_newest_first:
            nid = nk2nid.get(nk)
            if nid is None:
                continue
            for u in answer.units:
                if str(getattr(u, 'note_id', None)) != str(nid):
                    continue
                ts = getattr(u, 'mentioned_at', None) or getattr(u, 'occurred_start', None)
                if ts is not None:
                    first_ts[nk] = ts
                    break

        retrieved = len(first_ts)
        expected = len(self.expected_note_keys_newest_first)
        notes_retrieved = retrieved / expected if expected else 0.0

        if retrieved == 0:
            return {'pass': 0.0, 'notes_retrieved': 0.0, 'pairs_compared': 0.0}

        # Compare every consecutive pair where BOTH are retrieved.
        pairs_compared = 0
        pairs_correct = 0
        for i in range(expected - 1):
            a, b = (
                self.expected_note_keys_newest_first[i],
                self.expected_note_keys_newest_first[i + 1],
            )
            if a in first_ts and b in first_ts:
                pairs_compared += 1
                if first_ts[a] > first_ts[b]:
                    pairs_correct += 1

        if pairs_compared == 0:
            # Only one expected note retrieved — recency pruning. If the
            # retrieved one is the FIRST (newest) in the expected list, the
            # older notes correctly didn't make the cut: that's the strongest
            # recency signal achievable. Pass.
            newest_expected = self.expected_note_keys_newest_first[0]
            passed = newest_expected in first_ts
            return {
                'pass': 1.0 if passed else 0.0,
                'notes_retrieved': notes_retrieved,
                'pairs_compared': 0.0,
            }

        all_correct = pairs_correct == pairs_compared
        return {
            'pass': 1.0 if all_correct else 0.0,
            'notes_retrieved': notes_retrieved,
            'pairs_compared': float(pairs_compared),
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'notes_retrieved', 'pairs_compared']


@register_outcome('has_contradiction_link')
class HasContradictionLink(ExpectedOutcomeBase):
    """Assert at least one returned unit contains ``newer_keyword`` AND has
    a ``superseded_by`` entry whose ``unit_text`` contains ``older_keyword``.

    Validates that contradiction-detection produced a structural link (the
    correct way to test "current vs. former" knowledge: the system has
    recorded an explicit edge), rather than relying on rank-order which is
    sensitive to scoring tuning.
    """

    type: Literal['has_contradiction_link']
    newer_keyword: str
    older_keyword: str
    relation: Literal['contradicts', 'weakens', 'supersedes', 'any'] = 'any'

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        for u in answer.units:
            text = getattr(u, 'text', '') or ''
            if not _kw_present(self.newer_keyword, text):
                continue
            superseded = getattr(u, 'superseded_by', None) or []
            for ss in superseded:
                rel = getattr(ss, 'relation', '') or ''
                if self.relation != 'any' and rel != self.relation:
                    continue
                ss_text = getattr(ss, 'unit_text', '') or ''
                if _kw_present(self.older_keyword, ss_text):
                    return {'pass': 1.0}
        return {'pass': 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('newest_unit_contains')
class NewestUnitContains(ExpectedOutcomeBase):
    """Assert the *newest* unit (max ``mentioned_at`` / ``occurred_start``)
    in the result set contains every keyword in ``keywords``.

    Pairs with HasContradictionLink for "current state" assertions: even if
    rank ordering is unstable, the most-recent unit is unambiguous and the
    agent / consumer can reason about it.
    """

    type: Literal['newest_unit_contains']
    keywords: list[str]
    # Optional subject filter. When set, only units whose text matches at
    # least one of these terms are eligible to be "the newest unit".
    # Use this when many notes ingest in the same minute (mentioned_at
    # falls back to ingest time without frontmatter dates), so the literal
    # newest unit may be from an unrelated note. With ``subject_filter``
    # the test asserts: among units relevant to ``subject_filter``, the
    # newest one contains ``keywords``.
    subject_filter: list[str] | None = None

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        if not answer.units:
            return {'pass': 0.0}
        eligible = answer.units
        if self.subject_filter:
            eligible = [
                u
                for u in answer.units
                if any(_kw_present(s, getattr(u, 'text', '') or '') for s in self.subject_filter)
            ]
            if not eligible:
                return {'pass': 0.0, 'subject_units': 0.0}
        units_with_ts: list[tuple[Any, Any]] = []
        for u in eligible:
            ts = getattr(u, 'mentioned_at', None) or getattr(u, 'occurred_start', None)
            if ts is not None:
                units_with_ts.append((u, ts))
        if not units_with_ts:
            return {'pass': 0.0, 'subject_units': float(len(eligible))}
        newest_unit = max(units_with_ts, key=lambda pair: pair[1])[0]
        text = getattr(newest_unit, 'text', '') or ''
        passed = all(_kw_present(kw, text) for kw in self.keywords)
        return {
            'pass': 1.0 if passed else 0.0,
            'subject_units': float(len(eligible)),
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'subject_units']


@register_outcome('note_attribution')
class NoteAttribution(ExpectedOutcomeBase):
    """Assert every unit sourced from ``top_note_key`` ranks higher than
    every unit sourced from ``lower_note_key``.

    Uses note_id attribution rather than text substrings — robust to
    extraction paraphrasing. The right primitive for "outcomes ranking"
    style scenarios where the assertion is "facts from doc A rank above
    facts from doc B after stamping outcomes."
    """

    type: Literal['note_attribution']
    top_note_key: str
    lower_note_key: str

    def score(self, answer: AgentAnswer, scenario, *, context=None, **_kw) -> dict[str, float]:
        """Pass when units from ``top_note_key`` rank, on average, above
        units from ``lower_note_key``.

        The check is mean-rank dominance, not strict partition. A single
        weak top-note unit at a low rank no longer fails the test. Pass
        also requires at least one top-note unit to outrank at least one
        lower-note unit (``min(top_ranks) < max(low_ranks)``) — guards
        against a fluke where every top unit happens to land above every
        low unit but the means tied.

        Surfaced metrics:
        - ``top_mean_rank`` — average rank of top-note units (lower = better)
        - ``low_mean_rank`` — average rank of lower-note units
        - ``top_count`` / ``low_count`` — how many of each were retrieved
        """
        nk2nid: dict[str, str] = (context or {}).get('_note_id_by_key') or {}
        top_nid = nk2nid.get(self.top_note_key)
        low_nid = nk2nid.get(self.lower_note_key)
        if top_nid is None or low_nid is None:
            return {'pass': 0.0}
        top_ranks = [
            i
            for i, u in enumerate(answer.units)
            if str(getattr(u, 'note_id', None)) == str(top_nid)
        ]
        low_ranks = [
            i
            for i, u in enumerate(answer.units)
            if str(getattr(u, 'note_id', None)) == str(low_nid)
        ]
        if not top_ranks or not low_ranks:
            return {
                'pass': 0.0,
                'top_count': float(len(top_ranks)),
                'low_count': float(len(low_ranks)),
            }
        top_mean = sum(top_ranks) / len(top_ranks)
        low_mean = sum(low_ranks) / len(low_ranks)
        # Mean-rank dominance: lower mean = ranks closer to top.
        ok = top_mean < low_mean and min(top_ranks) < max(low_ranks)
        return {
            'pass': 1.0 if ok else 0.0,
            'top_mean_rank': float(top_mean),
            'low_mean_rank': float(low_mean),
            'top_count': float(len(top_ranks)),
            'low_count': float(len(low_ranks)),
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'top_mean_rank', 'low_mean_rank', 'top_count', 'low_count']


@register_outcome('llm_judge')
class LLMJudge(ExpectedOutcomeBase):
    """Graded-correctness judge.

    ``rubric`` is passed to the judge as the *expected answer description*
    (the ``GradedCorrectness`` DSPy signature treats it as the ground-truth
    target). Phrase it as a self-contained answer / acceptance criterion,
    not as instructions to the model. Examples:

    - GOOD: "The result must identify Sarah Chen as the lead of Project Alpha."
    - LESS GOOD: "Score 1 if the answer mentions Sarah Chen; 0 otherwise."
    """

    type: Literal['llm_judge']
    rubric: str
    threshold: float = 0.75
    # How many top-k unit texts to concatenate into the judge candidate
    # when no agent ``answer_text`` is available. Default 5 because
    # multi-fact rubrics ("must mention A AND B AND C AND D") cannot be
    # satisfied by a single unit; the judge needs the synthesised view.
    # Set to 1 explicitly when you really do want top-1-only evaluation.
    candidate_top_k: int = 5

    def score(
        self,
        answer: AgentAnswer,
        scenario,
        *,
        judge: Any | None = None,
        **_kw,
    ) -> dict[str, float]:
        if judge is None:
            return {'graded_score': 0.0, 'pass': 0.0, 'skipped': 1.0}
        # Prefer agent's final answer (single coherent narrative). When
        # absent, concatenate top-k unit texts so multi-fact rubrics are
        # judged on the union of evidence, not a single unit.
        candidate = answer.answer_text
        if not candidate and answer.units:
            top_units = answer.units[: max(1, self.candidate_top_k)]
            candidate = '\n---\n'.join((getattr(u, 'text', '') or '') for u in top_units)
        if not candidate:
            return {'graded_score': 0.0, 'pass': 0.0}
        score, _reasoning = judge.judge_graded_correctness(scenario.query, self.rubric, candidate)
        _absorb_judge_usage(answer, judge)
        return {
            'graded_score': float(score),
            'pass': 1.0 if float(score) >= self.threshold else 0.0,
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['graded_score', 'pass']


@register_outcome('useful_at_k')
class UsefulAtK(ExpectedOutcomeBase):
    type: Literal['useful_at_k']
    rubric: str
    k: int = 5
    threshold: float = 0.5

    def score(
        self,
        answer: AgentAnswer,
        scenario,
        *,
        judge: Any | None = None,
        **_kw,
    ) -> dict[str, float]:
        # Score against retrieved units; for agent backends fallback to
        # judging the answer text once.
        if judge is None:
            return {f'useful_at_{self.k}': 0.0, 'pass': 0.0}
        if answer.units:
            useful = 0
            considered = answer.units[: self.k]
            for u in considered:
                text = getattr(u, 'text', '') or ''
                is_relevant, _ = judge.judge_relevance(scenario.query, self.rubric, text)
                if is_relevant:
                    useful += 1
            ratio = useful / max(len(considered), 1) if considered else 0.0
        elif answer.answer_text:
            is_relevant, _ = judge.judge_relevance(scenario.query, self.rubric, answer.answer_text)
            ratio = 1.0 if is_relevant else 0.0
        else:
            ratio = 0.0
        _absorb_judge_usage(answer, judge)
        return {
            f'useful_at_{self.k}': ratio,
            'pass': 1.0 if ratio >= self.threshold else 0.0,
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return [f'useful_at_{self.k}', 'pass']


@register_outcome('lint_finding_present')
class LintFindingPresent(ExpectedOutcomeBase):
    type: Literal['lint_finding_present']
    expected_rule_name: str

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        for f in answer.lint_findings:
            if getattr(f, 'rule_name', None) == self.expected_rule_name:
                return {'pass': 1.0}
        return {'pass': 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('llm_lint_flags_unit')
class LLMLintFlagsUnit(ExpectedOutcomeBase):
    """Assert lint findings collectively cover ``target_keywords``.

    A contradiction lives across ≥2 units by construction (one says X,
    the other says Y). Requiring all keywords to appear in a single
    finding's ``unit_text`` is unsatisfiable. The default semantics
    therefore check that EACH keyword appears in *some* finding's
    ``unit_text``. Set ``match_mode='same_finding'`` to require all
    keywords in one finding (legacy behaviour).
    """

    type: Literal['llm_lint_flags_unit']
    target_keywords: list[str]
    match_mode: Literal['across_findings', 'same_finding'] = 'across_findings'

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        if self.match_mode == 'same_finding':
            for f in answer.lint_findings:
                target = (getattr(f, 'unit_text', '') or '').lower()
                if all(kw.lower() in target for kw in self.target_keywords):
                    return {'pass': 1.0, 'keywords_found': float(len(self.target_keywords))}
            return {'pass': 0.0, 'keywords_found': 0.0}
        # across_findings: each keyword need only appear in some finding.
        seen: set[str] = set()
        for f in answer.lint_findings:
            target = (getattr(f, 'unit_text', '') or '').lower()
            for kw in self.target_keywords:
                if kw.lower() in target:
                    seen.add(kw.lower())
        passed = len(seen) == len(self.target_keywords)
        return {
            'pass': 1.0 if passed else 0.0,
            'keywords_found': float(len(seen)),
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'keywords_found']


@register_outcome('entity_mention_contains')
class EntityMentionContains(ExpectedOutcomeBase):
    """Resolve an entity, then assert at least one of its mentions contains
    the expected keywords.

    Walks: ``search_entities(expected_name or scenario.query) → take top
    hit → get_entity_mentions(entity_id) → join unit text → keyword scan``.
    Used for "this entity is grounded in source content X" assertions
    that are stronger than name-only resolution.
    """

    type: Literal['entity_mention_contains']
    expected_name: str = ''
    expected_keywords: list[str]
    min_mentions: int = 1

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        if len(answer.entity_mentions) < self.min_mentions:
            return {'pass': 0.0}
        joined: list[str] = []
        for m in answer.entity_mentions:
            unit = m.get('unit') if isinstance(m, dict) else getattr(m, 'unit', None)
            if unit is None:
                # Fallback: maybe the mention IS the unit (DTO shape varies).
                unit = m
            text = ''
            if isinstance(unit, dict):
                text = str(unit.get('text', '') or '')
            else:
                text = str(getattr(unit, 'text', '') or '')
            if text:
                joined.append(text)
        blob = '\n'.join(joined).lower()
        if all(kw.lower() in blob for kw in self.expected_keywords):
            return {'pass': 1.0}
        return {'pass': 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('kv_roundtrip')
class KvRoundtrip(ExpectedOutcomeBase):
    """Assert that a KV key returns the expected value.

    The write happens via the ``kv_write`` setup_action; this outcome
    only reads (``api.kv_get``) and compares. Together they exercise the
    full write→read cycle without coupling write logic into the outcome.
    """

    type: Literal['kv_roundtrip']
    kv_key: str
    expected_value: str

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        if answer.kv_value is None:
            return {'pass': 0.0}
        return {'pass': 1.0 if answer.kv_value == self.expected_value else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('summary_nonempty')
class SummaryNonempty(ExpectedOutcomeBase):
    """Resolve an entity, call ``summarize_node``, assert non-empty summary.

    The ``min_chars`` field lets a scenario require more than a stub
    response (e.g. ``min_chars=50`` to catch summaries that returned
    a one-word "Acme" rather than a full sentence).
    """

    type: Literal['summary_nonempty']
    entity_query: str = ''
    min_chars: int = 1

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        text = (answer.summary_text or '').strip()
        return {
            'pass': 1.0 if len(text) >= self.min_chars else 0.0,
            'summary_chars': float(len(text)),
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'summary_chars']


@register_outcome('unit_metadata_matches')
class UnitMetadataMatches(ExpectedOutcomeBase):
    """Assert at least one returned memory unit matches every
    ``expected_metadata`` (key, value) pair.

    Lookup order per key:
      1. ``unit.metadata[key]`` (the dict-shaped metadata blob).
      2. ``getattr(unit, key)`` — top-level DTO fields like
         ``intent_class``, ``risk_class``, ``fact_type``, ``status``
         live here, NOT in the metadata dict. Without this fallback a
         scenario asking for ``intent_class='ephemeral'`` would always
         fail because that key never appears under ``.metadata``.

    Comparison is string-coerced (Enum values stringify to their
    ``.value``) so callers can write plain string expectations.
    """

    type: Literal['unit_metadata_matches']
    expected_metadata: dict[str, str]

    @staticmethod
    def _coerce(value: Any) -> str:
        if value is None:
            return ''
        inner = getattr(value, 'value', None)
        return str(inner if inner is not None else value)

    def _matches(self, unit: Any) -> bool:
        meta = getattr(unit, 'metadata', None) or {}
        for key, expected in self.expected_metadata.items():
            actual: Any = None
            if isinstance(meta, dict) and key in meta:
                actual = meta[key]
            else:
                actual = getattr(unit, key, None)
            if self._coerce(actual) != self._coerce(expected):
                return False
        return True

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        if not answer.units:
            return {'pass': 0.0}
        for unit in answer.units:
            if self._matches(unit):
                return {'pass': 1.0}
        return {'pass': 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('tool_call_contains')
class ToolCallContains(ExpectedOutcomeBase):
    """Agent-mode outcome: assert the agent called specific MCP tool(s).

    Useful for verifying integration patterns — e.g. "the agent must
    call ``memex_memory_search`` at least once before answering."
    Direct-API backend trivially fails this (no tool calls happen).

    ``match_mode`` controls how ``expected_tools`` is interpreted:

    - ``'all'`` (default): every listed tool must be called ``min_count``
      times. Use when each tool fills a distinct role and skipping any
      one of them indicates a real integration regression.
    - ``'any'``: at least ONE listed tool must be called ``min_count``
      times. Use when several tools satisfy the same need (e.g.
      ``memex_memory_search`` OR ``memex_note_search`` to discover a
      fact) — agents legitimately pick one route per question, and
      ``'all'`` would over-constrain.
    """

    type: Literal['tool_call_contains']
    expected_tools: list[str]
    min_count: int = 1
    match_mode: Literal['all', 'any'] = 'all'

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        seen = [c.get('tool', '') for c in answer.tool_calls]
        if self.match_mode == 'any':
            for expected in self.expected_tools:
                if sum(1 for t in seen if t == expected) >= self.min_count:
                    return {'pass': 1.0}
            return {'pass': 0.0}
        for expected in self.expected_tools:
            if sum(1 for t in seen if t == expected) < self.min_count:
                return {'pass': 0.0}
        return {'pass': 1.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('composite')
class CompositeOutcome(ExpectedOutcomeBase):
    """Bundle multiple child outcomes; metric keys are prefixed by index."""

    type: Literal['composite']
    children: list['ExpectedOutcomeUnion']

    def score(self, answer: AgentAnswer, scenario, **kw) -> dict[str, float]:
        out: dict[str, float] = {}
        all_pass = True
        # Single pass — calling each child's score() twice would double-bill any
        # LLM judges and risk inconsistent pass/fail across the two calls.
        for i, child in enumerate(self.children):
            child_metrics = child.score(answer, scenario, **kw)
            for k, v in child_metrics.items():
                out[f'child{i}.{k}'] = v
            # Mirror _execute_scenario's pass/fail derivation: explicit
            # ``pass`` key wins; otherwise ``any(v > 0)``. Without this, a
            # child that emits ``{'recall_at_5': 0.0}`` (no ``pass`` key)
            # silently passes inside a composite even though it would fail
            # standalone.
            if 'pass' in child_metrics:
                child_passed = child_metrics['pass'] >= 1.0
            else:
                child_passed = any(v > 0 for v in child_metrics.values())
            if not child_passed:
                all_pass = False
        out['pass'] = 1.0 if all_pass else 0.0
        return out

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        out: list[str] = ['pass']
        for i, child in enumerate(self.children):
            for k in child.metric_keys(top_k=top_k):
                out.append(f'child{i}.{k}')
        return out

    def referenced_note_keys(self) -> set[str]:
        out: set[str] = set()
        for child in self.children:
            out.update(child.referenced_note_keys())
        return out


CompositeOutcome.model_rebuild()


# ---------------------------------------------------------------------------
# Scenario / Suite
# ---------------------------------------------------------------------------


_SCENARIO_ID_RE = re.compile(r'^[a-z0-9_]+$')


class Scenario(BaseModel):
    """One verifiable assertion against memex behavior."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    description: str
    query: str
    expected: ExpectedOutcomeUnion
    top_k: int = 10
    strategies: list[str] | None = None
    include_superseded: bool | None = None
    include_deprioritized: bool | None = None
    setup_actions: list[SetupAction] = Field(default_factory=list)
    inline_notes: list[InlineNote] = Field(default_factory=list)
    vault_name: str | None = None
    max_duration_ms: float | None = None
    search_type: Literal['memory', 'note'] = 'memory'

    # Pluggable answer-generation backend. Defaults to inheriting from the
    # suite-level default; fall back to 'api' if neither is set.
    answer_mode: str | None = None

    # pytest-style xfail. When the active answer_mode is in this list, a
    # scenario fail is reported as ``xfail`` (expected failure → counted as
    # success in pass_rate) and an unexpected pass becomes ``xpass`` (counted
    # as failure — the constraint embedded here is wrong / now stale).
    # Example: ``ToolCallContains`` is unsatisfiable in api mode, so
    # ``expected_failure_modes=['api']`` keeps the scenario visible and
    # measurable rather than hiding it behind a skip.
    expected_failure_modes: list[str] = Field(default_factory=list)

    # P7: scenarios that depend on the NLI polarity classifier set this
    # flag. The runner gates such scenarios by reading
    # ``server.memory.lint_llm.polarity.enabled`` from /system/config; if
    # NLI is disabled or the polarity backend is 'disabled', the scenario
    # gets status='skip' with skip_reason='nli_disabled'. Suite-level
    # SuiteMetadata.requires_nli_classifier propagates the same effect
    # to every scenario in the suite.
    requires_nli_classifier: bool = False

    @model_validator(mode='after')
    def _validate_id(self) -> Scenario:
        if not _SCENARIO_ID_RE.match(self.id):
            raise ValueError(f'Scenario id {self.id!r} must match ^[a-z0-9_]+$')
        return self


class SuiteMetadata(BaseModel):
    """Top-of-suite metadata; logged as MLflow params and tags."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    schema_version: Literal['1'] = '1'
    suite_version: str
    description: str
    tags: list[str] = Field(default_factory=list)
    primary_metrics: list[str] = Field(default_factory=list)
    components_under_test: list[str] = Field(default_factory=list)
    knobs: list[str] = Field(default_factory=list)
    requires_llm_judge: bool = False
    requires_postgres: bool = True

    # P7: when True, every scenario in the suite is gated on NLI polarity
    # classifier availability (in addition to any per-Scenario override).
    # Used by suites that exercise the LLM-gated lint pass.
    requires_nli_classifier: bool = False

    # Suite-level default for Scenario.answer_mode. Per-scenario overrides win.
    default_answer_mode: str = 'api'

    @model_validator(mode='after')
    def _validate_name(self) -> SuiteMetadata:
        if not _SCENARIO_ID_RE.match(self.name):
            raise ValueError(f'Suite name {self.name!r} must match ^[a-z0-9_]+$')
        return self


class Suite(BaseModel):
    metadata: SuiteMetadata
    sources: SuiteSources
    scenarios: list[Scenario]
    readme_path: Path | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode='after')
    def _validate_referential_integrity(self) -> Suite:
        source_keys = self.sources.note_keys
        scenario_ids: set[str] = set()
        # Best-effort: pull live lint rule names from memex_core if available.
        # Two rulesets co-exist by design (see CHANGELOG / docs):
        #   - V1_RULES (services/lint.py): cheap structural rules, run inline.
        #   - LLM rules (memory/lint_llm/checks.py + services/lint_llm.py):
        #     LLM-gated semantic rules, run on a separate quota-limited queue.
        # The validator accepts names from either set so suites can assert
        # against whichever rule actually fires for the corpus shape.
        valid_rule_names: set[str] | None = None
        try:
            from memex_core.services.lint import V1_RULES

            valid_rule_names = {r.name for r in V1_RULES}
        except (ImportError, AttributeError):
            valid_rule_names = None
        try:
            from memex_core.memory.lint_llm import checks as _llm_checks
            from memex_core.services import lint_llm as _llm_services

            llm_names: set[str] = set()
            for mod in (_llm_checks, _llm_services):
                for attr in dir(mod):
                    if attr.startswith('_RULE_LLM'):
                        val = getattr(mod, attr)
                        if isinstance(val, str):
                            llm_names.add(val)
            if llm_names:
                valid_rule_names = (valid_rule_names or set()) | llm_names
        except (ImportError, AttributeError):
            pass

        def _walk(outcome: Any, depth: int = 0, visited: set[int] | None = None) -> None:
            if depth > 4:
                raise ValueError(
                    f'Composite outcome nesting exceeds depth 4 in suite {self.metadata.name!r}'
                )
            if visited is None:
                visited = set()
            if id(outcome) in visited:
                raise ValueError(
                    f'Composite outcome cycle detected in suite {self.metadata.name!r}'
                )
            visited = visited | {id(outcome)}
            if isinstance(outcome, LintFindingPresent) and valid_rule_names is not None:
                if outcome.expected_rule_name not in valid_rule_names:
                    raise ValueError(
                        f'Scenario references lint rule {outcome.expected_rule_name!r} '
                        f'not in V1_RULES; valid: {sorted(valid_rule_names)}'
                    )
            if isinstance(outcome, CompositeOutcome):
                for child in outcome.children:
                    _walk(child, depth + 1, visited)

        for sc in self.scenarios:
            if sc.id in scenario_ids:
                raise ValueError(f'Duplicate scenario_id {sc.id!r} in suite {self.metadata.name}')
            scenario_ids.add(sc.id)
            referenced = sc.expected.referenced_note_keys()
            # An outcome may reference either a suite-level source note_key or
            # one of THIS scenario's inline_notes (by short or prefixed key).
            inline_short = {n.note_key for n in sc.inline_notes}
            inline_prefixed = {f'inline-{sc.id}-{k}' for k in inline_short}
            available = source_keys | inline_short | inline_prefixed
            missing = referenced - available
            if missing:
                raise ValueError(
                    f'Scenario {sc.id!r} references note_keys {sorted(missing)} '
                    f'not present in suite {self.metadata.name!r} sources or its '
                    f'own inline_notes.'
                )
            _walk(sc.expected)
        return self

    @property
    def name(self) -> str:
        return self.metadata.name

    def answer_mode_for(self, scenario: Scenario) -> str:
        """Resolve the active answer-backend name for a scenario."""
        return scenario.answer_mode or self.metadata.default_answer_mode or 'api'


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class ScenarioOutcome(BaseModel):
    scenario_id: str
    status: Literal['pass', 'fail', 'skip', 'error', 'xfail', 'xpass']
    metrics: dict[str, float] = Field(default_factory=dict)
    actual_summary: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    error: str | None = None
    replicate_index: int = 0
    answer_mode: str = 'api'
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    answer_text: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    # Loud-skip reason. Free-form string; conventional values are
    # 'nli_disabled' (P7 — only emitted when /system/config IS readable
    # and explicitly reports polarity disabled), 'setup_action_not_reusable' (P8).
    skip_reason: str | None = None


class RunResult(BaseModel):
    suite_name: str
    suite_version: str
    schema_version: str
    run_id: str
    started_at: dt.datetime
    finished_at: dt.datetime
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    config_overrides: dict[str, str] = Field(default_factory=dict)
    sources_hash: str = ''
    git_sha: str = ''
    git_branch: str = ''
    memex_version: str = ''
    judge_model: str | None = None
    judge_model_probe: dict[str, Any] | None = None
    seed: int = 0
    embedding_model: str = ''
    reranker_model: str = ''
    vault_name: str = ''
    answer_modes: list[str] = Field(default_factory=list)
    replicates: int = 1
    notes: str | None = None  # Free-form change notes (uploaded as MLflow artifact)
    scenario_outcomes: list[ScenarioOutcome] = Field(default_factory=list)
    suite_metrics: dict[str, float] = Field(default_factory=dict)
    note_key_to_unit_ids: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def total_passed(self) -> int:
        return sum(1 for o in self.scenario_outcomes if o.status == 'pass')

    @property
    def total_failed(self) -> int:
        return sum(1 for o in self.scenario_outcomes if o.status == 'fail')

    @property
    def total_errored(self) -> int:
        return sum(1 for o in self.scenario_outcomes if o.status == 'error')

    @property
    def total_skipped(self) -> int:
        return sum(1 for o in self.scenario_outcomes if o.status == 'skip')

    @property
    def total_xfailed(self) -> int:
        return sum(1 for o in self.scenario_outcomes if o.status == 'xfail')

    @property
    def total_xpassed(self) -> int:
        return sum(1 for o in self.scenario_outcomes if o.status == 'xpass')

    @property
    def overall_pass_rate(self) -> float:
        # Mirrors _aggregate_results: numerator = pass + xfail; denominator =
        # every scenario that produced a verdict (pass + fail + xfail + xpass).
        verdict_total = (
            self.total_passed + self.total_failed + self.total_xfailed + self.total_xpassed
        )
        return (self.total_passed + self.total_xfailed) / verdict_total if verdict_total else 0.0


__all__ = [
    'AgentAnswer',
    'SetupAction',
    'InlineNote',
    'ExpectedOutcomeBase',
    'register_outcome',
    'replace_outcome',
    'unregister_outcome',
    'get_outcome_class',
    'list_outcomes',
    'KeywordsPresent',
    'KeywordsAbsent',
    'EntityResolves',
    'EntityCooccurs',
    'GoldUnitIds',
    'RankingOrder',
    'ExcludedByDefault',
    'LLMJudge',
    'UsefulAtK',
    'LintFindingPresent',
    'LLMLintFlagsUnit',
    'ToolCallContains',
    'CompositeOutcome',
    'ExpectedOutcomeUnion',
    'Scenario',
    'SuiteMetadata',
    'Suite',
    'ScenarioOutcome',
    'RunResult',
    'SourceNote',
    'SuiteSources',
]
