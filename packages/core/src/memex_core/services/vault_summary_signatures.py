"""DSPy signatures for vault summary generation and maintenance."""

import dspy


class VaultSummaryPatchSignature(dspy.Signature):
    """Patch an existing vault summary to incorporate a newly ingested note.

    Given the current vault summary, topics JSON, aggregate stats, and a new note's
    title and description, produce an updated summary and topics list. The patch should
    be minimal: adjust the existing summary to reflect the new information without
    rewriting it from scratch. If the note introduces a new topic, add it. If it
    reinforces an existing topic, update the note count and description.

    Keep the summary between 1-3 paragraphs. Topics should be specific and descriptive,
    not generic categories.
    """

    current_summary: str = dspy.InputField(desc='The current vault summary text (1-3 paragraphs).')
    current_topics_json: str = dspy.InputField(
        desc='JSON array of current topics: [{name, note_count, description}].'
    )
    current_stats_json: str = dspy.InputField(
        desc='JSON object of current stats: {total_notes, total_entities, ...}.'
    )
    note_title: str = dspy.InputField(desc='Title of the newly ingested note.')
    note_description: str = dspy.InputField(desc='Description of the newly ingested note.')

    updated_summary: str = dspy.OutputField(
        desc='Updated vault summary incorporating the new note (1-3 paragraphs).'
    )
    updated_topics_json: str = dspy.OutputField(
        desc='Updated JSON array of topics with adjusted counts and any new topics.'
    )


class VaultSummaryFullSignature(dspy.Signature):
    """Generate a complete vault summary from note titles and descriptions.

    Given a list of (title, description) pairs for all notes in a vault,
    produce a comprehensive summary and extract topics. The summary should
    capture the overall themes, key subjects, and scope of the vault's contents.

    Keep the summary between 1-3 paragraphs. Extract 5-15 topics, each with a
    descriptive name and a brief description of what the topic covers.
    """

    notes_json: str = dspy.InputField(desc='JSON array of note objects: [{title, description}].')
    vault_note_count: int = dspy.InputField(desc='Total number of notes in the vault.')

    summary: str = dspy.OutputField(desc='Comprehensive vault summary (1-3 paragraphs).')
    topics_json: str = dspy.OutputField(
        desc=(
            'JSON array of extracted topics: '
            '[{name, note_count, description}]. '
            'Between 5-15 topics.'
        )
    )


class VaultTopicExtractSignature(dspy.Signature):
    """Extract topics from a batch of note titles and descriptions.

    Given a batch of (title, description) pairs, identify the key topics
    covered. Each topic should have a descriptive name, the count of notes
    that relate to it, and a brief description.

    This is used as the first pass in hierarchical summarization for large vaults.
    """

    notes_json: str = dspy.InputField(
        desc='JSON array of note objects in this batch: [{title, description}].'
    )
    batch_index: int = dspy.InputField(desc='The index of this batch (0-based).')
    total_batches: int = dspy.InputField(desc='Total number of batches being processed.')

    topics_json: str = dspy.OutputField(
        desc=('JSON array of extracted topics: [{name, note_count, description}].')
    )
    batch_summary: str = dspy.OutputField(
        desc='A brief summary of this batch of notes (2-4 sentences).'
    )


class VaultTopicMergeSignature(dspy.Signature):
    """Merge topic lists from multiple batches into a consolidated list.

    Given topic lists extracted from separate batches and their batch summaries,
    merge overlapping topics (combine counts, merge descriptions) and produce
    a unified topic list and a comprehensive vault summary.

    Deduplicate topics that refer to the same concept under different names.
    Keep between 5-15 final topics.
    """

    batch_topics_json: str = dspy.InputField(
        desc=(
            'JSON array of batch results: '
            '[{batch_index, topics: [{name, note_count, description}], batch_summary}].'
        )
    )
    vault_note_count: int = dspy.InputField(desc='Total number of notes in the vault.')

    summary: str = dspy.OutputField(
        desc='Comprehensive vault summary synthesized from all batches (1-3 paragraphs).'
    )
    topics_json: str = dspy.OutputField(
        desc=(
            'JSON array of merged topics: '
            '[{name, note_count, description}]. '
            'Between 5-15 topics, duplicates merged.'
        )
    )
