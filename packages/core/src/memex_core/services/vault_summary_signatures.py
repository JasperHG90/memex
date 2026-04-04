"""DSPy signatures for vault summary generation and maintenance."""

import dspy


class VaultSummaryUpdateSignature(dspy.Signature):
    """Update a vault summary based on newly added notes.

    Given the current vault summary and rich metadata for notes added since
    the last update, produce an updated summary. The summary should be a
    high-level overview of the vault's themes, content types, key topics,
    and scope. Mention specific references only when they define a theme.
    Do NOT list individual notes. Maximum 750 tokens.

    The new_notes_json contains per-note: title, publish_date, tags,
    template (content type), author, source_domain, description, and
    summaries (list of {topic, key_points} from chunk-level extraction).
    """

    current_summary: str = dspy.InputField(
        desc='Current vault summary text (max 750 tokens). Empty string if first generation.'
    )
    current_topics_json: str = dspy.InputField(
        desc='JSON array of current topics: [{name, note_count, description}].'
    )
    new_notes_json: str = dspy.InputField(
        desc='JSON array of newly added notes with rich metadata: '
        '[{title, publish_date, tags, template, author, source_domain, '
        'description, summaries: [{topic, key_points}]}].'
    )
    vault_stats_json: str = dspy.InputField(
        desc='JSON: {total_notes, new_since_last, max_summary_tokens}.'
    )

    updated_summary: str = dspy.OutputField(
        desc='Updated vault summary. High-level thematic overview, max 750 tokens. '
        'Describe themes and scope, not individual notes.'
    )
    updated_topics_json: str = dspy.OutputField(
        desc='Updated JSON array of topics: [{name, note_count, description}]. 5-15 topics.'
    )


class VaultSummaryFullSignature(dspy.Signature):
    """Generate a complete vault summary from note metadata.

    Given rich metadata for all notes in a vault (title, summaries with
    topic + key_points, tags, template, author, source_domain, publish_date),
    produce a comprehensive summary and extract topics. The summary should
    capture the overall themes, key subjects, content types, and scope.

    Keep the summary under 750 tokens. Extract 5-15 topics, each with a
    descriptive name and a brief description of what the topic covers.
    """

    notes_json: str = dspy.InputField(
        desc='JSON array of note metadata: '
        '[{title, publish_date, tags, template, author, source_domain, '
        'description, summaries: [{topic, key_points}]}].'
    )
    vault_note_count: int = dspy.InputField(desc='Total number of notes in the vault.')
    max_summary_tokens: int = dspy.InputField(desc='Maximum token count for the summary output.')

    summary: str = dspy.OutputField(
        desc='Comprehensive vault summary, max 750 tokens. '
        'High-level thematic overview of themes, content types, and scope.'
    )
    topics_json: str = dspy.OutputField(
        desc=(
            'JSON array of extracted topics: '
            '[{name, note_count, description}]. '
            'Between 5-15 topics.'
        )
    )


class VaultTopicExtractSignature(dspy.Signature):
    """Extract topics from a batch of note metadata.

    Given a batch of note metadata (with rich summaries: topic + key_points),
    identify the key topics covered. Each topic should have a descriptive
    name, the count of notes that relate to it, and a brief description.

    This is used as the first pass in hierarchical summarization for large vaults.
    """

    notes_json: str = dspy.InputField(
        desc='JSON array of note metadata in this batch: '
        '[{title, publish_date, tags, template, author, source_domain, '
        'description, summaries: [{topic, key_points}]}].'
    )
    batch_index: int = dspy.InputField(desc='The index of this batch (0-based).')
    total_batches: int = dspy.InputField(desc='Total number of batches being processed.')

    topics_json: str = dspy.OutputField(
        desc='JSON array of extracted topics: [{name, note_count, description}].'
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
    Keep between 5-15 final topics. Summary must be under 750 tokens.
    """

    batch_topics_json: str = dspy.InputField(
        desc=(
            'JSON array of batch results: '
            '[{batch_index, topics: [{name, note_count, description}], batch_summary}].'
        )
    )
    vault_note_count: int = dspy.InputField(desc='Total number of notes in the vault.')

    summary: str = dspy.OutputField(
        desc='Comprehensive vault summary synthesized from all batches, max 750 tokens. '
        'High-level thematic overview.'
    )
    topics_json: str = dspy.OutputField(
        desc=(
            'JSON array of merged topics: '
            '[{name, note_count, description}]. '
            'Between 5-15 topics, duplicates merged.'
        )
    )
