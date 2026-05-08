"""Unit coverage for ``_sanitise_evidence_text`` in the contradiction engine.

Pins the runtime sanitisation contract that migration 035's SQL backfill
mirrors: control-char stripping (incl. CR, DEL, C1), ASCII edge-whitespace
trim (deliberately narrow so Postgres ``\\s`` and Python parity hold),
ellipsis truncation at the cap, ``None`` on empty/None input, and a
pathological-input perf check (no pre-truncate guard — that was removed
because it dropped real content past the cut).
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


def test_strips_carriage_return() -> None:
    """CR is stripped — it is terminal-hostile (line-overwrite). The
    earlier per-char loop stripped it; the regex refactor must too.
    """
    assert _sanitise_evidence_text('a\rb\r\nc', max_len=100) == 'a' + 'b\nc'


def test_strips_form_feed_and_vertical_tab() -> None:
    """``\\f`` (0x0C) and ``\\v`` (0x0B) are C0 controls — stripped."""
    assert _sanitise_evidence_text('a\x0bb\x0cc', max_len=100) == 'abc'


def test_does_not_strip_nbsp_or_other_unicode_whitespace_at_edges() -> None:
    """Edge trim is ASCII-only — matches Postgres POSIX ``\\s`` and the
    migration's regex. NBSP (``U+00A0``) and ideographic space
    (``U+3000``) at edges must survive so runtime and backfill agree.
    """
    out = _sanitise_evidence_text(' foo　', max_len=100)
    assert out == ' foo　'


def test_huge_pathological_nul_input_completes_quickly() -> None:
    """10 MB of NULs must process in under a second via the compiled
    regex without iterating Python per-char. There is no pre-truncation
    guard — the regex runs the per-char work in C.
    """
    import time

    huge = '\x00' * (10 * 1024 * 1024)
    start = time.monotonic()
    assert _sanitise_evidence_text(huge, max_len=1000) is None
    assert time.monotonic() - start < 1.0, 'sanitiser too slow on pathological input'


def test_real_content_after_a_long_run_of_controls_survives() -> None:
    """A small real prefix + 1 MB of NULs + a real suffix must still
    yield the suffix in the output (capped by ``max_len`` truncation
    rules, not by an arbitrary pre-truncate). This pins behaviour
    against any future re-introduction of the ``max_len * 4`` guard
    that drops trailing real content.
    """
    prefix = 'A' * 30  # short real prefix
    middle = '\x00' * (1024 * 1024)  # 1 MB of NULs
    suffix = 'B' * 30  # short real suffix
    out = _sanitise_evidence_text(prefix + middle + suffix, max_len=200)
    assert out is not None
    assert out.startswith('A' * 30)
    assert out.endswith('B' * 30)
    assert len(out) == 60


def test_non_string_input_is_coerced() -> None:
    assert _sanitise_evidence_text(42, max_len=100) == '42'


@pytest.mark.parametrize('cap', [1, 2, 10, 100, 1000])
def test_truncation_obeys_arbitrary_caps(cap: int) -> None:
    out = _sanitise_evidence_text('X' * (cap * 3), max_len=cap)
    assert out is not None
    assert len(out) == cap
    assert out.endswith('…')
