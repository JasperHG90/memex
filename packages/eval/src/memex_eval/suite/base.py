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
    """
    if answer.answer_text:
        return answer.answer_text.lower()
    parts: list[str] = []
    for u in answer.units:
        text = getattr(u, 'text', '') or ''
        if text:
            parts.append(text)
    return ' '.join(parts).lower()


def _aggregate_unit_ids(answer: AgentAnswer) -> list[str]:
    """Return the retrieved unit IDs, preferring the explicit list."""
    if answer.retrieved_unit_ids:
        return list(answer.retrieved_unit_ids)
    return [str(getattr(u, 'id', '')) for u in answer.units if getattr(u, 'id', None)]


@register_outcome('keywords_present')
class KeywordsPresent(ExpectedOutcomeBase):
    type: Literal['keywords_present']
    keywords: list[str]

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        text = _aggregate_text(answer)
        all_present = all(kw.lower() in text for kw in self.keywords)
        return {'pass': 1.0 if all_present else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


@register_outcome('keywords_absent')
class KeywordsAbsent(ExpectedOutcomeBase):
    type: Literal['keywords_absent']
    keywords: list[str]

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        text = _aggregate_text(answer)
        none_present = not any(kw.lower() in text for kw in self.keywords)
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
            types = {str(getattr(e, 'type', None) or '').lower() for e in answer.entities}
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
        names = {(getattr(c, 'name', '') or '').lower() for c in answer.cooccurrences}
        all_found = all(n.lower() in names for n in self.expected_neighbors)
        return {'pass': 1.0 if all_found else 0.0}

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
                idx = text.lower().find(kw.lower())
                if idx >= 0 and kw not in first_idx:
                    first_idx[kw] = idx
        else:
            for i, u in enumerate(answer.units):
                t = (getattr(u, 'text', '') or '').lower()
                for kw in self.expected_keyword_order:
                    if kw.lower() in t and kw not in first_idx:
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
        none_present = not any(kw.lower() in text for kw in self.forbidden_keywords)
        return {'pass': 1.0 if none_present else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass']


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
        # Prefer agent's final answer; fall back to top-1 unit text.
        candidate = answer.answer_text
        if not candidate and answer.units:
            candidate = getattr(answer.units[0], 'text', '') or ''
        if not candidate:
            return {'graded_score': 0.0, 'pass': 0.0}
        score, _reasoning = judge.judge_graded_correctness(scenario.query, self.rubric, candidate)
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
    type: Literal['llm_lint_flags_unit']
    target_keywords: list[str]

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        for f in answer.lint_findings:
            target = (getattr(f, 'unit_text', '') or '').lower()
            if all(kw.lower() in target for kw in self.target_keywords):
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
    """

    type: Literal['tool_call_contains']
    expected_tools: list[str]
    min_count: int = 1

    def score(self, answer: AgentAnswer, scenario, **_kw) -> dict[str, float]:
        seen = [c.get('tool', '') for c in answer.tool_calls]
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
        # If memex_core isn't installed, skip the check (non-fatal).
        valid_rule_names: set[str] | None = None
        try:
            from memex_core.services.lint import V1_RULES

            valid_rule_names = {r.name for r in V1_RULES}
        except (ImportError, AttributeError):
            valid_rule_names = None

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
    status: Literal['pass', 'fail', 'skip', 'error']
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
    def overall_pass_rate(self) -> float:
        runnable = len(self.scenario_outcomes) - self.total_skipped
        return self.total_passed / runnable if runnable else 0.0


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
