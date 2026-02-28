"""Property-based tests for entity resolution using Hypothesis.

Tests target pure functions: _prepare_inputs, normalize_name, calculate_match_score.
"""

from datetime import datetime, timezone
from uuid import uuid4

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from memex_core.memory.entity_resolver import (
    EntityCandidate,
    EntityResolver,
    calculate_match_score,
)
from memex_core.memory.utils import normalize_name, get_phonetic_code


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# General entity names (may include Unicode)
entity_name_st = st.text(
    alphabet=st.characters(whitelist_categories=('L', 'N', 'Zs'), min_codepoint=32),
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip() != '')

# ASCII-only entity names (for properties that depend on normalize_name's ASCII regex)
ascii_entity_name_st = st.text(
    alphabet=st.characters(
        whitelist_categories=('L', 'N', 'Zs'), min_codepoint=32, max_codepoint=127
    ),
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip() != '')

entity_type_st = st.sampled_from([None, 'person', 'organization', 'location', 'concept'])

event_date_st = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)

nearby_entity_st = st.lists(
    st.fixed_dictionaries({'text': entity_name_st}),
    min_size=0,
    max_size=5,
)

entity_data_st = st.fixed_dictionaries(
    {
        'text': entity_name_st,
        'entity_type': entity_type_st,
        'event_date': event_date_st,
        'nearby_entities': nearby_entity_st,
    }
)

entity_batch_st = st.lists(entity_data_st, min_size=1, max_size=20)


# ---------------------------------------------------------------------------
# normalize_name properties
# ---------------------------------------------------------------------------


class TestNormalizeNameProperties:
    @given(name=entity_name_st)
    @settings(max_examples=200)
    def test_idempotent(self, name: str) -> None:
        """Normalizing an already-normalized name should produce the same result."""
        once = normalize_name(name)
        twice = normalize_name(once)
        assert once == twice

    @given(name=entity_name_st)
    @settings(max_examples=200)
    def test_lowercase(self, name: str) -> None:
        """Result should always be lowercase."""
        result = normalize_name(name)
        assert result == result.lower()

    @given(name=entity_name_st)
    @settings(max_examples=200)
    def test_no_leading_trailing_whitespace(self, name: str) -> None:
        """Result should never have leading/trailing whitespace."""
        result = normalize_name(name)
        assert result == result.strip()

    @given(name=st.text(min_size=0, max_size=5))
    @settings(max_examples=100)
    def test_empty_or_whitespace_returns_empty(self, name: str) -> None:
        """Empty or whitespace-only input should return empty string."""
        if not name.strip():
            assert normalize_name(name) == ''

    @given(name=ascii_entity_name_st)
    @settings(max_examples=200)
    def test_case_insensitive(self, name: str) -> None:
        """Names differing only in case should normalize to the same string.

        Uses ASCII-only names because normalize_name strips non-ASCII chars,
        and some Unicode chars (e.g. Turkish dotless-i) have asymmetric case mapping.
        """
        assert normalize_name(name.upper()) == normalize_name(name.lower())


# ---------------------------------------------------------------------------
# _prepare_inputs properties
# ---------------------------------------------------------------------------


class TestPrepareInputsProperties:
    def setup_method(self) -> None:
        self.resolver = EntityResolver()
        self.default_date = datetime(2025, 1, 1, tzinfo=timezone.utc)

    @given(batch=entity_batch_st)
    @settings(max_examples=100)
    def test_conservation_of_indices(self, batch: list[dict]) -> None:
        """Every original index must appear in exactly one output's indices list."""
        inputs = self.resolver._prepare_inputs(batch, self.default_date)

        all_indices: list[int] = []
        for inp in inputs:
            all_indices.extend(inp.indices)

        # Every original index should appear exactly once
        assert sorted(all_indices) == list(range(len(batch)))

    @given(batch=entity_batch_st)
    @settings(max_examples=100)
    def test_deduplication_reduces_or_preserves_count(self, batch: list[dict]) -> None:
        """Output should have <= input count (deduplication merges)."""
        inputs = self.resolver._prepare_inputs(batch, self.default_date)
        assert len(inputs) <= len(batch)

    @given(batch=entity_batch_st)
    @settings(max_examples=100)
    def test_no_empty_indices(self, batch: list[dict]) -> None:
        """Every output entity should reference at least one original index."""
        inputs = self.resolver._prepare_inputs(batch, self.default_date)
        for inp in inputs:
            assert len(inp.indices) >= 1

    @given(batch=entity_batch_st)
    @settings(max_examples=100)
    def test_event_dates_are_timezone_aware(self, batch: list[dict]) -> None:
        """All output event dates should be timezone-aware."""
        inputs = self.resolver._prepare_inputs(batch, self.default_date)
        for inp in inputs:
            assert inp.event_date.tzinfo is not None

    @given(name=ascii_entity_name_st)
    @settings(max_examples=100)
    def test_same_name_different_case_deduplicates(self, name: str) -> None:
        """Two inputs with the same name but different case should be merged.

        Uses ASCII-only names because normalize_name strips non-ASCII chars,
        and some Unicode chars (e.g. Turkish dotless-i) have asymmetric case mapping.
        """
        assume(normalize_name(name) != '')

        batch: list[dict[str, object]] = [
            {
                'text': name.lower(),
                'entity_type': None,
                'event_date': self.default_date,
                'nearby_entities': [],
            },
            {
                'text': name.upper(),
                'entity_type': None,
                'event_date': self.default_date,
                'nearby_entities': [],
            },
        ]
        inputs = self.resolver._prepare_inputs(batch, self.default_date)

        # Should be deduplicated to 1 group (same normalized name)
        assert len(inputs) == 1
        assert sorted(inputs[0].indices) == [0, 1]


