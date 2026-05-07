"""Unit coverage for ``_sanitise_evidence_text`` in the contradiction engine.

Pins the runtime sanitisation contract that migration 035's SQL backfill
mirrors: control-char stripping, edge whitespace trim, ellipsis truncation
at the cap, ``None`` on empty/None input, and the pre-truncation guard
that bounds work for pathological multi-MB payloads.
"""

from __future__ import annotations

import pytest

from memex_core.memory.contradiction.engine import _sanitise_evidence_text


def test_returns_none_for_none_input() -> None:
    assert _sanitise_evidence_text(None, max_len=100) is None


def test_returns_none_for_empty_string() -> None:
    assert _sanitise_evidence_text('', max_len=100) is None


def test_returns_none_for_whitespace_only_input() -> None:
    assert _sanitise_evidence_text('   \t\n  ', max_len=100) is None


def test_returns_none_for_input_that_is_only_controls() -> None:
    assert _sanitise_evidence_text('\x01\x02\x03\x7f\x9f', max_len=100) is None


def test_short_clean_text_passes_through_verbatim() -> None:
    assert _sanitise_evidence_text('hello world', max_len=100) == 'hello world'


def test_strips_c0_controls_except_tab_newline() -> None:
    out = _sanitise_evidence_text('a\x01b\tc\nd\x0ee', max_len=100)
    assert out == 'a' + 'b\tc\nd' + 'e'


def test_strips_del_and_c1_controls() -> None:
    out = _sanitise_evidence_text('a\x7fb' + chr(0x80) + 'c' + chr(0x9F) + 'd', max_len=100)
    assert out == 'abcd'


def test_trims_leading_and_trailing_whitespace() -> None:
    assert _sanitise_evidence_text('  hello  ', max_len=100) == 'hello'


def test_truncates_with_ellipsis_at_cap() -> None:
    out = _sanitise_evidence_text('A' * 1500, max_len=1000)
    assert out is not None
    assert len(out) == 1000
    assert out.endswith('…')
    assert out[:-1] == 'A' * 999


def test_no_ellipsis_when_under_cap() -> None:
    out = _sanitise_evidence_text('A' * 100, max_len=1000)
    assert out == 'A' * 100


def test_no_ellipsis_when_exactly_at_cap() -> None:
    out = _sanitise_evidence_text('A' * 1000, max_len=1000)
    assert out == 'A' * 1000  # equality, not '>', so no ellipsis


def test_pre_truncation_bounds_work_for_huge_input() -> None:
    """A 10 MB payload of NULs must not iterate the full length.

    Pre-fix the sanitiser scanned every char in Python before stripping;
    post-fix the helper truncates to ``max_len * 4`` upfront and lets the
    compiled regex do the per-char work in C. The functional assertion
    here is that the call returns ``None`` (everything was a stripped
    control) without timing out.
    """
    huge = '\x00' * (10 * 1024 * 1024)
    assert _sanitise_evidence_text(huge, max_len=1000) is None


def test_pre_truncation_preserves_first_max_len_4_chars_of_real_content() -> None:
    """The pre-truncation cap must be wide enough that real content within
    the first ``max_len`` chars survives, even when the input is huge.
    """
    prefix = 'The first 50 chars of real content. ' + ('A' * 14)  # 50 chars
    huge = prefix + ('B' * (10 * 1024 * 1024))
    out = _sanitise_evidence_text(huge, max_len=100)
    assert out is not None
    assert out.startswith('The first 50 chars of real content.')
    assert len(out) == 100
    assert out.endswith('…')


def test_non_string_input_is_coerced() -> None:
    assert _sanitise_evidence_text(42, max_len=100) == '42'


@pytest.mark.parametrize('cap', [1, 2, 10, 100, 1000])
def test_truncation_obeys_arbitrary_caps(cap: int) -> None:
    out = _sanitise_evidence_text('X' * (cap * 3), max_len=cap)
    assert out is not None
    assert len(out) == cap
    assert out.endswith('…')
