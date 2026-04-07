"""Tests for vault summary DSPy signatures and Pydantic models."""

from memex_core.services.vault_summary_signatures import (
    BatchResult,
    LLMTheme,
    NoteMetadata,
    VaultStats,
    VaultSummaryFullSignature,
    VaultSummaryUpdateSignature,
    VaultTopicExtractSignature,
    VaultTopicMergeSignature,
)


# ─── Pydantic model tests ───


class TestLLMThemeModel:
    def test_required_fields(self):
        t = LLMTheme(name='AI', description='AI research', note_count=5, trend='stable')
        assert t.note_count == 5
        assert t.trend == 'stable'
        assert t.representative_titles == []

    def test_with_representative_titles(self):
        t = LLMTheme(
            name='AI',
            description='AI research',
            note_count=3,
            trend='growing',
            representative_titles=['Paper A', 'Paper B'],
        )
        assert t.note_count == 3
        assert t.trend == 'growing'
        assert len(t.representative_titles) == 2

    def test_model_dump_roundtrip(self):
        t = LLMTheme(name='AI', description='AI research', note_count=5, trend='dormant')
        d = t.model_dump()
        assert d['name'] == 'AI'
        assert d['note_count'] == 5
        assert d['trend'] == 'dormant'
        t2 = LLMTheme(**d)
        assert t2 == t


class TestNoteMetadataModel:
    def test_minimal(self):
        n = NoteMetadata(title='Test')
        assert n.title == 'Test'
        assert n.tags == []
        assert n.summaries == []

    def test_full(self):
        n = NoteMetadata(
            title='ML Paper',
            publish_date='2026-04-01',
            tags=['ml'],
            template='article',
            author='Test',
            source_domain='arxiv.org',
            description='About ML',
            summaries=[{'topic': 'ML', 'key_points': ['Point 1']}],
        )
        assert n.source_domain == 'arxiv.org'


class TestVaultStatsModel:
    def test_defaults(self):
        s = VaultStats(total_notes=10)
        assert s.new_since_last == 0
        assert s.max_narrative_tokens == 200


class TestBatchResultModel:
    def test_with_themes(self):
        br = BatchResult(
            batch_index=0,
            themes=[LLMTheme(name='AI', description='AI', note_count=3, trend='growing')],
            batch_summary='AI batch',
        )
        assert len(br.themes) == 1
        assert br.themes[0].name == 'AI'
        assert br.themes[0].note_count == 3
        assert br.themes[0].trend == 'growing'


# ─── Signature field tests ───


class TestVaultSummaryUpdateSignature:
    def test_input_fields(self):
        fields = VaultSummaryUpdateSignature.input_fields
        assert 'current_narrative' in fields
        assert 'current_themes' in fields
        assert 'new_notes' in fields
        assert 'vault_stats' in fields

    def test_output_fields(self):
        fields = VaultSummaryUpdateSignature.output_fields
        assert 'updated_narrative' in fields
        assert 'updated_themes' in fields


class TestVaultSummaryFullSignature:
    def test_input_fields(self):
        fields = VaultSummaryFullSignature.input_fields
        assert 'notes' in fields
        assert 'vault_note_count' in fields
        assert 'max_narrative_tokens' in fields

    def test_output_fields(self):
        fields = VaultSummaryFullSignature.output_fields
        assert 'narrative' in fields
        assert 'themes' in fields


class TestVaultTopicExtractSignature:
    def test_input_fields(self):
        fields = VaultTopicExtractSignature.input_fields
        assert 'notes' in fields
        assert 'batch_index' in fields
        assert 'total_batches' in fields

    def test_output_fields(self):
        fields = VaultTopicExtractSignature.output_fields
        assert 'themes' in fields
        assert 'batch_summary' in fields


class TestVaultTopicMergeSignature:
    def test_input_fields(self):
        fields = VaultTopicMergeSignature.input_fields
        assert 'batch_results' in fields
        assert 'vault_note_count' in fields

    def test_output_fields(self):
        fields = VaultTopicMergeSignature.output_fields
        assert 'narrative' in fields
        assert 'themes' in fields
