"""Round-5 regression guard: canonical-source parity for IntentClass / RiskClass.

The Hermes round-5 review flagged a DRY violation: the ``permanent | durable |
ephemeral`` and ``none | sensitive | private | safety`` value lists were
duplicated across:

- ``memex_common.schemas.IntentClass`` / ``RiskClass`` (canonical Python enums)
- ``memex_common.schemas.IntentLiteral`` / ``RiskLiteral`` (canonical Literal aliases)
- ``memex_mcp.server`` (MCP tool ``Literal[...]`` annotations)
- ``memex_hermes_plugin.memex.tools`` (JSON Schema ``enum: [...]`` lists)
- ``memex_core.memory.extraction.classifier`` (DSPy signature Literal aliases)

After the round-5 patch the downstream sites all import from
``memex_common.schemas`` — but mypy refuses to accept a runtime-computed
``Literal[*tuple(c.value for c in IntentClass)]`` as a valid type alias,
so ``IntentLiteral`` / ``RiskLiteral`` are still hand-typed in a single
canonical location. This test asserts they cannot drift from the enum
without a test failure: adding or renaming a value in ``IntentClass`` /
``RiskClass`` without also updating the Literal aliases (or vice versa) is
caught here.

Also asserts the MCP ``Annotated`` parameter types derive from the same
canonical source. Hermes plugin JSON Schema parity has its own test under
``packages/hermes-plugin/tests/test_enum_schema_parity.py`` because Hermes
modules require runtime stubs that only the hermes-plugin conftest installs.
"""

from typing import Annotated, get_args, get_origin, get_type_hints

import pytest


class TestEnumLiteralParity:
    """The canonical Literal aliases in memex_common.schemas must equal the enums."""

    def test_intent_literal_matches_intent_class(self) -> None:
        from memex_common.schemas import IntentClass, IntentLiteral

        assert get_args(IntentLiteral) == tuple(c.value for c in IntentClass), (
            'IntentLiteral has drifted from IntentClass. Update both in '
            'memex_common.schemas — they must enumerate identical values.'
        )

    def test_risk_literal_matches_risk_class(self) -> None:
        from memex_common.schemas import RiskClass, RiskLiteral

        assert get_args(RiskLiteral) == tuple(c.value for c in RiskClass), (
            'RiskLiteral has drifted from RiskClass. Update both in '
            'memex_common.schemas — they must enumerate identical values.'
        )

    def test_valid_intent_classes_matches_intent_class(self) -> None:
        from memex_common.schemas import VALID_INTENT_CLASSES, IntentClass

        assert VALID_INTENT_CLASSES == frozenset(c.value for c in IntentClass)

    def test_valid_risk_classes_matches_risk_class(self) -> None:
        from memex_common.schemas import VALID_RISK_CLASSES, RiskClass

        assert VALID_RISK_CLASSES == frozenset(c.value for c in RiskClass)


class TestMCPServerParity:
    """MCP search tool intent/risk parameter Literal types must derive from canonical aliases."""

    def _get_search_hints(self) -> dict[str, object]:
        """Resolve type hints on the underlying ``memex_memory_search`` function.

        ``@mcp.tool`` wraps the function in a ``FunctionTool``; the original
        callable is exposed via ``.fn`` (FastMCP's standard accessor).
        """
        from memex_mcp.server import memex_memory_search

        underlying = getattr(memex_memory_search, 'fn', memex_memory_search)
        return get_type_hints(underlying, include_extras=True)

    def test_mcp_search_intent_class_uses_canonical_literal(self) -> None:
        try:
            from memex_common.schemas import IntentLiteral
        except ImportError:
            pytest.skip('memex_common not installed in this environment')
        try:
            hints = self._get_search_hints()
        except ImportError:
            pytest.skip('memex_mcp not installed in this environment')
        intent_hint = hints['intent_class']
        # intent_class is Annotated[IntentLiteral | None, Field(...)] — extract
        # the underlying union and assert IntentLiteral is one of its members.
        assert get_origin(intent_hint) is Annotated, (
            'intent_class hint should be Annotated[...]; got: ' + repr(intent_hint)
        )
        union_type = get_args(intent_hint)[0]
        union_members = get_args(union_type)
        assert IntentLiteral in union_members, (
            'MCP memex_memory_search intent_class param does not reference '
            'the canonical IntentLiteral; saw union members: ' + repr(union_members)
        )

    def test_mcp_search_risk_class_uses_canonical_literal(self) -> None:
        try:
            from memex_common.schemas import RiskLiteral
        except ImportError:
            pytest.skip('memex_common not installed in this environment')
        try:
            hints = self._get_search_hints()
        except ImportError:
            pytest.skip('memex_mcp not installed in this environment')
        risk_hint = hints['risk_class']
        assert get_origin(risk_hint) is Annotated, (
            'risk_class hint should be Annotated[...]; got: ' + repr(risk_hint)
        )
        union_type = get_args(risk_hint)[0]
        union_members = get_args(union_type)
        assert RiskLiteral in union_members, (
            'MCP memex_memory_search risk_class param does not reference '
            'the canonical RiskLiteral; saw union members: ' + repr(union_members)
        )


class TestClassifierParity:
    """The DSPy classifier signature must derive its Literal types from the canonical aliases."""

    def test_classifier_intent_literal_is_canonical(self) -> None:
        from memex_common.schemas import IntentLiteral as CanonicalIntentLiteral
        from memex_core.memory.extraction.classifier import IntentLiteral

        assert IntentLiteral is CanonicalIntentLiteral, (
            'Classifier IntentLiteral is not the canonical alias from '
            'memex_common.schemas — it has been re-defined locally.'
        )

    def test_classifier_risk_literal_is_canonical(self) -> None:
        from memex_common.schemas import RiskLiteral as CanonicalRiskLiteral
        from memex_core.memory.extraction.classifier import RiskLiteral

        assert RiskLiteral is CanonicalRiskLiteral

    def test_classifier_intent_values_matches_canonical(self) -> None:
        from memex_common.schemas import IntentClass
        from memex_core.memory.extraction.classifier import INTENT_VALUES

        assert INTENT_VALUES == tuple(c.value for c in IntentClass)

    def test_classifier_risk_values_matches_canonical(self) -> None:
        from memex_common.schemas import RiskClass
        from memex_core.memory.extraction.classifier import RISK_VALUES

        assert RISK_VALUES == tuple(c.value for c in RiskClass)
