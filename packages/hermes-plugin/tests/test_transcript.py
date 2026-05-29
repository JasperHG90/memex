"""Tests for the transcript preprocessing pipeline."""

from __future__ import annotations

from typing import Any

from memex_hermes_plugin.memex.transcript import (
    format_transcript,
    is_system_metadata,
    is_system_prompt,
    passes_quality_gate,
    preprocess_turns,
    sanitize_html_content,
)

# ---------------------------------------------------------------------------
# Fixtures — reusable prompt fragments
# ---------------------------------------------------------------------------

SKILL_REVIEW_PROMPT = (
    'Review the conversation above and update the skill library. '
    'Be ACTIVE — most sessions produce at least one skill update, even if small. '
    'A pass that does nothing is a missed learning opportunity, not a neutral outcome.\n\n'
    'Target shape of the library: CLASS-LEVEL skills, each with a rich SKILL.md '
    'and a `references/` directory for session-specific detail. Not a long flat list '
    'of narrow one-session-one-skill entries. This shapes HOW you update, not WHETHER '
    'you update.\n\n'
    'Signals to look for (any one of these warrants action):\n'
    '  • User corrected your style, tone, format, legibility, or verbosity.\n'
    '  • User corrected your workflow, approach, or sequence of steps.\n'
    '  • Non-trivial technique, fix, workaround, debugging path emerged.\n'
    '  • A skill that got loaded or consulted this session turned out to be wrong.\n\n'
    "'Nothing to save.' is a real option but should NOT be the default. "
    'If the session ran smoothly with no corrections and produced no new technique, '
    "just say 'Nothing to save.' and stop. Otherwise, act.\n\n"
    'You can only call memory and skill management tools. Other tools will be denied '
    'at runtime — do not attempt them.'
)

MEMORY_REVIEW_PROMPT = (
    'Review the conversation above and consider saving to memory if appropriate.\n\n'
    'Focus on:\n'
    '1. Has the user revealed things about themselves?\n'
    '2. Has the user expressed expectations about how you should behave?\n\n'
    "If nothing is worth saving, just say 'Nothing to save.' and stop.\n\n"
    'You can only call memory and skill management tools. Other tools will be denied '
    'at runtime — do not attempt them.'
)


# ===================================================================
# A. is_system_prompt
# ===================================================================


class TestIsSystemPrompt:
    def test_detects_skill_review_prompt(self) -> None:
        assert is_system_prompt(SKILL_REVIEW_PROMPT) is True

    def test_detects_memory_review_prompt(self) -> None:
        assert is_system_prompt(MEMORY_REVIEW_PROMPT) is True

    def test_detects_reworded_prompt(self) -> None:
        """Minor rewording should still be detected (2+ anchors survive)."""
        reworded = (
            'Look at the conversation and refresh the skill library. '
            'Be proactive — most conversations yield at least one improvement.\n\n'
            'Signals to watch for:\n'
            '  • Style corrections\n'
            '  • Workflow corrections\n\n'
            "'Nothing to save.' is fine if nothing changed.\n\n"
            'You can only call memory and skill management tools. Other tools '
            'will be denied at runtime — do not attempt them.'
        )
        assert is_system_prompt(reworded) is True

    def test_detects_prompt_with_extra_paragraphs(self) -> None:
        extended = SKILL_REVIEW_PROMPT + '\n\nAdditional paragraph about new guidelines.\n' * 5
        assert is_system_prompt(extended) is True

    def test_does_not_flag_organic_conversation(self) -> None:
        organic = (
            'Hey, I was thinking about the architecture of the retrieval pipeline. '
            'Can you explain how the TEMPR strategies work together? I want to '
            'understand the ranking fusion approach and how MMR diversity is applied. '
            'Also, what happens when the circuit breaker trips during an LLM call? '
            'Does the system fall back to keyword-only search?'
        )
        assert is_system_prompt(organic) is False

    def test_does_not_flag_long_organic_text(self) -> None:
        """Long organic text should not trigger even if it exceeds the length threshold."""
        long_text = 'This is a detailed technical discussion. ' * 50
        assert len(long_text) > 500
        assert is_system_prompt(long_text) is False

    def test_does_not_flag_short_text_with_one_anchor(self) -> None:
        short = 'Nothing to save.'
        assert is_system_prompt(short) is False

    def test_empty_string(self) -> None:
        assert is_system_prompt('') is False

    def test_single_anchor_in_long_text(self) -> None:
        """One anchor in a long text is not enough — needs >= 2."""
        text = 'a ' * 150 + 'update the skill library' + ' a' * 50
        assert len(text) > 200
        assert is_system_prompt(text) is False

    def test_two_anchors_in_long_text(self) -> None:
        text = (
            'a ' * 150
            + 'update the skill library. '
            + 'You can only call memory and skill management tools.'
            + ' a' * 50
        )
        assert len(text) > 200
        assert is_system_prompt(text) is True

    def test_anchor_removal_still_detects(self) -> None:
        """Removing one anchor from the real prompt should still detect (others remain)."""
        modified = SKILL_REVIEW_PROMPT.replace("'Nothing to save.'", "'No updates needed.'")
        assert is_system_prompt(modified) is True


