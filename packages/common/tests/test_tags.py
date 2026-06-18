"""Unit tests for the protected-tag registry."""

import pytest

from memex_common.tags import (
    PROVENANCE_TAG_NAMESPACES,
    RESERVED_SEMANTIC_TAGS,
    is_protected_tag,
)


class TestIsProtectedTag:
    @pytest.mark.parametrize('tag', sorted(RESERVED_SEMANTIC_TAGS))
    def test_reserved_semantic_tags_are_protected(self, tag: str):
        assert is_protected_tag(tag)

    @pytest.mark.parametrize(
        'tag',
        [
            'surface:claude-code',
            'project:github.com/JasperHG90/memex',
            'session:2026-06-18T09:07:19.034',
            'session-end:exit',
            'git:branch=main',
            'git:sha=abc123',
            'git:dirty',
            'claude:model=claude-opus-4-8',
            'app:claude-code:theme',
            'cc:plugin=2.3.1',
            'cc:9b5090d2-c5f9-4f39-9e9e-1ed76dd99c8e',
        ],
    )
    def test_provenance_namespaced_tags_are_protected(self, tag: str):
        assert is_protected_tag(tag)

    @pytest.mark.parametrize(
        'tag',
        ['python', 'testing', 'cli', 'memex', 'workflow', 'automation', 'development'],
    )
    def test_topical_tags_are_not_protected(self, tag: str):
        assert not is_protected_tag(tag)

    @pytest.mark.parametrize(
        'tag',
        ['project', 'session', 'git', 'app', 'surface'],
    )
    def test_bare_namespace_words_are_not_protected(self, tag: str):
        """A namespace word without a ``:`` segment is a topical tag, not provenance."""
        assert not is_protected_tag(tag)

    def test_case_and_whitespace_insensitive(self):
        assert is_protected_tag('  Handoff ')
        assert is_protected_tag('PROJECT:foo')

    @pytest.mark.parametrize('tag', ['', '   ', '\t'])
    def test_empty_tags_are_not_protected(self, tag: str):
        assert not is_protected_tag(tag)

    def test_unknown_namespace_not_protected(self):
        """A colon tag whose prefix is not a known provenance namespace is topical."""
        assert not is_protected_tag('topic:databases')

    def test_empty_namespace_not_protected(self):
        """A leading-colon tag has an empty namespace segment — not provenance."""
        assert not is_protected_tag(':foo')

    def test_multi_colon_topical_not_protected(self):
        """Multi-colon tag with a non-provenance prefix is topical, not stripped."""
        assert not is_protected_tag('topic:sub:value')

    def test_namespaces_are_lowercase(self):
        """Registry invariant: matching normalizes to lower-case."""
        assert all(ns == ns.lower() for ns in PROVENANCE_TAG_NAMESPACES)
        assert all(t == t.lower() for t in RESERVED_SEMANTIC_TAGS)
