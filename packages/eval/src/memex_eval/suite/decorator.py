"""Decorator-based authoring surface for evaluation suites.

A thin sugar layer over the existing primitives (``Scenario``,
``ExpectedOutcomeBase``, ``SetupAction``, ``InlineNote``,
``SuiteMetadata``, ``SuiteSources``). Three surfaces, increasing power:

1. ``suite.register(...)`` — pure declarative method call.
   Best for the 95% case: pick an existing ``ExpectedOutcome``, declare
   the scenario, done.

2. ``@suite.scenario(...)`` — decorator whose function body REPLACES
   ``expected``. The function is called from a synthetic ``CustomEvaluate``
   outcome; it mutates ``ScenarioContext.metrics`` in place. Use this when
   no built-in outcome class fits and you want to write a Python
   assertion inline.

3. ``@suite.register_class`` — class-based decorator on a
   ``BaseScenario`` subclass (or pre-instantiated instance) for full
   lifecycle override. Each phase
   (``setup`` / ``act`` / ``evaluate`` / ``teardown``) is overrideable
   independently; ``super().<phase>(ctx)`` always reaches the existing
   machinery (setup-action registry, backend dispatch, ``expected.score``,
   per-action teardowns + inline-note delete). Skip ``super`` only when
   you intend to bypass the default.

All three converge on the same ``Scenario`` Pydantic model fed to the
existing runner; the decorator authoring layer never replaces the
typed-model substrate.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Literal,
    Sequence,
)

from pydantic import ConfigDict, Field

logger = logging.getLogger(__name__)

from memex_eval.suite.base import (
    ExpectedOutcomeBase,
    ExpectedOutcomeUnion,
    InlineNote,
    Scenario,
    SetupAction,
    Suite as _LegacySuite,
    SuiteMetadata,
    SuiteSources,
    register_outcome,
)

if TYPE_CHECKING:
    from memex_eval.suite.agents import AgentAnswer


# ---------------------------------------------------------------------------
# ScenarioContext — the dict that flows through the lifecycle.
# ---------------------------------------------------------------------------


@dataclass
class ScenarioContext:
    """State threaded through ``setup → query → evaluate → teardown``.

    Kept intentionally narrow (no ``ctx.db``, no ``ctx.temp_user_id``).
    Complex state lives on the BaseScenario subclass instance (``self.x``).
    """

    # Inputs (populated before lifecycle runs).
    query: str
    scenario: Scenario
    api: Any  # RemoteMemexAPI; typed loosely so this module stays import-light
    vault_id: Any  # UUID
    server_url: str
    judge: Any | None = None

    # Filled by the lifecycle.
    answer: 'AgentAnswer | None' = None
    metrics: dict[str, float] = field(default_factory=dict)

    # Plumbing the runner already builds — handed through unchanged.
    note_key_to_unit_ids: dict[str, list[str]] = field(default_factory=dict)
    note_id_by_key: dict[str, str] = field(default_factory=dict)
    setup_context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# CustomEvaluate — outcome class that wraps a user function.
# ---------------------------------------------------------------------------


# The wrapped function may be sync or async; either signature is fine.
EvaluatorFn = Callable[[ScenarioContext], Any]


def _coerce_numeric(raw: dict[str, Any] | None) -> dict[str, float]:
    """Filter ``raw`` to ``dict[str, float]``, dropping any value that
    can't be converted to float. The runner's ``ScenarioOutcome.metrics``
    is strictly typed; a stray string in ``ctx.metrics`` would otherwise
    crash Pydantic validation downstream and erase the user's verdict."""
    if not raw:
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            logger.warning(
                'discarding non-numeric ctx.metrics[%r]=%r — '
                'ScenarioOutcome.metrics requires float values',
                k,
                v,
            )
    return out