# ===================================================================
# B. is_system_metadata
# ===================================================================


class TestIsSystemMetadata:
    def test_detects_model_switch_note(self) -> None:
        text = (
            '[Note: model was just switched from gemma4:31b-cloud to glm-5.1 '
            'via Ollama Cloud. Adjust your self-identification accordingly.]'
        )
        assert is_system_metadata(text) is True

    def test_detects_system_prefix(self) -> None:
        text = '[System: session resumed after network interruption]'
        assert is_system_metadata(text) is True

    def test_does_not_flag_footnotes(self) -> None:
        assert is_system_metadata('[1]') is False
        assert is_system_metadata('[citation needed]') is False

    def test_does_not_flag_normal_brackets(self) -> None:
        assert is_system_metadata('Use [this approach] for the fix') is False

    def test_does_not_flag_empty(self) -> None:
        assert is_system_metadata('') is False

    def test_handles_leading_trailing_whitespace(self) -> None:
        text = '  [Note: something happened]  '
        assert is_system_metadata(text) is True

    def test_does_not_flag_multiline_content(self) -> None:
        """Multi-line user content starting with [Note: must not false-positive."""
        text = (
            '[Note: here is my analysis\n'
            'of the deployment pipeline.\n'
            'It covers many topics and ends with a summary]'
        )
        assert is_system_metadata(text) is False


# ===================================================================
# C. sanitize_html_content
# ===================================================================


class TestSanitizeHtmlContent:
    def test_replaces_full_html_page(self) -> None:
        html = (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
            '<meta charset="UTF-8">\n<title>Test</title>\n'
            '<style>body { color: white; background: #020617; }</style>\n</head>\n'
            '<body>\n<div class="container">\n<h1>Title</h1>\n'
            + '<p class="text">Content paragraph.</p>\n<div class="card"><span>Item</span></div>\n'
            * 10
            + '</div>\n</body>\n</html>'
        )
        assert sanitize_html_content(html) == '[HTML content removed]'

    def test_replaces_llm_generated_html_without_doctype(self) -> None:
        """LLM-generated HTML without <!DOCTYPE> but with dense tag patterns."""
        html = (
            '<div class="card" style="background: #020617;">\n'
            '<span class="title">Architecture</span>\n'
            '<div class="body"><p>Some content</p></div>\n'
            '<style>.card { border: 1px solid; }</style>\n'
            '</div>\n' * 5
        )
        assert len(html) > 500
        assert sanitize_html_content(html) == '[HTML content removed]'

    def test_does_not_flag_inline_formatting(self) -> None:
        text = 'Use <b>bold</b> and <i>italic</i> for emphasis.'
        assert sanitize_html_content(text) == text

    def test_threshold_respected(self) -> None:
        html = '<div>x</div>' * 10
        assert sanitize_html_content(html, threshold=5000) == html
        assert sanitize_html_content(html, threshold=10) == '[HTML content removed]'

    def test_mixed_markdown_and_small_html(self) -> None:
        text = '## Heading\n\nSome text with <code>inline</code> code.\n\n- Item 1\n- Item 2'
        assert sanitize_html_content(text) == text

    def test_placeholder_text(self) -> None:
        html = '<html><body>' + '<div>x</div>' * 100 + '</body></html>'
        result = sanitize_html_content(html)
        assert result == '[HTML content removed]'

    def test_empty_string(self) -> None:
        assert sanitize_html_content('') == ''


