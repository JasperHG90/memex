"""Tests for cognitive-memory scenario group definitions."""

from __future__ import annotations

import pytest

from memex_eval.internal.scenarios import (
    ALL_GROUPS,
    GROUP_DEPRIORITIZATION,
    GROUP_INTENT_CLASSIFICATION,
    GROUP_LINT,
    GROUP_OUTCOMES_MW,
    GROUP_PROCEDURAL_KV,
    GROUP_SUMMARIZATION,
    GroundTruthCheck,
    SyntheticDoc,
    get_group,
)


# ---------------------------------------------------------------------------
# Group existence and membership
# ---------------------------------------------------------------------------


class TestGroupRegistration:
    """All 6 new groups appear in ALL_GROUPS and are retrievable by name."""

    NEW_GROUP_NAMES = [
        'outcomes_mw',
        'deprioritization',
        'intent_classification',
        'procedural_kv',
        'summarization',
        'lint',
    ]

    def test_all_groups_includes_new_groups(self) -> None:
        names = {g.name for g in ALL_GROUPS}
        for name in self.NEW_GROUP_NAMES:
            assert name in names, f'{name} missing from ALL_GROUPS'

    @pytest.mark.parametrize('name', NEW_GROUP_NAMES)
    def test_get_group_returns_correct_group(self, name: str) -> None:
        group = get_group(name)
        assert group is not None, f'get_group({name!r}) returned None'
        assert group.name == name


# ---------------------------------------------------------------------------
# Well-formedness checks
# ---------------------------------------------------------------------------


class TestGroupWellFormedness:
    """Each group's docs and checks are well-formed."""

    @pytest.mark.parametrize(
        'group',
        [
            GROUP_OUTCOMES_MW,
            GROUP_DEPRIORITIZATION,
            GROUP_INTENT_CLASSIFICATION,
            GROUP_PROCEDURAL_KV,
            GROUP_SUMMARIZATION,
            GROUP_LINT,
        ],
        ids=[
            'outcomes_mw',
            'deprioritization',
            'intent_classification',
            'procedural_kv',
            'summarization',
            'lint',
        ],
    )
    def test_docs_are_synthetic_docs(self, group) -> None:
        for doc in group.docs:
            assert isinstance(doc, SyntheticDoc)
            assert doc.filename
            assert doc.title
            assert doc.content

    @pytest.mark.parametrize(
        'group',
        [
            GROUP_OUTCOMES_MW,
            GROUP_DEPRIORITIZATION,
            GROUP_INTENT_CLASSIFICATION,
            GROUP_PROCEDURAL_KV,
            GROUP_SUMMARIZATION,
            GROUP_LINT,
        ],
        ids=[
            'outcomes_mw',
            'deprioritization',
            'intent_classification',
            'procedural_kv',
            'summarization',
            'lint',
        ],
    )
    def test_checks_are_ground_truth_checks(self, group) -> None:
        for check in group.checks:
            assert isinstance(check, GroundTruthCheck)
            assert check.name
            assert check.description
            assert check.check_type

    def test_procedural_kv_has_no_docs(self) -> None:
        assert len(GROUP_PROCEDURAL_KV.docs) == 0

    def test_procedural_kv_has_kv_roundtrip(self) -> None:
        assert any(c.check_type == 'kv_roundtrip' for c in GROUP_PROCEDURAL_KV.checks)


# ---------------------------------------------------------------------------
# Setup actions validation
# ---------------------------------------------------------------------------


class TestSetupActions:
    """All setup_actions reference valid action kinds."""

    VALID_KINDS = {'record_outcome', 'deprioritize', 'kv_write', 'consolidation_tick'}

    @pytest.mark.parametrize(
        'group',
        [
            GROUP_OUTCOMES_MW,
            GROUP_DEPRIORITIZATION,
            GROUP_INTENT_CLASSIFICATION,
            GROUP_PROCEDURAL_KV,
            GROUP_SUMMARIZATION,
            GROUP_LINT,
        ],
        ids=[
            'outcomes_mw',
            'deprioritization',
            'intent_classification',
            'procedural_kv',
            'summarization',
            'lint',
        ],
    )
    def test_setup_action_kinds_are_valid(self, group) -> None:
        for check in group.checks:
            for action in check.setup_actions or []:
                assert action.kind in self.VALID_KINDS, f'Invalid action kind: {action.kind}'

    def test_kv_write_actions_have_key_and_value(self) -> None:
        for group in ALL_GROUPS:
            for check in group.checks:
                for action in check.setup_actions or []:
                    if action.kind == 'kv_write':
                        assert action.kv_key is not None
                        assert action.kv_value is not None

    def test_record_outcome_actions_have_search_query_or_ids(self) -> None:
        for group in ALL_GROUPS:
            for check in group.checks:
                for action in check.setup_actions or []:
                    if action.kind == 'record_outcome':
                        assert action.search_query or action.unit_ids, (
                            'record_outcome action must have search_query or unit_ids'
                        )

    def test_deprioritize_actions_have_search_query_or_ids(self) -> None:
        for group in ALL_GROUPS:
            for check in group.checks:
                for action in check.setup_actions or []:
                    if action.kind == 'deprioritize':
                        assert action.search_query or action.unit_ids, (
                            'deprioritize action must have search_query or unit_ids'
                        )


# ---------------------------------------------------------------------------
# Vault name consistency
# ---------------------------------------------------------------------------


class TestVaultNames:
    """Every vault_name in checks must reference a doc in the same group."""

    @pytest.mark.parametrize(
        'group',
        [
            GROUP_OUTCOMES_MW,
            GROUP_DEPRIORITIZATION,
            GROUP_INTENT_CLASSIFICATION,
            GROUP_PROCEDURAL_KV,
            GROUP_SUMMARIZATION,
            GROUP_LINT,
        ],
        ids=[
            'outcomes_mw',
            'deprioritization',
            'intent_classification',
            'procedural_kv',
            'summarization',
            'lint',
        ],
    )
    def test_check_vault_names_reference_docs(self, group) -> None:
        doc_vault_names = {d.vault_name for d in group.docs}
        for check in group.checks:
            if check.vault_name is not None:
                assert check.vault_name in doc_vault_names, (
                    f'Check {check.name} references unknown vault {check.vault_name}'
                )


# ---------------------------------------------------------------------------
# Check type validity
# ---------------------------------------------------------------------------


class TestCheckTypes:
    """All check types are in the dispatch table."""

    from memex_eval.internal.checks import _CHECK_DISPATCH

    @pytest.mark.parametrize(
        'group',
        [
            GROUP_OUTCOMES_MW,
            GROUP_DEPRIORITIZATION,
            GROUP_INTENT_CLASSIFICATION,
            GROUP_PROCEDURAL_KV,
            GROUP_SUMMARIZATION,
            GROUP_LINT,
        ],
        ids=[
            'outcomes_mw',
            'deprioritization',
            'intent_classification',
            'procedural_kv',
            'summarization',
            'lint',
        ],
    )
    def test_check_types_in_dispatch(self, group) -> None:
        for check in group.checks:
            assert check.check_type in self._CHECK_DISPATCH, (
                f'Check type {check.check_type!r} not in dispatch table'
            )