@register_outcome('custom_evaluate')
class CustomEvaluate(ExpectedOutcomeBase):
    """Outcome whose score is delegated to a user function.

    Wraps a ``Callable[[ScenarioContext], None | Awaitable[None]]``. The
    function mutates ``ctx.metrics`` in place. ``AssertionError`` is
    converted to ``ctx.metrics = {'pass': 0.0}`` so the runner records
    ``status='fail'`` (the natural reading of a failed assert in an
    eval). Any *other* exception propagates and the runner records
    ``status='error'`` with the traceback.

    score() runs sync. If the user wrote an ``async def`` evaluator,
    score() drives it with ``asyncio.run`` only when no event loop is
    running; if a loop IS running, score() raises — the runner detects
    this case at dispatch time (``runner._invoke_custom_evaluate_async``)
    and awaits the function directly. The class-based ``@suite.register_class``
    API also routes async evaluators through ``BaseScenario.evaluate``,
    which the runner awaits natively.
    """

    type: Literal['custom_evaluate'] = 'custom_evaluate'

    # Inherit ``arbitrary_types_allowed=True`` from ExpectedOutcomeBase.
    # We override only to declare the same flag explicitly for clarity;
    # we deliberately do NOT set ``extra='allow'`` so unknown fields are
    # rejected at construction (catches typoed kwargs at suite-build time).
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ``fn`` is a closure, not JSON-serializable — exclude from any
    # ``model_dump()`` so the outer suite-snapshot pipeline doesn't choke
    # serializing this outcome.
    fn: EvaluatorFn = Field(exclude=True)
    # Optional human-readable label for ``metric_keys()`` reporting; the
    # default is whatever metric keys the user populated in ``ctx.metrics``.
    declared_metric_keys: list[str] | None = None

    def score(
        self,
        answer: 'AgentAnswer',
        scenario: 'Scenario',
        *,
        note_key_to_unit_ids: dict[str, list[str]] | None = None,
        judge: Any | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        ctx = ScenarioContext(
            query=scenario.query,
            scenario=scenario,
            api=(context or {}).get('_api'),
            vault_id=(context or {}).get('_vault_id'),
            server_url=(context or {}).get('_server_url', ''),
            judge=judge,
            answer=answer,
            note_key_to_unit_ids=dict(note_key_to_unit_ids or {}),
            note_id_by_key=dict((context or {}).get('_note_id_by_key') or {}),
            setup_context=dict(context or {}),
        )
        try:
            result = self.fn(ctx)
            if inspect.isawaitable(result):
                # User declared the function ``async def``. We block on it
                # here because score() is sync by contract; this is safe
                # when score() is called outside any event loop. If score()
                # IS called from within a running loop, refuse — running
                # ``loop.run_until_complete`` on the same loop would
                # deadlock. The runner's async dispatch path
                # (``_invoke_custom_evaluate_async``) bypasses score()
                # entirely for the running-loop case.
                try:
                    asyncio.get_running_loop()
                    in_loop = True
                except RuntimeError:
                    in_loop = False
                if in_loop:
                    raise RuntimeError(
                        'CustomEvaluate.score() received an async fn but '
                        'is being called from inside a running event '
                        'loop. The runner should have routed this to the '
                        'async dispatch path; this indicates a runner-side '
                        'dispatch bug.'
                    )

                # ``inspect.isawaitable`` accepts ``Awaitable``; ``asyncio.run``
                # wants a ``Coroutine``. Wrap so the type checker is happy and
                # the runtime correctly drives any awaitable to completion.
                async def _drive_awaitable() -> None:
                    await result

                asyncio.run(_drive_awaitable())
        except AssertionError as exc:
            # An assertion failure inside the user evaluator is the natural
            # signal for a scenario fail (not error). Preserve any metrics
            # the evaluator populated BEFORE the assertion fired (e.g.
            # ``ctx.metrics['recall'] = 0.6; assert recall >= 0.8``) so
            # downstream MLflow / report artifacts retain the gradient
            # signal. ``pass=0.0`` is overlaid so the status logic
            # downstream agrees with the verdict. Non-numeric values are
            # silently dropped — ``ScenarioOutcome.metrics`` is strictly
            # ``dict[str, float]`` and would otherwise raise
            # ValidationError, erasing the verdict.
            preserved = _coerce_numeric(ctx.metrics)
            preserved['pass'] = 0.0
            logger.info('CustomEvaluate fn raised AssertionError: %s', exc)
            return preserved
        # No exception: fail-closed if user did not populate any metric.
        # The previous default ('pass': 1.0 on empty) silently passed
        # evaluators that forgot to assert anything — the inverse of what
        # an eval framework should do.
        if not ctx.metrics:
            logger.warning(
                'CustomEvaluate fn for scenario %r returned without '
                'populating ctx.metrics; recording pass=0.0 (fail-closed). '
                "Set ctx.metrics['pass'] = 1.0 explicitly for a passing "
                'scenario.',
                scenario.id,
            )
            ctx.metrics['pass'] = 0.0
        return _coerce_numeric(ctx.metrics)

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return list(self.declared_metric_keys) if self.declared_metric_keys else ['pass']


# ---------------------------------------------------------------------------
# BaseScenario — class-based authoring + lifecycle override surface.
# ---------------------------------------------------------------------------


_BASE_SCENARIO_DATA_FIELDS: tuple[str, ...] = (
    'id',
    'query',
    'description',
    'expected',
    'group',
    'top_k',
    'strategies',
    'include_superseded',
    'include_deprioritized',
    'setup_actions',
    'inline_notes',
    'vault_name',
    'max_duration_ms',
    'search_type',
    'answer_mode',
    'expected_failure_modes',
    'requires_nli_classifier',
    'depends_on_prior_scenarios',
)
_BASE_SCENARIO_MUTABLE_LIST_FIELDS: tuple[str, ...] = (
    'setup_actions',
    'inline_notes',
    'expected_failure_modes',
    'depends_on_prior_scenarios',
)


class BaseScenario:
    """Class-based scenario with overrideable lifecycle methods.

    Class attributes carry the same fields as today's ``Scenario`` Pydantic
    model. Override any of ``setup``, ``act``, ``evaluate``, ``teardown``
    on a subclass; call ``await super().<phase>(ctx)`` to delegate to the
    default machinery, or skip super to bypass it. A subclass that sets
    ``expected = SomeOutcome(...)`` and never overrides ``evaluate`` runs
    purely declaratively.

    The default ``evaluate`` runs ``self.expected.score(...)`` if expected
    is non-None, merging the result into ``ctx.metrics``. Subclasses that
    override ``evaluate`` and skip super gain full imperative control.

    Subclasses MUST have a no-arg ``__init__`` (``register_class``
    instantiates with ``cls()``). Pre-instantiated objects are also
    accepted: ``suite.register_class(MyScenario(top_k_override=20))``.
    """

    # ---- Required class attributes (must be set on the subclass) ----
    id: ClassVar[str]
    query: ClassVar[str]

    # ---- Optional class attributes (defaults match Scenario field defaults) ----
    description: ClassVar[str] = ''
    expected: ClassVar[ExpectedOutcomeUnion | None] = None
    group: ClassVar[str | None] = None
    top_k: ClassVar[int] = 10
    strategies: ClassVar[list[str] | None] = None
    include_superseded: ClassVar[bool | None] = None
    include_deprioritized: ClassVar[bool | None] = None
    setup_actions: ClassVar[list[SetupAction]] = []
    inline_notes: ClassVar[list[InlineNote]] = []
    vault_name: ClassVar[str | None] = None
    max_duration_ms: ClassVar[float | None] = None
    search_type: ClassVar[Literal['memory', 'note']] = 'memory'
    answer_mode: ClassVar[str | None] = None
    expected_failure_modes: ClassVar[list[str]] = []
    requires_nli_classifier: ClassVar[bool] = False
    depends_on_prior_scenarios: ClassVar[list[str]] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Per-subclass deep-copy of mutable list defaults.

        Without this, every subclass that doesn't override e.g.
        ``setup_actions`` shares the SAME list object as
        ``BaseScenario.setup_actions``. A user mutating it in place
        (``self.setup_actions.append(...)``) would then leak across all
        subsequent subclasses in the same process. We give each subclass
        its own copy of every mutable list field.

        Snapshot semantics: the copy is taken from the *resolved* parent
        attribute at class-definition time. So if ``class A(BaseScenario)``
        already has its own ``setup_actions`` (or has been mutated in
        place), then ``class C(A)`` inherits the deep-copied snapshot of
        ``A``'s current ``setup_actions``, not the pristine empty list
        on ``BaseScenario``. The natural pattern (subclass directly from
        ``BaseScenario``, never mutate class-level lists between class
        definitions) avoids the surprise; mention it here so a reviewer
        debugging a mid-chain subclass knows where the inherited entries
        came from.
        """
        super().__init_subclass__(**kwargs)
        for field_name in _BASE_SCENARIO_MUTABLE_LIST_FIELDS:
            if field_name in cls.__dict__:
                continue  # subclass declared an explicit override
            inherited = getattr(cls, field_name)
            setattr(cls, field_name, copy.deepcopy(inherited))

    def to_scenario_model(self) -> Scenario:
        """Build the Pydantic ``Scenario`` for the runner to consume.

        The runner stores a reference back to the BaseScenario instance
        on ``Scenario._base_scenario_instance`` (an extra attribute, since
        ``Scenario`` allows extras) so it can dispatch to the lifecycle
        methods at execution time.
        """
        # Description fallback chain: explicit class attr > class docstring
        # (first non-empty line, trimmed) > id. The docstring is the most
        # natural place to describe a scenario class; promoting it as a
        # data field saves users from declaring ``description = "..."``
        # twice.
        description = self.description
        if not description:
            doc = (type(self).__doc__ or '').strip()
            if doc:
                description = doc.splitlines()[0].strip()
        if not description:
            description = self.id
        sc_kwargs: dict[str, Any] = {
            'id': self.id,
            'description': description,
            'query': self.query,
            'expected': self.expected if self.expected is not None else _SENTINEL_EXPECTED,
            'group': self.group,
            'top_k': self.top_k,
            'strategies': self.strategies,
            'include_superseded': self.include_superseded,
            'include_deprioritized': self.include_deprioritized,
            'setup_actions': list(self.setup_actions),
            'inline_notes': list(self.inline_notes),
            'vault_name': self.vault_name,
            'max_duration_ms': self.max_duration_ms,
            'search_type': self.search_type,
            'answer_mode': self.answer_mode,
            'expected_failure_modes': list(self.expected_failure_modes),
            'requires_nli_classifier': self.requires_nli_classifier,
            'depends_on_prior_scenarios': list(self.depends_on_prior_scenarios),
        }
        sc = Scenario(**sc_kwargs)
        # Stash the live instance on the model. ``Scenario`` doesn't
        # declare ``_base_scenario_instance`` as a Pydantic field, so we
        # bypass model validation via ``object.__setattr__``. The runner
        # ALSO maintains a sidecar mapping on the legacy ``Suite`` (see
        # ``Suite.build``) so re-validation / model_copy paths that drop
        # the stashed attr can still recover the instance by id.
        object.__setattr__(sc, '_base_scenario_instance', self)
        return sc

    # ---- Lifecycle methods. Defaults delegate to the existing machinery. ----

    async def setup(self, ctx: ScenarioContext) -> None:
        """Run declared ``setup_actions`` and ingest ``inline_notes``.

        The runner already does this work in its ``_execute_scenario``
        body; the default here is a no-op because by the time ``setup``
        is invoked the runner has ALREADY done both. Subclasses override
        to add EXTRA side effects (e.g. flip a feature flag, write KV)
        AFTER the declared actions ran.
        """
        return None

    async def act(self, ctx: ScenarioContext) -> None:
        """Run after the backend produced ``ctx.answer``.

        The runner default executes the active ``AnswerBackend.answer``
        for the resolved ``answer_mode`` and populates ``ctx.answer``
        BEFORE this method runs. The default ``act`` is a no-op:
        subclasses can mutate ``ctx.answer`` (e.g. enrich with extra
        retrieval steps) or replace it entirely.

        Named ``act`` rather than ``query`` to avoid collision with
        the ``query: str`` data field on ``Scenario`` / ``BaseScenario``.
        """
        return None

    async def evaluate(self, ctx: ScenarioContext) -> None:
        """Score ``self.expected`` against ``ctx.answer``, merging into
        ``ctx.metrics``.

        Subclasses can:
        - **Replace**: don't call super; populate ``ctx.metrics`` directly.
        - **Extend**: call ``await super().evaluate(ctx)`` first, then add
          extra metrics / asserts.
        - **Skip declarative**: don't set ``expected`` AND override
          ``evaluate`` to populate ``ctx.metrics`` imperatively.

        If ``self.expected`` is None AND a subclass leaves ``ctx.metrics``
        empty after this call (i.e. the user forgot to override
        ``evaluate`` or to assign metrics), the runner records the
        scenario as ``status='fail'`` with a breadcrumb in the outcome
        ``error`` field — fail-closed (do not silently pass).
        """
        if self.expected is None:
            return None
        scored = self.expected.score(
            ctx.answer,
            ctx.scenario,
            note_key_to_unit_ids=ctx.note_key_to_unit_ids,
            judge=ctx.judge,
            context=ctx.setup_context,
        )
        ctx.metrics.update(scored)

    async def teardown(self, ctx: ScenarioContext) -> None:
        """Default: no-op (the runner runs handler teardowns +
        inline-note delete unconditionally outside this method).

        Subclasses override to add EXTRA cleanup that must run regardless
        of pass/fail (the runner invokes this in a ``finally`` block,
        same as the existing teardown machinery).
        """
        return None


# Sentinel: when a BaseScenario subclass leaves ``expected`` as None, we
# can't pass None to the Scenario Pydantic model (the field is required).
# We use a CustomEvaluate whose function defers to the BaseScenario
# instance's ``evaluate``. The runner detects ``_base_scenario_instance``
# and dispatches lifecycle methods directly, so this sentinel is never
# actually invoked at score time.
def _sentinel_evaluator(ctx: ScenarioContext) -> None:
    raise RuntimeError(
        'CustomEvaluate sentinel was invoked — the runner should have '
        'dispatched to the BaseScenario instance instead. This indicates '
        'a runner-side dispatch bug; please file an issue.'
    )


_SENTINEL_EXPECTED = CustomEvaluate(fn=_sentinel_evaluator)


# Sentinel for "attribute not set" used by register_class's MRO walk.
# Distinct from None (which IS a valid value for several optional fields).
_NOT_SET: Any = object()


# ---------------------------------------------------------------------------
# Suite — the decorator-aware authoring surface.
# ---------------------------------------------------------------------------


class Suite:
    """Authoring container for the decorator API.

    Build scenarios via:
    - ``suite.register(...)`` (method call, fully declarative)
    - ``@suite.scenario(...)`` (decorator; function body is the evaluator)
    - ``@suite.register_class`` (decorator; class with overrideable lifecycle)

    Call ``suite.build()`` to materialize a legacy ``memex_eval.suite.base.Suite``
    that the loader and runner consume. Existing suites that export
    ``SUITE: <legacy Suite>`` keep working unchanged; the loader detects
    either form.
    """

    def __init__(
        self,
        metadata: SuiteMetadata,
        sources: SuiteSources | None = None,
        readme_path: Path | None = None,
        shipped_snapshot_path: Path | None = None,
    ) -> None:
        self.metadata = metadata
        self.sources = sources or SuiteSources(notes=[])
        self.readme_path = readme_path
        self.shipped_snapshot_path = shipped_snapshot_path
        self._scenarios: list[Scenario] = []
        self._seen_ids: set[str] = set()
        # Sidecar mapping survives Pydantic revalidation: the runner
        # falls back to this lookup when ``Scenario._base_scenario_instance``
        # has been stripped (e.g. by ``model_copy(deep=True)``).
        self._base_scenarios_by_id: dict[str, BaseScenario] = {}

    # ---- 1. Pure declarative: method call ----

    def register(
        self,
        *,
        id: str,
        query: str,
        expected: ExpectedOutcomeUnion,
        description: str | None = None,
        group: str | None = None,
        top_k: int = 10,
        strategies: list[str] | None = None,
        include_superseded: bool | None = None,
        include_deprioritized: bool | None = None,
        setup_actions: Sequence[SetupAction] | None = None,
        inline_notes: Sequence[InlineNote] | None = None,
        vault_name: str | None = None,
        max_duration_ms: float | None = None,
        search_type: Literal['memory', 'note'] = 'memory',
        answer_mode: str | None = None,
        expected_failure_modes: Sequence[str] | None = None,
        requires_nli_classifier: bool = False,
        depends_on_prior_scenarios: Sequence[str] | None = None,
        replicates_override: int | None = None,
        mutating_scenario: bool = False,
    ) -> Scenario:
        """Register a fully-declarative scenario.

        Same field set as ``memex_eval.suite.base.Scenario``. Returns the
        materialized model so callers can introspect (rarely needed)."""
        sc = Scenario(
            id=id,
            description=description if description is not None else id,
            query=query,
            expected=expected,
            group=group,
            top_k=top_k,
            strategies=strategies,
            include_superseded=include_superseded,
            include_deprioritized=include_deprioritized,
            setup_actions=list(setup_actions or []),
            inline_notes=list(inline_notes or []),
            vault_name=vault_name,
            max_duration_ms=max_duration_ms,
            search_type=search_type,
            answer_mode=answer_mode,
            expected_failure_modes=list(expected_failure_modes or []),
            requires_nli_classifier=requires_nli_classifier,
            depends_on_prior_scenarios=list(depends_on_prior_scenarios or []),
            replicates_override=replicates_override,
            mutating_scenario=mutating_scenario,
        )
        self._append(sc)
        return sc

    # ---- 2. Decorator: function body becomes the evaluator ----

    def scenario(
        self,
        *,
        id: str,
        query: str,
        description: str | None = None,
        group: str | None = None,
        top_k: int = 10,
        strategies: list[str] | None = None,
        include_superseded: bool | None = None,
        include_deprioritized: bool | None = None,
        setup_actions: Sequence[SetupAction] | None = None,
        inline_notes: Sequence[InlineNote] | None = None,
        vault_name: str | None = None,
        max_duration_ms: float | None = None,
        search_type: Literal['memory', 'note'] = 'memory',
        answer_mode: str | None = None,
        expected_failure_modes: Sequence[str] | None = None,
        requires_nli_classifier: bool = False,
        depends_on_prior_scenarios: Sequence[str] | None = None,
        declared_metric_keys: Sequence[str] | None = None,
    ) -> Callable[[EvaluatorFn], EvaluatorFn]:
        """Decorator: the wrapped function REPLACES ``expected``.

        The function is wrapped in a ``CustomEvaluate`` outcome whose
        ``score()`` invokes it with a ``ScenarioContext``. The function
        mutates ``ctx.metrics`` in place; ``AssertionError`` raises out
        of score() and is caught by the runner as ``status='fail'``.

        ``expected`` is intentionally NOT a kwarg here — the function
        body IS the expected. If you want a typed outcome AND extra
        checks, use the class-based API (``@suite.register_class``)
        with a ``super().evaluate(ctx)`` chain.

        ``declared_metric_keys`` is optional — pass to advertise the
        metric keys this evaluator emits, so reporters / MLflow tooling
        can pre-allocate. If omitted, defaults to ``['pass']``.
        """

        def deco(fn: EvaluatorFn) -> EvaluatorFn:
            outcome = CustomEvaluate(
                fn=fn,
                declared_metric_keys=list(declared_metric_keys)
                if declared_metric_keys is not None
                else None,
            )
            sc = Scenario(
                id=id,
                description=description if description is not None else id,
                query=query,
                expected=outcome,
                group=group,
                top_k=top_k,
                strategies=strategies,
                include_superseded=include_superseded,
                include_deprioritized=include_deprioritized,
                setup_actions=list(setup_actions or []),
                inline_notes=list(inline_notes or []),
                vault_name=vault_name,
                max_duration_ms=max_duration_ms,
                search_type=search_type,
                answer_mode=answer_mode,
                expected_failure_modes=list(expected_failure_modes or []),
                requires_nli_classifier=requires_nli_classifier,
                depends_on_prior_scenarios=list(depends_on_prior_scenarios or []),
            )
            self._append(sc)
            return fn

        return deco

    # ---- 3. Class-based: full lifecycle override ----

    def register_class(
        self, cls_or_instance: type[BaseScenario] | BaseScenario
    ) -> type[BaseScenario] | BaseScenario:
        """Decorator (or method call) for ``BaseScenario`` subclasses.

        Two forms:
        - ``@suite.register_class`` on a class: instantiated via ``cls()``.
        - ``suite.register_class(MyScenario(top_k_override=20))`` with a
          pre-instantiated object: used as-is. Lets users build
          parameterized scenarios and register the same class multiple
          times with different settings.

        The returned value is the same object — decoration is a side effect.

        Required: the subclass MUST set ``id`` and ``query`` as
        non-empty STRING class attributes (not inherited methods, not
        callables, not non-strings). The walk stops at ``BaseScenario``
        so a missing override surfaces here rather than as an opaque
        ValidationError downstream. Data field names that collide with
        callable subclass attributes (e.g. ``def expected(self): ...``)
        are also rejected with a clear error.
        """
        instance_provided = isinstance(cls_or_instance, BaseScenario)
        if instance_provided:
            instance: BaseScenario = cls_or_instance  # type: ignore[assignment]
            cls = type(instance)
            return_value: type[BaseScenario] | BaseScenario = instance
        elif isinstance(cls_or_instance, type) and issubclass(cls_or_instance, BaseScenario):
            cls = cls_or_instance
            try:
                instance = cls()
            except TypeError as exc:
                raise TypeError(
                    f'{cls.__qualname__} must define a no-arg ``__init__`` '
                    f'(register_class instantiates via ``cls()``). To pass '
                    f'arguments, instantiate first and call '
                    f'``suite.register_class(MyScenario(...))``. '
                    f'Original: {exc}'
                ) from exc
            return_value = cls
        else:
            raise TypeError(
                f'@suite.register_class expects a BaseScenario subclass or '
                f'an instance; got {cls_or_instance!r}'
            )

        def _walk_mro_attrs(name: str) -> Any:
            """Return the value of ``name`` from the lowest subclass that
            declares it, stopping at ``BaseScenario``. ``object()`` sentinel
            for "not set."""
            for klass in cls.__mro__:
                if klass is BaseScenario:
                    break
                if name in klass.__dict__:
                    return klass.__dict__[name]
            return _NOT_SET

        def _resolve(name: str) -> Any:
            """Lookup attribute on the live instance first (so per-instance
            ``__init__`` overrides are honored when a pre-instantiated
            instance is passed), then walk class MRO. The ``to_scenario_model``
            ultimately reads from ``self.<name>``, so the validator must
            see the same value the runner will."""
            if instance_provided and name in instance.__dict__:
                return instance.__dict__[name]
            return _walk_mro_attrs(name)

        for required in ('id', 'query'):
            val = _resolve(required)
            if val is _NOT_SET:
                raise TypeError(
                    f'{cls.__qualname__} must set class attribute '
                    f'``{required}`` (e.g. ``{required} = "..."``) or assign '
                    f'``self.{required}`` in ``__init__`` when registering an '
                    f'instance via ``suite.register_class(MyScenario(...))``'
                )
            if callable(val) and not isinstance(val, str):
                raise TypeError(
                    f'{cls.__qualname__}.{required} is a {type(val).__name__}; '
                    f'this name collides with a BaseScenario data field. '
                    f'Either rename your method or set ``{required}`` to a '
                    f'non-empty string class attribute.'
                )
            if not isinstance(val, str) or not val:
                raise TypeError(
                    f'{cls.__qualname__}.{required} = {val!r} '
                    f'(type {type(val).__name__}); must be a non-empty string.'
                )

        # Detect data-field/method collisions on every other data field —
        # callables masquerading as data values produce opaque Pydantic
        # errors deep in the validation pipeline. Surface them here.
        for name in _BASE_SCENARIO_DATA_FIELDS:
            if name in ('id', 'query'):
                continue
            val = _resolve(name)
            if val is _NOT_SET:
                continue
            if callable(val) and not isinstance(val, (str, list, tuple, dict)):
                raise TypeError(
                    f'{cls.__qualname__}.{name} is a {type(val).__name__}; '
                    f'this name collides with the BaseScenario data field '
                    f'``{name}``. Rename your method, or set the data '
                    f'field explicitly as a class attribute.'
                )

        sc = instance.to_scenario_model()
        self._append(sc)
        # Sidecar registry: surviving Pydantic re-validation paths via the
        # Suite-level lookup (model_copy / model_validate roundtrips drop
        # the ``_base_scenario_instance`` attr stashed by to_scenario_model).
        self._base_scenarios_by_id[sc.id] = instance
        return return_value

    # ---- Materialization ----

    def build(self) -> _LegacySuite:
        """Materialize as a legacy ``Suite`` for the loader and runner."""
        suite = _LegacySuite(
            metadata=self.metadata,
            sources=self.sources,
            scenarios=list(self._scenarios),
            readme_path=self.readme_path,
            shipped_snapshot_path=self.shipped_snapshot_path,
        )
        # Mirror the BaseScenario instance map onto the legacy Suite so
        # the runner can recover dispatch instances even when ``Scenario``
        # has been re-validated and lost its ``_base_scenario_instance``
        # extra attr. Stored as a non-Pydantic attribute via
        # ``object.__setattr__``.
        object.__setattr__(suite, '_base_scenarios_by_id', dict(self._base_scenarios_by_id))
        return suite

    # ---- Internal ----

    def _append(self, sc: Scenario) -> None:
        if sc.id in self._seen_ids:
            raise ValueError(
                f'Duplicate scenario id {sc.id!r} in suite {self.metadata.name!r}. '
                f'Each scenario id must be unique within a suite.'
            )
        # Dependency ordering: every id named in
        # ``depends_on_prior_scenarios`` must have already been registered.
        # Surfacing this here gives a clean stack frame pointing at the
        # offending ``register/scenario/register_class`` call instead of
        # an opaque ValidationError at suite ``build()`` time.
        for dep_id in sc.depends_on_prior_scenarios:
            if dep_id not in self._seen_ids:
                raise ValueError(
                    f'Scenario {sc.id!r} declares '
                    f'depends_on_prior_scenarios={sc.depends_on_prior_scenarios!r} '
                    f'but {dep_id!r} has not been registered yet. '
                    f'Register dependencies before consumers.'
                )
        self._seen_ids.add(sc.id)
        self._scenarios.append(sc)


__all__ = [
    'BaseScenario',
    'CustomEvaluate',
    'EvaluatorFn',
    'ScenarioContext',
    'Suite',
]
