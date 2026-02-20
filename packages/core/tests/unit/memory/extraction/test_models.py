import pytest
from pydantic import ValidationError

from memex_core.memory.extraction.models import CausalRelation, RawFact
from memex_core.types import CausalRelationshipTypes, FactKindTypes, FactTypes


class TestRawFact:
    """Tests for RawFact model."""

    def test_normalize_type_assistant_to_experience(self) -> None:
        """Test that 'assistant' fact type is normalized to 'experience'."""
        fact = RawFact(
            what='Test',
            fact_type='assistant',  # Testing string input normalization
            fact_kind=FactKindTypes.CONVERSATION,
        )
        assert fact.fact_type == FactTypes.EXPERIENCE

    def test_normalize_type_other_remains(self) -> None:
        """Test that other fact types remain unchanged."""
        fact = RawFact(
            what='Test',
            fact_type=FactTypes.WORLD,
            fact_kind=FactKindTypes.CONVERSATION,
        )
        assert fact.fact_type == FactTypes.WORLD

    @pytest.mark.parametrize(
        ('what', 'when', 'who', 'why', 'expected'),
        [
            (
                'Fact',
                'Today',
                'User',
                'Reason',
                'Fact | When: Today | Involving: User | Reason',
            ),
            ('Fact', 'N/A', 'N/A', 'N/A', 'Fact'),
            ('Fact', None, None, None, 'Fact'),
            ('Fact', 'Today', 'N/A', 'Reason', 'Fact | When: Today | Reason'),
        ],
    )
    def test_formatted_text(
        self,
        what: str,
        when: str | None,
        who: str | None,
        why: str | None,
        expected: str,
    ) -> None:
        """Test formatted_text property logic."""
        fact = RawFact(
            what=what,
            when=when,
            who=who,
            why=why,
            fact_type=FactTypes.WORLD,
            fact_kind=FactKindTypes.CONVERSATION,
        )
        assert fact.formatted_text == expected


class TestCausalRelation:
    """Tests for CausalRelation model."""

    @pytest.mark.parametrize('strength', [0.0, 0.5, 1.0])
    def test_valid_strength(self, strength: float) -> None:
        """Test valid strength values."""
        relation = CausalRelation(
            relationship_type=CausalRelationshipTypes.CAUSED_BY,
            target_fact_index=0,
            strength=strength,
        )
        assert relation.strength == strength

    @pytest.mark.parametrize('strength', [-0.1, 1.1])
    def test_invalid_strength(self, strength: float) -> None:
        """Test invalid strength values raise ValidationError."""
        with pytest.raises(ValidationError):
            CausalRelation(
                relationship_type=CausalRelationshipTypes.CAUSED_BY,
                target_fact_index=0,
                strength=strength,
            )
