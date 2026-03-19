import pytest
from pydantic import ValidationError

from memex_core.memory.extraction.models import CausalRelation, RawFact
from memex_core.types import CausalRelationshipTypes, FactKindTypes, FactTypes


class TestRawFact:
    """Tests for RawFact model."""

    def test_normalize_type_assistant_to_event(self) -> None:
        """Test that 'assistant' fact type is normalized to 'event'."""
        fact = RawFact(
            what='Test',
            fact_type='assistant',  # Testing string input normalization
            fact_kind=FactKindTypes.CONVERSATION,
        )
        assert fact.fact_type == FactTypes.EVENT

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


class TestChunkMetadata:
    """Tests for ChunkMetadata summary fields."""

    def test_chunk_metadata_with_summary(self) -> None:
        from memex_core.memory.extraction.models import ChunkMetadata

        summary = {'topic': 'AI Safety', 'key_points': ['Alignment', 'Interpretability']}
        cm = ChunkMetadata(
            chunk_text='text',
            fact_count=0,
            content_index=0,
            chunk_index=0,
            content_hash='h1',
            summary=summary,
            summary_formatted='AI Safety — Alignment | Interpretability',
        )
        assert cm.summary == summary
        assert cm.summary_formatted == 'AI Safety — Alignment | Interpretability'

    def test_chunk_metadata_summary_defaults_to_none(self) -> None:
        from memex_core.memory.extraction.models import ChunkMetadata

        cm = ChunkMetadata(
            chunk_text='text',
            fact_count=0,
            content_index=0,
            chunk_index=0,
            content_hash='h1',
        )
        assert cm.summary is None
        assert cm.summary_formatted is None


class TestBlockSummaryFormatted:
    """Tests for BlockSummary.formatted property."""

    def test_formatted_with_key_points(self) -> None:
        from memex_core.memory.extraction.models import BlockSummary

        bs = BlockSummary(topic='ML Ops', key_points=['CI/CD', 'Monitoring'])
        assert bs.formatted == 'ML Ops — CI/CD | Monitoring'

    def test_formatted_without_key_points(self) -> None:
        from memex_core.memory.extraction.models import BlockSummary

        bs = BlockSummary(topic='Overview')
        assert bs.formatted == 'Overview'

    def test_model_dump_roundtrip(self) -> None:
        from memex_core.memory.extraction.models import BlockSummary

        bs = BlockSummary(topic='Test', key_points=['A', 'B'])
        d = bs.model_dump()
        assert d == {'topic': 'Test', 'key_points': ['A', 'B']}
        restored = BlockSummary(**d)
        assert restored.topic == bs.topic
        assert restored.key_points == bs.key_points
