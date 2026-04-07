"""LLM integration tests for vault summary signatures.

Tests that a real LLM produces valid Theme objects through DSPy typed fields.
Requires GOOGLE_API_KEY (or ANTHROPIC_API_KEY) to be set.
"""

import os

import dspy
import pytest

from memex_core.llm import run_dspy_operation
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


def _skip_without_api_key():
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')


def _make_lm() -> dspy.LM:
    api_key = os.environ['GOOGLE_API_KEY']
    return dspy.LM(model='gemini/gemini-2.0-flash', api_key=api_key)


SAMPLE_NOTES = [
    NoteMetadata(
        title='ReAct: Synergizing Reasoning and Acting in Language Models',
        publish_date='2023-03-10',
        tags=['ai', 'agents', 'reasoning'],
        template='article',
        author='Yao et al.',
        source_domain='arxiv.org',
        description='Proposes interleaving reasoning traces and actions for LLM agents.',
        summaries=[{'topic': 'Agent reasoning', 'key_points': ['ReAct pattern', 'Tool use']}],
    ),
    NoteMetadata(
        title='Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks',
        publish_date='2020-05-22',
        tags=['ai', 'rag', 'retrieval'],
        template='article',
        author='Lewis et al.',
        source_domain='arxiv.org',
        description='Combines parametric and non-parametric memory for question answering.',
        summaries=[{'topic': 'RAG', 'key_points': ['Retrieval + generation', 'Knowledge base']}],
    ),
    NoteMetadata(
        title='Building a Personal Knowledge Graph with Memex',
        publish_date='2026-03-15',
        tags=['memex', 'knowledge-graph', 'personal'],
        template='quick-note',
        description='Notes on setting up a personal knowledge management system.',
        summaries=[{'topic': 'PKM', 'key_points': ['Entity extraction', 'Note linking']}],
    ),
]


def _validate_themes(themes: list, min_count: int = 1) -> None:
    """Assert that themes are valid LLMTheme objects with required fields."""
    assert isinstance(themes, list), f'Expected list, got {type(themes)}'
    assert len(themes) >= min_count, f'Expected >= {min_count} themes, got {len(themes)}'
    for t in themes:
        if isinstance(t, LLMTheme):
            assert t.name, 'LLMTheme must have a name'
            assert t.description, 'LLMTheme must have a description'
            assert isinstance(t.note_count, int), 'note_count must be an int'
            assert t.trend in ('growing', 'stable', 'dormant'), f'Invalid trend: {t.trend}'
            assert isinstance(t.representative_titles, list), 'representative_titles must be a list'
        elif isinstance(t, dict):
            assert 'name' in t, 'Theme dict must have name'
            assert 'description' in t, 'Theme dict must have description'
        else:
            pytest.fail(f'Unexpected theme type: {type(t)}')


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_full_signature_produces_typed_themes():
    """Real LLM generates valid Theme objects via VaultSummaryFullSignature."""
    _skip_without_api_key()
    lm = _make_lm()

    predictor = dspy.Predict(VaultSummaryFullSignature)
    result = await run_dspy_operation(
        lm=lm,
        predictor=predictor,
        input_kwargs={
            'notes': SAMPLE_NOTES,
            'vault_note_count': len(SAMPLE_NOTES),
            'max_narrative_tokens': 200,
        },
        operation_name='test_vault_summary_full',
    )

    assert result.narrative, 'Narrative must not be empty'
    assert len(result.narrative.split()) <= 250, 'Narrative should be concise'
    _validate_themes(result.themes)


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_update_signature_produces_typed_themes():
    """Real LLM generates valid Theme objects via VaultSummaryUpdateSignature."""
    _skip_without_api_key()
    lm = _make_lm()

    existing_themes = [
        LLMTheme(
            name='AI Agents',
            description='Research on autonomous AI agents',
            note_count=1,
            trend='growing',
        ),
    ]

    new_note = NoteMetadata(
        title='Toolformer: Language Models Can Teach Themselves to Use Tools',
        publish_date='2023-02-09',
        tags=['ai', 'tools', 'agents'],
        template='article',
        author='Schick et al.',
        source_domain='arxiv.org',
        description='LLMs learn to use external tools via self-supervised training.',
        summaries=[{'topic': 'Tool use', 'key_points': ['Self-supervised', 'API calls']}],
    )

    predictor = dspy.Predict(VaultSummaryUpdateSignature)
    result = await run_dspy_operation(
        lm=lm,
        predictor=predictor,
        input_kwargs={
            'current_narrative': 'This vault covers AI agent research.',
            'current_themes': existing_themes,
            'new_notes': [new_note],
            'vault_stats': VaultStats(total_notes=2, new_since_last=1),
        },
        operation_name='test_vault_summary_update',
    )

    assert result.updated_narrative, 'Updated narrative must not be empty'
    _validate_themes(result.updated_themes)


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_extract_signature_produces_typed_themes():
    """Real LLM generates valid Theme objects via VaultTopicExtractSignature."""
    _skip_without_api_key()
    lm = _make_lm()

    predictor = dspy.Predict(VaultTopicExtractSignature)
    result = await run_dspy_operation(
        lm=lm,
        predictor=predictor,
        input_kwargs={
            'notes': SAMPLE_NOTES,
            'batch_index': 0,
            'total_batches': 1,
        },
        operation_name='test_vault_summary_theme_extract',
    )

    _validate_themes(result.themes)
    assert result.batch_summary, 'Batch summary must not be empty'


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_llm_merge_signature_produces_typed_themes():
    """Real LLM merges batch results into valid Theme objects via VaultTopicMergeSignature."""
    _skip_without_api_key()
    lm = _make_lm()

    batch_results = [
        BatchResult(
            batch_index=0,
            themes=[
                LLMTheme(
                    name='AI Agents',
                    description='Autonomous agents',
                    note_count=2,
                    trend='growing',
                ),
                LLMTheme(
                    name='RAG',
                    description='Retrieval-augmented generation',
                    note_count=1,
                    trend='stable',
                ),
            ],
            batch_summary='Papers on AI agents and RAG.',
        ),
        BatchResult(
            batch_index=1,
            themes=[
                LLMTheme(
                    name='Knowledge Management',
                    description='PKM systems',
                    note_count=1,
                    trend='stable',
                ),
                LLMTheme(
                    name='AI Agents',
                    description='LLM-based agents',
                    note_count=1,
                    trend='growing',
                ),
            ],
            batch_summary='Notes on knowledge management and more agent research.',
        ),
    ]

    predictor = dspy.Predict(VaultTopicMergeSignature)
    result = await run_dspy_operation(
        lm=lm,
        predictor=predictor,
        input_kwargs={
            'batch_results': batch_results,
            'vault_note_count': 4,
        },
        operation_name='test_vault_summary_theme_merge',
    )

    assert result.narrative, 'Merged narrative must not be empty'
    _validate_themes(result.themes)
    # The merge should deduplicate "AI Agents" from both batches
    theme_names = [t.name if isinstance(t, LLMTheme) else t['name'] for t in result.themes]
    assert len(theme_names) == len(set(theme_names)), f'Duplicate themes found: {theme_names}'
