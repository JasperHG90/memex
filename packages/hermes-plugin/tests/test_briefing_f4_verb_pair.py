"""F4 — Hermes briefing verb-pair invariant (Wave 0 §6 #12).

Asserts the Hermes routing guide:
1. Mentions `memex_memory_deprioritize` as the NON-destructive verb.
2. Mentions archive as the DESTRUCTIVE counterpart.
3. Does NOT cross-wire archive as the deprioritize-equivalent (negative
   assertion — no instruction like "use archive when you want to deprioritize").

Pure string-contains; the matching `@pytest.mark.llm` driven-agent verb
selection lives in `tests/test_e2e_f4_llm_turn.py`.
"""

from __future__ import annotations

from memex_hermes_plugin.memex.briefing import _ROUTING_GUIDE


def test_routing_guide_mentions_deprioritize_verb():
    assert 'memex_memory_deprioritize' in _ROUTING_GUIDE


def test_routing_guide_marks_deprioritize_as_non_destructive():
    text = _ROUTING_GUIDE
    # Per Wave 0 §6 #12: deprioritize is the NON-destructive verb.
    assert 'NON-DESTRUCTIVE' in text or 'non-destructive' in text.lower()


def test_routing_guide_marks_archive_as_destructive():
    text = _ROUTING_GUIDE.lower()
    assert 'archive' in text
    assert 'destructive' in text


def test_routing_guide_does_not_cross_wire_archive_as_deprioritize_alt():
    """Negative assertion: the briefing must NOT suggest archive as a
    same-purpose alternative to deprioritize. Wave 0 §6 #12 keeps these
    verbs distinct.
    """
    lower = _ROUTING_GUIDE.lower()
    forbidden_phrases = (
        'use archive instead of deprioritize',
        'archive is equivalent to deprioritize',
        'archive or deprioritize',
    )
    for p in forbidden_phrases:
        assert p not in lower, f'Briefing cross-wires archive: contains "{p}"'
