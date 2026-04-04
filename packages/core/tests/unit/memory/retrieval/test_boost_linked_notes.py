"""Unit tests for NoteSearchEngine._boost_linked_notes (Feature E: Note Linking)."""

import pytest
from uuid import uuid4

from memex_common.schemas import NoteSearchResult
from memex_core.memory.retrieval.document_search import (
    LINKED_NOTE_BOOST,
    MIN_TITLE_LENGTH,
    NoteSearchEngine,
)


def _make_result(note_id=None, title='Test Note', score=0.05):
    return NoteSearchResult(
        note_id=note_id or uuid4(),
        metadata={'title': title},
        score=score,
    )


class TestBoostLinkedNotes:
    def test_default_boost_value(self):
        """AC-E03: Default boost is 0.15."""
        assert LINKED_NOTE_BOOST == 0.15

    def test_min_title_length_is_4(self):
        """Titles shorter than 4 chars are skipped to avoid false positives."""
        assert MIN_TITLE_LENGTH == 4

    def test_boost_applied_when_title_in_chunk_text(self):
        """AC-E01: Note whose title appears in another note's chunk text gets boosted."""
        note_a = _make_result(title='Redis Caching Analysis', score=0.10)
        note_b = _make_result(title='Performance Review', score=0.08)

        # note_b's chunk text mentions note_a's title
        best_chunk_text = {
            note_a.note_id: 'Some analysis of caching strategies',
            note_b.note_id: 'As discussed in Redis Caching Analysis, the approach works',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a, note_b], best_chunk_text)

        # note_a should be boosted because note_b mentions its title
        boosted_a = next(r for r in results if r.note_id == note_a.note_id)
        assert boosted_a.score == pytest.approx(0.10 + LINKED_NOTE_BOOST)

        # note_b should NOT be boosted (its title isn't mentioned in note_a's text)
        unboosted_b = next(r for r in results if r.note_id == note_b.note_id)
        assert unboosted_b.score == pytest.approx(0.08)

    def test_boost_is_case_insensitive(self):
        """Title matching is case-insensitive."""
        note_a = _make_result(title='Redis Caching', score=0.10)
        note_b = _make_result(title='Other Note', score=0.08)

        best_chunk_text = {
            note_a.note_id: 'unrelated text',
            note_b.note_id: 'mentions redis caching in lowercase',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a, note_b], best_chunk_text)

        boosted_a = next(r for r in results if r.note_id == note_a.note_id)
        assert boosted_a.score == pytest.approx(0.10 + LINKED_NOTE_BOOST)

    def test_short_titles_are_skipped(self):
        """Titles shorter than MIN_TITLE_LENGTH are not matched."""
        note_a = _make_result(title='API', score=0.10)  # 3 chars < 4
        note_b = _make_result(title='Other', score=0.08)

        best_chunk_text = {
            note_a.note_id: 'text',
            note_b.note_id: 'calls the API frequently',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a, note_b], best_chunk_text)

        # note_a should NOT be boosted (title too short)
        a = next(r for r in results if r.note_id == note_a.note_id)
        assert a.score == pytest.approx(0.10)

    def test_self_reference_does_not_boost(self):
        """A note mentioning its own title in its chunk text does not boost itself."""
        note_a = _make_result(title='Redis Analysis', score=0.10)

        best_chunk_text = {
            note_a.note_id: 'This is the Redis Analysis document',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a], best_chunk_text)

        assert results[0].score == pytest.approx(0.10)

    def test_results_re_sorted_after_boost(self):
        """After boosting, results are re-sorted by score descending."""
        note_a = _make_result(title='First Note', score=0.10)
        note_b = _make_result(title='Second Note', score=0.08)
        note_c = _make_result(title='Third Note', score=0.06)

        # note_a mentions note_c's title, boosting note_c from 0.06 to 0.21
        best_chunk_text = {
            note_a.note_id: 'Related to Third Note findings',
            note_b.note_id: 'unrelated text',
            note_c.note_id: 'some content',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a, note_b, note_c], best_chunk_text)

        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)
        # note_c should now be ranked higher after boost
        assert results[0].note_id == note_c.note_id

    def test_no_boost_when_no_title_match(self):
        """When no titles are mentioned in other notes, scores are unchanged."""
        note_a = _make_result(title='First Note', score=0.10)
        note_b = _make_result(title='Second Note', score=0.08)

        best_chunk_text = {
            note_a.note_id: 'unrelated content',
            note_b.note_id: 'also unrelated',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a, note_b], best_chunk_text)

        assert results[0].score == pytest.approx(0.10)
        assert results[1].score == pytest.approx(0.08)

    def test_empty_results_returns_empty(self):
        """Empty results list returns empty."""
        results = NoteSearchEngine._boost_linked_notes([], {})
        assert results == []

    def test_missing_chunk_text_is_handled(self):
        """Notes without chunk text in the dict are handled gracefully."""
        note_a = _make_result(title='First Note', score=0.10)
        note_b = _make_result(title='Second Note', score=0.08)

        # note_a has no chunk text entry
        best_chunk_text = {
            note_b.note_id: 'mentions First Note here',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a, note_b], best_chunk_text)

        # note_a should be boosted (note_b mentions it)
        a = next(r for r in results if r.note_id == note_a.note_id)
        assert a.score == pytest.approx(0.10 + LINKED_NOTE_BOOST)

    def test_boost_before_rerank_placement(self):
        """AC-E02: Verify boost changes input order that would be seen by a reranker."""
        note_a = _make_result(title='Important Analysis', score=0.10)
        note_b = _make_result(title='Summary Report', score=0.12)
        note_c = _make_result(title='Background Data', score=0.05)

        # note_c mentions note_a's title -> note_a gets boosted
        best_chunk_text = {
            note_a.note_id: 'analysis content',
            note_b.note_id: 'summary content',
            note_c.note_id: 'references the Important Analysis for context',
        }

        # Before boost, order is: B(0.12), A(0.10), C(0.05)
        results = [note_b, note_a, note_c]

        boosted = NoteSearchEngine._boost_linked_notes(results, best_chunk_text)

        # After boost, A should be 0.25, reordered above B
        note_ids = [r.note_id for r in boosted]
        assert note_ids[0] == note_a.note_id  # A now first due to boost

    def test_custom_boost_value(self):
        """Boost value can be overridden."""
        note_a = _make_result(title='Target Note', score=0.10)
        note_b = _make_result(title='Other', score=0.08)

        best_chunk_text = {
            note_a.note_id: 'content',
            note_b.note_id: 'mentions Target Note here',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a, note_b], best_chunk_text, boost=0.5)

        a = next(r for r in results if r.note_id == note_a.note_id)
        assert a.score == pytest.approx(0.10 + 0.5)

    def test_mutual_references_both_boosted(self):
        """When two notes reference each other's titles, both get boosted."""
        note_a = _make_result(title='Redis Guide', score=0.10)
        note_b = _make_result(title='Cache Strategy', score=0.08)

        best_chunk_text = {
            note_a.note_id: 'see also the Cache Strategy document',
            note_b.note_id: 'based on the Redis Guide approach',
        }

        results = NoteSearchEngine._boost_linked_notes([note_a, note_b], best_chunk_text)

        a = next(r for r in results if r.note_id == note_a.note_id)
        b = next(r for r in results if r.note_id == note_b.note_id)
        assert a.score == pytest.approx(0.10 + LINKED_NOTE_BOOST)
        assert b.score == pytest.approx(0.08 + LINKED_NOTE_BOOST)
