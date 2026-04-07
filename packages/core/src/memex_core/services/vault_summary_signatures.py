"""DSPy signatures for vault summary generation and maintenance."""

from __future__ import annotations

import dspy
from pydantic import BaseModel, Field

NOTE_INDEX_DESCRIPTION = (
    'Zero-based integer indices into the notes list identifying which notes belong to this theme. '
    'Example: [0, 3, 7] means the first, fourth, and eighth notes. '
    'STRICT RULE: Only use indices that exist in the provided notes list.'
)


# ─── Pydantic models ───


class LLMTheme(BaseModel):
    """A thematic area as returned by the LLM. Contains note_indices for grounding."""

    name: str = Field(description='Descriptive theme name.')
    description: str = Field(description='Brief description of what the theme covers.')
    note_indices: list[int] = Field(
        default_factory=list,
        description=NOTE_INDEX_DESCRIPTION,
    )


class ResolvedTheme(BaseModel):
    """A theme after post-processing: indices resolved to titles, trend computed from dates."""

    name: str
    description: str
    note_count: int
    trend: str  # 'growing', 'stable', 'dormant'
    last_addition: str | None = None
    representative_titles: list[str] = Field(default_factory=list)


class NoteMetadata(BaseModel):
    """Rich metadata for a note, used as LLM input."""

    index: int = Field(description='Zero-based index of this note in the list.')
    title: str
    publish_date: str | None = None
    tags: list[str] = Field(default_factory=list)
    template: str = ''
    author: str = ''
    source_domain: str = ''
    description: str = ''
    summaries: list[dict] = Field(
        default_factory=list,
        description='Chunk-level summaries: [{topic, key_points}].',
    )


class VaultStats(BaseModel):
    """Stats passed to the LLM for context."""

    total_notes: int
    new_since_last: int = 0
    max_narrative_tokens: int = 200


class BatchResult(BaseModel):
    """Result from a batch theme extraction pass."""

    batch_index: int
    themes: list[LLMTheme] = Field(default_factory=list)
    batch_summary: str = ''


# ─── DSPy signatures ───


class VaultSummaryUpdateSignature(dspy.Signature):
    """Update a vault's themes and narrative based on newly added notes.

    Given the current themes and narrative plus rich metadata for new notes,
    produce updated themes and an updated narrative. The narrative is a short
    thematic synthesis (2-4 sentences, max 200 tokens) capturing what the vault
    is about and what cross-cutting patterns connect the themes.

    Each note has an index field. Use note_indices to reference which notes
    belong to each theme. Only use valid indices from the new_notes list.
    """

    current_narrative: str = dspy.InputField(
        desc='Current narrative text (max 200 tokens). Empty string if first generation.'
    )
    current_themes: list[LLMTheme] = dspy.InputField(desc='Current themes.')
    new_notes: list[NoteMetadata] = dspy.InputField(desc='Newly added notes with rich metadata.')
    vault_stats: VaultStats = dspy.InputField(desc='Vault statistics for context.')

    updated_narrative: str = dspy.OutputField(
        desc='Updated narrative. Short thematic synthesis (2-4 sentences), max 200 tokens. '
        'Capture what the vault is about and what patterns connect the themes.'
    )
    updated_themes: list[LLMTheme] = dspy.OutputField(
        desc='Updated themes. 5-15 themes. Use note_indices to reference new notes.'
    )


class VaultSummaryFullSignature(dspy.Signature):
    """Generate themes and narrative from note metadata.

    Given rich metadata for all notes in a vault, produce themes and a short
    narrative. The narrative (2-4 sentences, max 200 tokens) should capture the
    overall scope of the vault and the patterns connecting its themes.

    Each note has an index field. Use note_indices to reference which notes
    belong to each theme. Only use valid indices from the notes list.
    """

    notes: list[NoteMetadata] = dspy.InputField(desc='All note metadata in the vault.')
    vault_note_count: int = dspy.InputField(desc='Total number of notes in the vault.')
    max_narrative_tokens: int = dspy.InputField(
        desc='Maximum token count for the narrative output.'
    )

    narrative: str = dspy.OutputField(
        desc='Thematic synthesis of the vault, max 200 tokens. '
        '2-4 sentences: what the vault covers and what patterns connect its themes.'
    )
    themes: list[LLMTheme] = dspy.OutputField(
        desc='Extracted themes. Between 5-15 themes. Use note_indices to reference notes.'
    )


class VaultTopicExtractSignature(dspy.Signature):
    """Extract themes from a batch of note metadata.

    Given a batch of note metadata, identify the key themes covered.
    Each note has an index field. Use note_indices to reference which notes
    belong to each theme. Only use valid indices from the notes list.

    This is the first pass in hierarchical summarization for large vaults.
    """

    notes: list[NoteMetadata] = dspy.InputField(desc='Note metadata in this batch.')
    batch_index: int = dspy.InputField(desc='The index of this batch (0-based).')
    total_batches: int = dspy.InputField(desc='Total number of batches being processed.')

    themes: list[LLMTheme] = dspy.OutputField(
        desc='Extracted themes from this batch. Use note_indices to reference notes.'
    )
    batch_summary: str = dspy.OutputField(
        desc='A brief summary of this batch of notes (2-4 sentences).'
    )


class VaultTopicMergeSignature(dspy.Signature):
    """Merge theme lists from multiple batches into a consolidated list.

    Given theme lists extracted from separate batches and their batch summaries,
    merge overlapping themes and produce a unified theme list and a short narrative.

    Deduplicate themes that refer to the same concept under different names.
    Keep between 5-15 final themes. Narrative must be under 200 tokens.
    Note: note_indices from different batches cannot be combined — leave them empty
    in the merged output.
    """

    batch_results: list[BatchResult] = dspy.InputField(
        desc='Results from each batch: themes and batch summaries.'
    )
    vault_note_count: int = dspy.InputField(desc='Total number of notes in the vault.')

    narrative: str = dspy.OutputField(
        desc='Thematic synthesis from all batches, max 200 tokens. '
        '2-4 sentences: scope and cross-cutting patterns.'
    )
    themes: list[LLMTheme] = dspy.OutputField(
        desc='Merged themes. Between 5-15 themes, duplicates merged. '
        'note_indices should be empty (cross-batch indices are not comparable).'
    )
