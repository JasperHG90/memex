"""Tests for scenario definitions — structure validation and edge cases."""

from __future__ import annotations

import base64

from memex_eval.internal.scenarios import (
    ALL_GROUPS,
    GroundTruthCheck,
    ScenarioGroup,
    SyntheticDoc,
    get_group,
)


# ---------------------------------------------------------------------------
# SyntheticDoc
# ---------------------------------------------------------------------------


class TestSyntheticDoc:
    def test_content_b64_roundtrip(self):
        doc = SyntheticDoc(
            filename='test.md',
            title='Test',
            description='A test doc',
            content='Hello, world!',
        )
        decoded = base64.b64decode(doc.content_b64).decode('utf-8')
        assert decoded == 'Hello, world!'

    def test_files_b64_roundtrip(self):
        raw_data = b'\x89PNG\r\n'
        doc = SyntheticDoc(
            filename='test.md',
            title='Test',
            description='A test doc',
            content='text',
            files={'image.png': raw_data},
        )
        decoded = base64.b64decode(doc.files_b64['image.png'])
        assert decoded == raw_data

    def test_empty_files(self):
        doc = SyntheticDoc(
            filename='test.md',
            title='Test',
            description='Desc',
            content='text',
        )
        assert doc.files_b64 == {}


# ---------------------------------------------------------------------------
# GroundTruthCheck defaults
# ---------------------------------------------------------------------------


class TestGroundTruthCheck:
    def test_defaults(self):
        check = GroundTruthCheck(
            name='c1',
            description='desc',
            query='query',
            check_type='keyword_in_results',
            expected='keyword',
        )
        assert check.search_type == 'memory'
        assert check.top_k == 10
        assert check.strategies is None
        assert check.vault_name is None
        assert check.max_duration_ms is None
        assert check.include_superseded is None

    def test_expected_can_be_list(self):
        check = GroundTruthCheck(
            name='c1',
            description='desc',
            query='query',
            check_type='keyword_in_results',
            expected=['a', 'b', 'c'],
        )
        assert isinstance(check.expected, list)
        assert len(check.expected) == 3


# ---------------------------------------------------------------------------
# ScenarioGroup
# ---------------------------------------------------------------------------


class TestScenarioGroup:
    def test_sequential_ingest_default(self):
        group = ScenarioGroup(name='g', description='d', docs=[], checks=[])
        assert group.sequential_ingest is False


# ---------------------------------------------------------------------------
# get_group
# ---------------------------------------------------------------------------


class TestGetGroup:
    def test_found(self):
        group = get_group('basic_extraction')
        assert group is not None
        assert group.name == 'basic_extraction'

    def test_not_found(self):
        assert get_group('nonexistent_group_xyz') is None


# ---------------------------------------------------------------------------
# ALL_GROUPS integrity
# ---------------------------------------------------------------------------


class TestAllGroups:
    def test_all_groups_non_empty(self):
        assert len(ALL_GROUPS) > 0

    def test_all_groups_have_unique_names(self):
        names = [g.name for g in ALL_GROUPS]
        assert len(names) == len(set(names)), f'Duplicate group names: {names}'

    def test_all_groups_have_checks(self):
        for group in ALL_GROUPS:
            assert len(group.checks) > 0, f'Group "{group.name}" has no checks'

    def test_all_checks_have_valid_types(self):
        valid_types = {
            'keyword_in_results',
            'keyword_absent_from_results',
            'entity_exists',
            'entity_type_check',
            'entity_cooccurrence_check',
            'entity_mention_check',
            'result_ordering',
            'llm_judge',
        }
        for group in ALL_GROUPS:
            for check in group.checks:
                assert check.check_type in valid_types, (
                    f'Group "{group.name}", check "{check.name}" has '
                    f'invalid type: {check.check_type}'
                )

    def test_all_checks_have_required_fields(self):
        for group in ALL_GROUPS:
            for check in group.checks:
                assert check.name, f'Check in group "{group.name}" has empty name'
                assert check.query, f'Check "{check.name}" has empty query'
                assert check.expected is not None, f'Check "{check.name}" has None expected'