# ===================================================================
# D. preprocess_turns
# ===================================================================


class TestPreprocessTurns:
    def test_strips_system_prompt_keeps_assistant(self) -> None:
        turns = [{'user': SKILL_REVIEW_PROMPT, 'assistant': 'Updated sorting-hat skill.'}]
        result = preprocess_turns(turns)
        assert result[0]['user'] == '[system prompt omitted]'
        assert result[0]['assistant'] == 'Updated sorting-hat skill.'

    def test_strips_system_metadata(self) -> None:
        turns = [
            {
                'user': '[Note: model was just switched from X to Y]',
                'assistant': 'Hello!',
            }
        ]
        result = preprocess_turns(turns)
        assert result[0]['user'] == ''
        assert result[0]['assistant'] == 'Hello!'

    def test_sanitizes_html_in_assistant(self) -> None:
        html = (
            '<html><head><style>body{}</style></head><body>'
            + '<div>x</div>' * 100
            + '</body></html>'
        )
        turns = [{'user': 'Make a diagram', 'assistant': html}]
        result = preprocess_turns(turns)
        assert result[0]['user'] == 'Make a diagram'
        assert result[0]['assistant'] == '[HTML content removed]'

    def test_normal_turns_unchanged(self) -> None:
        turns = [
            {'user': 'What is the weather?', 'assistant': 'It is sunny.'},
            {'user': 'Thanks!', 'assistant': 'You are welcome.'},
        ]
        result = preprocess_turns(turns)
        assert result == turns

    def test_does_not_mutate_input(self) -> None:
        original = [{'user': SKILL_REVIEW_PROMPT, 'assistant': 'Updated.'}]
        original_copy = [dict(t) for t in original]
        preprocess_turns(original)
        assert original == original_copy

    def test_handles_role_content_format(self) -> None:
        messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there!'},
        ]
        result = preprocess_turns(messages)
        assert result[0]['user'] == 'Hello'
        assert result[0]['assistant'] == 'Hi there!'

    def test_multiple_issues_in_session(self) -> None:
        html = '<html><body>' + '<div class="x" style="y">z</div>' * 50 + '</body></html>'
        turns = [
            {'user': 'Make a diagram', 'assistant': html},
            {'user': SKILL_REVIEW_PROMPT, 'assistant': 'Nothing to save.'},
        ]
        result = preprocess_turns(turns)
        assert result[0]['assistant'] == '[HTML content removed]'
        assert result[1]['user'] == '[system prompt omitted]'
        assert result[1]['assistant'] == 'Nothing to save.'

    def test_strip_system_prompts_toggle_off(self) -> None:
        turns = [{'user': SKILL_REVIEW_PROMPT, 'assistant': 'Updated.'}]
        result = preprocess_turns(turns, strip_system_prompts=False)
        assert result[0]['user'] == SKILL_REVIEW_PROMPT

    def test_strip_html_toggle_off(self) -> None:
        html = '<html><body>' + '<div>x</div>' * 100 + '</body></html>'
        turns = [{'user': 'Make it', 'assistant': html}]
        result = preprocess_turns(turns, strip_html_content=False)
        assert result[0]['assistant'] == html

    def test_strip_system_metadata_toggle_off(self) -> None:
        turns = [
            {
                'user': '[Note: model was just switched from X to Y]',
                'assistant': 'Hello!',
            }
        ]
        result = preprocess_turns(turns, strip_system_prompts=False)
        assert result[0]['user'] == '[Note: model was just switched from X to Y]'

    def test_missing_role_defaults_to_user(self) -> None:
        """Messages with no 'role' key should be treated as user turns."""
        messages: list[dict[str, Any]] = [
            {'content': 'orphaned text'},
            {'role': 'assistant', 'content': 'response'},
        ]
        result = preprocess_turns(messages)
        assert result[0]['user'] == 'orphaned text'
        assert result[0]['assistant'] == 'response'

    def test_pending_user_flushed_before_pair_dict(self) -> None:
        """A pending {role:'user'} must be flushed before a {user,assistant} dict."""
        messages: list[dict[str, Any]] = [
            {'role': 'user', 'content': 'Q1'},
            {'user': 'Q2', 'assistant': 'A2'},
        ]
        result = preprocess_turns(messages)
        assert result[0]['user'] == 'Q1'
        assert result[0]['assistant'] == ''
        assert result[1]['user'] == 'Q2'
        assert result[1]['assistant'] == 'A2'

    def test_empty_turns(self) -> None:
        assert preprocess_turns([]) == []