# ---------------------------------------------------------------------------
# calculate_match_score properties
# ---------------------------------------------------------------------------


class TestCalculateMatchScoreProperties:
    @given(
        name_sim=st.floats(min_value=0.0, max_value=1.0),
        date=event_date_st,
    )
    @settings(max_examples=200)
    def test_score_bounded_0_to_1(self, name_sim: float, date: datetime) -> None:
        """Match score should always be in [0, 1]."""
        candidate = EntityCandidate(
            id=str(uuid4()),
            canonical_name='test',
            last_seen=date,
            name_similarity_score=name_sim,
        )
        score = calculate_match_score(
            candidate=candidate,
            input_date=date,
            input_nearby_names=set(),
            known_neighbors={},
        )
        assert 0.0 <= score <= 1.0

    @given(
        name_sim=st.floats(min_value=0.0, max_value=1.0),
    )
    @settings(max_examples=200)
    def test_zero_similarity_low_score(self, name_sim: float) -> None:
        """With no co-occurrence and no temporal data, score depends on name similarity."""
        candidate = EntityCandidate(
            id=str(uuid4()),
            canonical_name='test',
            last_seen=None,
            name_similarity_score=name_sim,
        )
        score = calculate_match_score(
            candidate=candidate,
            input_date=datetime.now(timezone.utc),
            input_nearby_names=set(),
            known_neighbors={},
        )
        # Score should be roughly name_sim * 0.5 (50% weight for name)
        expected = name_sim * 0.5
        # Phonetic boost can raise it
        if not candidate.phonetic_match:
            assert abs(score - expected) < 0.01

    @given(
        name_sim=st.floats(min_value=0.0, max_value=1.0),
        date=event_date_st,
    )
    @settings(max_examples=200)
    def test_monotonic_in_name_similarity(self, name_sim: float, date: datetime) -> None:
        """Higher name similarity should produce higher or equal score (no co-occurrence)."""
        base_candidate = EntityCandidate(
            id=str(uuid4()),
            canonical_name='test',
            last_seen=date,
            name_similarity_score=name_sim,
        )

        better_candidate = EntityCandidate(
            id=str(uuid4()),
            canonical_name='test',
            last_seen=date,
            name_similarity_score=min(name_sim + 0.1, 1.0),
        )

        base_score = calculate_match_score(
            candidate=base_candidate,
            input_date=date,
            input_nearby_names=set(),
            known_neighbors={},
        )
        better_score = calculate_match_score(
            candidate=better_candidate,
            input_date=date,
            input_nearby_names=set(),
            known_neighbors={},
        )
        assert better_score >= base_score


# ---------------------------------------------------------------------------
# get_phonetic_code properties
# ---------------------------------------------------------------------------


class TestPhoneticCodeProperties:
    @given(name=entity_name_st)
    @settings(max_examples=200)
    def test_idempotent_through_normalize(self, name: str) -> None:
        """Phonetic code should be the same whether computed on raw or normalized name."""
        code_raw = get_phonetic_code(name)
        code_norm = get_phonetic_code(normalize_name(name))
        assert code_raw == code_norm

    @given(name=ascii_entity_name_st)
    @settings(max_examples=200)
    def test_case_insensitive(self, name: str) -> None:
        """Phonetic code should be case-insensitive.

        Uses ASCII-only names because normalize_name strips non-ASCII chars,
        and some Unicode chars (e.g. Turkish dotless-i) have asymmetric case mapping
        that causes different phonetic codes for upper vs lower.
        """
        code_lower = get_phonetic_code(name.lower())
        code_upper = get_phonetic_code(name.upper())
        assert code_lower == code_upper
