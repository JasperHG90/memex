"""Tests for vault summary DSPy signatures."""

import json

import dspy
from dspy.utils.dummies import DummyLM

from memex_core.services.vault_summary_signatures import (
    VaultSummaryFullSignature,
    VaultSummaryUpdateSignature,
    VaultTopicExtractSignature,
    VaultTopicMergeSignature,
)


class TestVaultSummaryUpdateSignature:
    def test_input_fields(self):
        fields = VaultSummaryUpdateSignature.input_fields
        assert 'current_summary' in fields
        assert 'current_topics_json' in fields
        assert 'new_notes_json' in fields
        assert 'vault_stats_json' in fields

    def test_output_fields(self):
        fields = VaultSummaryUpdateSignature.output_fields
        assert 'updated_summary' in fields
        assert 'updated_topics_json' in fields

    def test_predict_with_dummy_lm(self):
        topics = [{'name': 'AI', 'note_count': 4, 'description': 'AI and ML research'}]
        lm = DummyLM(
            [
                {
                    'updated_summary': 'Updated summary with ML note.',
                    'updated_topics_json': json.dumps(topics),
                }
            ]
        )
        with dspy.context(lm=lm):
            predictor = dspy.Predict(VaultSummaryUpdateSignature)
            result = predictor(
                current_summary='Vault contains AI research.',
                current_topics_json=json.dumps(
                    [{'name': 'AI', 'note_count': 3, 'description': 'AI research'}]
                ),
                new_notes_json=json.dumps(
                    [
                        {
                            'title': 'ML Optimization',
                            'description': 'A study on ML optimization techniques.',
                            'summaries': [
                                {'topic': 'ML Optimization', 'key_points': ['Gradient methods']}
                            ],
                            'tags': ['ml'],
                            'template': 'general_note',
                            'author': 'test',
                            'source_domain': '',
                            'publish_date': '2026-04-04',
                        }
                    ]
                ),
                vault_stats_json=json.dumps({'total_notes': 4, 'new_since_last': 1}),
            )
        assert 'ML' in result.updated_summary
        parsed = json.loads(result.updated_topics_json)
        assert isinstance(parsed, list)
        assert parsed[0]['note_count'] == 4


class TestVaultSummaryFullSignature:
    def test_input_fields(self):
        fields = VaultSummaryFullSignature.input_fields
        assert 'notes_json' in fields
        assert 'vault_note_count' in fields

    def test_output_fields(self):
        fields = VaultSummaryFullSignature.output_fields
        assert 'summary' in fields
        assert 'topics_json' in fields

    def test_predict_with_dummy_lm(self):
        topics = [
            {'name': 'AI', 'note_count': 1, 'description': 'Artificial intelligence'},
            {'name': 'Databases', 'note_count': 1, 'description': 'Database systems'},
        ]
        lm = DummyLM(
            [
                {
                    'summary': 'A vault about AI and databases.',
                    'topics_json': json.dumps(topics),
                }
            ]
        )
        with dspy.context(lm=lm):
            predictor = dspy.Predict(VaultSummaryFullSignature)
            result = predictor(
                notes_json=json.dumps(
                    [
                        {'title': 'Note 1', 'description': 'About AI'},
                        {'title': 'Note 2', 'description': 'About databases'},
                    ]
                ),
                vault_note_count=2,
            )
        assert len(result.summary) > 0
        parsed = json.loads(result.topics_json)
        assert len(parsed) == 2


class TestVaultTopicExtractSignature:
    def test_input_fields(self):
        fields = VaultTopicExtractSignature.input_fields
        assert 'notes_json' in fields
        assert 'batch_index' in fields
        assert 'total_batches' in fields

    def test_output_fields(self):
        fields = VaultTopicExtractSignature.output_fields
        assert 'topics_json' in fields
        assert 'batch_summary' in fields

    def test_predict_with_dummy_lm(self):
        lm = DummyLM(
            [
                {
                    'topics_json': json.dumps(
                        [
                            {'name': 'General', 'note_count': 10, 'description': 'General topics'},
                        ]
                    ),
                    'batch_summary': 'Batch of 10 general notes.',
                }
            ]
        )
        notes = [{'title': f'Note {i}', 'description': f'Desc {i}'} for i in range(10)]
        with dspy.context(lm=lm):
            predictor = dspy.Predict(VaultTopicExtractSignature)
            result = predictor(
                notes_json=json.dumps(notes),
                batch_index=0,
                total_batches=5,
            )
        parsed = json.loads(result.topics_json)
        assert len(parsed) >= 1
        assert len(result.batch_summary) > 0


class TestVaultTopicMergeSignature:
    def test_input_fields(self):
        fields = VaultTopicMergeSignature.input_fields
        assert 'batch_topics_json' in fields
        assert 'vault_note_count' in fields

    def test_output_fields(self):
        fields = VaultTopicMergeSignature.output_fields
        assert 'summary' in fields
        assert 'topics_json' in fields

    def test_predict_with_dummy_lm(self):
        merged = [
            {'name': 'AI', 'note_count': 5, 'description': 'AI topics'},
            {'name': 'Databases', 'note_count': 3, 'description': 'Database topics'},
        ]
        lm = DummyLM(
            [
                {
                    'summary': 'A vault about AI and databases with 8 total notes.',
                    'topics_json': json.dumps(merged),
                }
            ]
        )
        batch_results = [
            {
                'batch_index': 0,
                'topics': [{'name': 'AI', 'note_count': 5, 'description': 'AI topics'}],
                'batch_summary': 'AI-focused batch.',
            },
            {
                'batch_index': 1,
                'topics': [{'name': 'DB', 'note_count': 3, 'description': 'Database topics'}],
                'batch_summary': 'Database-focused batch.',
            },
        ]
        with dspy.context(lm=lm):
            predictor = dspy.Predict(VaultTopicMergeSignature)
            result = predictor(
                batch_topics_json=json.dumps(batch_results),
                vault_note_count=8,
            )
        assert len(result.summary) > 0
        parsed = json.loads(result.topics_json)
        assert len(parsed) == 2