# ===================================================================
# E. passes_quality_gate
# ===================================================================


class TestPassesQualityGate:
    def test_rejects_nothing_to_save(self) -> None:
        turns = [{'user': '[system prompt omitted]', 'assistant': 'Nothing to save.'}]
        assert passes_quality_gate(turns) is False

    def test_accepts_substantive_response(self) -> None:
        turns = [
            {
                'user': '[system prompt omitted]',
                'assistant': (
                    'Updated sorting-hat skill to add CLI commands for vault listing '
                    'and note enumeration.'
                ),
            }
        ]
        assert passes_quality_gate(turns) is True

    def test_rejects_trivial_greeting(self) -> None:
        turns = [{'user': 'Hello', 'assistant': 'Hi!'}]
        assert passes_quality_gate(turns) is False

    def test_accepts_multi_turn_conversation(self) -> None:
        turns = [
            {
                'user': 'How does vault routing work?',
                'assistant': 'Vault routing uses a hierarchy of resolution...',
            },
            {
                'user': 'Can you show me an example?',
                'assistant': 'Sure, here is how you configure it...',
            },
        ]
        assert passes_quality_gate(turns) is True

    def test_rejects_empty_list(self) -> None:
        assert passes_quality_gate([]) is False

    def test_custom_thresholds(self) -> None:
        turns = [{'user': 'x', 'assistant': 'y'}]
        assert passes_quality_gate(turns, min_turns=1, min_content_chars=1) is True
        assert passes_quality_gate(turns, min_turns=1, min_content_chars=100) is False
        assert passes_quality_gate(turns, min_turns=2, min_content_chars=1) is False

    def test_placeholder_strings_not_counted(self) -> None:
        turns = [{'user': '[system prompt omitted]', 'assistant': '[HTML content removed]'}]
        assert passes_quality_gate(turns) is False

    def test_zero_thresholds_pass_everything(self) -> None:
        turns = [{'user': '', 'assistant': 'x'}]
        assert passes_quality_gate(turns, min_turns=0, min_content_chars=0) is True


# ===================================================================
# F. format_transcript
# ===================================================================


class TestFormatTranscript:
    def test_basic_format(self) -> None:
        turns = [{'user': 'Hello', 'assistant': 'Hi there!'}]
        result = format_transcript(turns)
        assert '### User\n\nHello' in result
        assert '### Assistant\n\nHi there!' in result

    def test_horizontal_rules_between_turns(self) -> None:
        turns = [
            {'user': 'First', 'assistant': 'Response 1'},
            {'user': 'Second', 'assistant': 'Response 2'},
        ]
        result = format_transcript(turns)
        assert '\n\n---\n\n' in result

    def test_no_rule_for_single_turn(self) -> None:
        turns = [{'user': 'Only', 'assistant': 'Turn'}]
        result = format_transcript(turns)
        assert '---' not in result

    def test_handles_role_content_format(self) -> None:
        messages = [
            {'role': 'user', 'content': 'Query'},
            {'role': 'assistant', 'content': 'Answer'},
        ]
        result = format_transcript(messages)
        assert '### User\n\nQuery' in result
        assert '### Assistant\n\nAnswer' in result

    def test_skips_empty_content(self) -> None:
        turns = [{'user': '', 'assistant': 'Only assistant spoke'}]
        result = format_transcript(turns)
        assert '### User' not in result
        assert '### Assistant\n\nOnly assistant spoke' in result

    def test_empty_turns(self) -> None:
        assert format_transcript([]) == ''

    def test_content_list_format(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                'role': 'user',
                'content': [{'type': 'text', 'text': 'From a list'}],
            },
            {'role': 'assistant', 'content': 'Plain text'},
        ]
        result = format_transcript(messages)
        assert 'From a list' in result
