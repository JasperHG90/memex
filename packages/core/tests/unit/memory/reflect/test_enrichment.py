"""Tests for enrichment models and signature validation."""

import dspy

from memex_core.memory.reflect.prompts import (
    EnrichedTagSet,
    EnrichmentSignature,
)


class TestEnrichedTagSet:
    def test_valid_inputs_accepted(self):
        tag_set = EnrichedTagSet(
            memory_index=0,
            enriched_tags=['compliance', 'eu-regulation'],
            enriched_keywords=['gdpr', 'data protection'],
        )
        assert tag_set.memory_index == 0
        assert tag_set.enriched_tags == ['compliance', 'eu-regulation']
        assert tag_set.enriched_keywords == ['gdpr', 'data protection']

    def test_empty_lists_accepted(self):
        tag_set = EnrichedTagSet(
            memory_index=3,
            enriched_tags=[],
            enriched_keywords=[],
        )
        assert tag_set.enriched_tags == []
        assert tag_set.enriched_keywords == []


class TestEnrichmentSignature:
    def test_input_fields(self):
        fields = EnrichmentSignature.input_fields
        assert 'entity_name' in fields
        assert 'entity_summary' in fields
        assert 'observations' in fields
        assert 'memories' in fields

    def test_output_fields(self):
        fields = EnrichmentSignature.output_fields
        assert 'enrichments' in fields

    def test_predictor_can_be_created(self):
        predictor = dspy.Predict(EnrichmentSignature)
        assert predictor is not None
