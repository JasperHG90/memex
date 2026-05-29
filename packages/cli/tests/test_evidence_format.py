"""Unit tests for evidence and unit-meta formatting in the cockpit TUI.

Tests ``_format_evidence_line`` and ``_format_unit_meta_line`` from
``packages/cli/src/memex_cli/cockpit/app.py``.
"""

from __future__ import annotations

from memex_cli.cockpit.app import (
    _EVIDENCE_SKIP_KEYS,
    _format_evidence_line,
    _format_unit_meta_line,
)
from memex_cli.cockpit.controller import UnitMeta


# ---------------------------------------------------------------------------
# _format_evidence_line tests
# ---------------------------------------------------------------------------


class TestFormatEvidenceLine:
    def test_skips_keys_in_skip_set(self) -> None:
        """All keys in _EVIDENCE_SKIP_KEYS are omitted from the output."""
        evidence = {key: 'value' for key in _EVIDENCE_SKIP_KEYS}
        result = _format_evidence_line(evidence)
        assert result == ''

    def test_formats_floats_to_2dp(self) -> None:
        """Float values are formatted to exactly 2 decimal places."""
        evidence = {'mw_score': 0.123456789}
        result = _format_evidence_line(evidence)
        assert '0.12' in result
        assert '0.123' not in result

    def test_truncates_iso_timestamps_to_date(self) -> None:
        """ISO timestamps (strings with 'T' longer than 10 chars) → date only."""
        evidence = {'created_at': '2026-01-15T14:30:00Z'}
        result = _format_evidence_line(evidence)
        assert '2026-01-15' in result
        assert 'T14:30:00Z' not in result

    def test_skips_none_values(self) -> None:
        """None values are omitted from the output."""
        evidence = {'mw_score': None, 'risk_class': 'high'}
        result = _format_evidence_line(evidence)
        assert 'mw_score' not in result.lower()
        assert 'Risk class' in result

    def test_skips_dict_values(self) -> None:
        """Dict values are omitted from the output."""
        evidence = {'components': {'a': 1}, 'risk_class': 'high'}
        result = _format_evidence_line(evidence)
        assert 'Risk class' in result
        # 'components' is in SKIP_KEYS AND is a dict — double skip.

    def test_skips_list_values(self) -> None:
        """List values are omitted from the output."""
        evidence = {'some_list': [1, 2, 3], 'risk_class': 'low'}
        result = _format_evidence_line(evidence)
        assert 'some_list' not in result.lower()
        assert 'Risk class' in result

    def test_empty_evidence_returns_empty_string(self) -> None:
        """Empty evidence dict produces empty string."""
        assert _format_evidence_line({}) == ''

    def test_all_skipped_returns_empty_string(self) -> None:
        """Evidence with only skip keys + None produces empty string."""
        evidence = {'entity_name': 'foo', 'explanation': 'bar', 'missing': None}
        result = _format_evidence_line(evidence)
        assert result == ''

    def test_uses_human_labels_when_available(self) -> None:
        """Known evidence keys get human-friendly labels."""
        evidence = {'observation_count': 5}
        result = _format_evidence_line(evidence)
        assert 'Observations' in result

    def test_unknown_key_uses_title_case(self) -> None:
        """Unknown keys get title-cased with underscores replaced by spaces."""
        evidence = {'some_custom_field': 42}
        result = _format_evidence_line(evidence)
        assert 'Some custom field' in result

    def test_integer_values_rendered_as_strings(self) -> None:
        """Integer values are str()-ed, not formatted as float."""
        evidence = {'link_count': 7}
        result = _format_evidence_line(evidence)
        assert '7' in result
        assert '7.00' not in result

    def test_multiple_fields_joined_with_dot_separator(self) -> None:
        """Multiple fields are joined with a dot separator."""
        evidence = {'risk_class': 'high', 'link_count': 3}
        result = _format_evidence_line(evidence)
        # The function joins with ' · ' (middle dot).
        assert '·' in result  # unicode middle dot

    def test_short_string_not_truncated(self) -> None:
        """Short strings (<=10 chars or no 'T') are not truncated."""
        evidence = {'risk_class': 'sensitive'}
        result = _format_evidence_line(evidence)
        assert 'sensitive' in result

    def test_long_string_without_t_not_truncated(self) -> None:
        """Long strings without 'T' are NOT truncated (only ISO timestamps are)."""
        evidence = {'flag_reason': 'this is a longer reason string'}
        result = _format_evidence_line(evidence)
        assert 'this is a longer reason string' in result


# ---------------------------------------------------------------------------
# _format_unit_meta_line tests
# ---------------------------------------------------------------------------


class TestFormatUnitMetaLine:
    def test_formats_date_note_status(self) -> None:
        """All three fields are included when present."""
        meta = UnitMeta(unit_id='abc', date='2026-01-15', note_name='my-note', status='active')
        result = _format_unit_meta_line(meta)
        assert 'created: 2026-01-15' in result
        assert 'note: my-note' in result
        assert 'status: active' in result

    def test_returns_empty_when_all_none(self) -> None:
        """When all display fields are None, returns empty string."""
        meta = UnitMeta(unit_id='abc', date=None, note_name=None, status=None)
        result = _format_unit_meta_line(meta)
        assert result == ''

    def test_partial_fields(self) -> None:
        """When some fields are None, only present fields appear."""
        meta = UnitMeta(unit_id='abc', date='2026-03-01', note_name=None, status='stale')
        result = _format_unit_meta_line(meta)
        assert 'created: 2026-03-01' in result
        assert 'status: stale' in result
        assert 'note:' not in result

    def test_only_date(self) -> None:
        """When only date is set."""
        meta = UnitMeta(unit_id='abc', date='2026-05-20', note_name=None, status=None)
        result = _format_unit_meta_line(meta)
        assert 'created: 2026-05-20' in result
        assert result != ''

    def test_only_note_name(self) -> None:
        """When only note_name is set."""
        meta = UnitMeta(unit_id='abc', date=None, note_name='test-note', status=None)
        result = _format_unit_meta_line(meta)
        assert 'note: test-note' in result

    def test_output_has_dim_markup(self) -> None:
        """Output is wrapped in Rich [dim] markup."""
        meta = UnitMeta(unit_id='abc', date='2026-01-01', note_name=None, status=None)
        result = _format_unit_meta_line(meta)
        assert '[dim]' in result
        assert '[/dim]' in result

    def test_fields_separated_by_middle_dot(self) -> None:
        """Multiple fields are separated by a middle-dot separator."""
        meta = UnitMeta(unit_id='abc', date='2026-01-01', note_name='note', status='active')
        result = _format_unit_meta_line(meta)
        assert '·' in result  # unicode middle dot
