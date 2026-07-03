"""Round-5 regression guard: Hermes JSON Schema enum lists must match the canonical enums.

The Hermes plugin's tool surface declares ``intent_class`` and ``risk_class``
JSON Schema ``enum: [...]`` lists. Before the round-5 patch these were
hand-typed string literals — adding or renaming a value in
``memex_common.schemas.IntentClass`` / ``RiskClass`` would silently desync
the tool surface.

After the patch the lists are derived at module load from the canonical
enums (``[c.value for c in IntentClass]``). These tests assert the
derivation holds — if the values diverge for any reason, this test fails.

Lives in the hermes-plugin tests dir (rather than common) because importing
``memex_hermes_plugin.memex.tools`` requires the Hermes ``agent`` /
``tools.registry`` stubs registered by ``conftest.py``.
"""

from __future__ import annotations

from memex_common.schemas import IntentClass, RiskClass

from memex_hermes_plugin.memex.tools import (
    RECALL_SCHEMA,
    RETAIN_SCHEMA,
)


class TestHermesJsonSchemaParity:
    """JSON Schema enum lists in tool definitions must derive from the canonical enums."""

    def test_recall_tool_intent_enum_matches_canonical(self) -> None:
        """RECALL_SCHEMA backs ``memex_memory_search``."""
        intent_field = RECALL_SCHEMA['parameters']['properties']['intent_class']
        assert intent_field['enum'] == [c.value for c in IntentClass], (
            'memex_memory_search intent_class enum has drifted from '
            'IntentClass. The list should be derived via [c.value for c in '
            'IntentClass] — not hand-typed.'
        )

    def test_recall_tool_risk_enum_matches_canonical(self) -> None:
        risk_field = RECALL_SCHEMA['parameters']['properties']['risk_class']
        assert risk_field['enum'] == [c.value for c in RiskClass], (
            'memex_memory_search risk_class enum has drifted from RiskClass.'
        )

    def test_retain_tool_intent_enum_matches_canonical(self) -> None:
        """RETAIN_SCHEMA backs ``memex_add_note``."""
        intent_field = RETAIN_SCHEMA['parameters']['properties']['intent_class']
        assert intent_field['enum'] == [c.value for c in IntentClass], (
            'memex_add_note intent_class enum has drifted from IntentClass.'
        )

    def test_retain_tool_risk_enum_matches_canonical(self) -> None:
        risk_field = RETAIN_SCHEMA['parameters']['properties']['risk_class']
        assert risk_field['enum'] == [c.value for c in RiskClass], (
            'memex_add_note risk_class enum has drifted from RiskClass.'
        )
