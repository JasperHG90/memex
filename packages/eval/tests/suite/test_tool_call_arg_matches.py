"""Tests for ToolCallArgMatches — value coercion, regex match, expect_absent."""

from __future__ import annotations

from memex_eval.suite import AgentAnswer, KeywordsPresent, Scenario, ToolCallArgMatches


def _scenario() -> Scenario:
    return Scenario(
        id='s1',
        description='d',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        top_k=5,
    )


def _outcome(**overrides):
    base = dict(
        type='tool_call_arg_matches',
        tool='memex_kv_put',
        arg_name='key',
        regex=r'^user:',
        min_count=1,
        expect_absent=False,
    )
    base.update(overrides)
    return ToolCallArgMatches(**base)


class TestNestedEnvelopeArg:
    """MCP tools that wrap params in a request object (e.g.
    memex_procedural_search → {'request': {'query': …}}) must still be
    matched on the inner arg — a flat lookup would false-fail."""

    def test_resolves_arg_inside_request_envelope(self) -> None:
        outcome = _outcome(
            tool='memex_procedural_search', arg_name='query', regex=r'deploy|payment'
        )
        ans = AgentAnswer(
            tool_calls=[
                {
                    'tool': 'memex_procedural_search',
                    'input': {'request': {'query': 'deploy payments service', 'limit': 10}},
                }
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_dotted_path_resolves_explicit_nesting(self) -> None:
        outcome = _outcome(
            tool='memex_procedural_search', arg_name='request.query', regex=r'deploy'
        )
        ans = AgentAnswer(
            tool_calls=[
                {'tool': 'memex_procedural_search', 'input': {'request': {'query': 'deploy x'}}}
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_missing_nested_arg_does_not_match(self) -> None:
        outcome = _outcome(tool='memex_procedural_search', arg_name='query', regex=r'.*')
        ans = AgentAnswer(
            tool_calls=[{'tool': 'memex_procedural_search', 'input': {'request': {'limit': 10}}}]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestStringValue:
    def test_positive_match(self) -> None:
        outcome = _outcome()
        ans = AgentAnswer(
            tool_calls=[{'tool': 'memex_kv_put', 'input': {'key': 'user:indentation'}}]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_negative_match(self) -> None:
        outcome = _outcome()
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_kv_put', 'input': {'key': 'session:abc'}}])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_re_search_not_fullmatch(self) -> None:
        outcome = _outcome(tool='memex_append_note', arg_name='delta', regex=r'BigQuery')
        ans = AgentAnswer(
            tool_calls=[
                {
                    'tool': 'memex_append_note',
                    'input': {'delta': 'we decided BigQuery for analytics'},
                }
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}


class TestBooleanValue:
    def test_true_matches_true_regex(self) -> None:
        outcome = _outcome(tool='memex_record_outcome', arg_name='success', regex=r'^True$')
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_record_outcome', 'input': {'success': True}}])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_false_does_not_match_true_regex(self) -> None:
        outcome = _outcome(tool='memex_record_outcome', arg_name='success', regex=r'^True$')
        ans = AgentAnswer(
            tool_calls=[{'tool': 'memex_record_outcome', 'input': {'success': False}}]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestIntValue:
    def test_int_coerces(self) -> None:
        outcome = _outcome(tool='memex_memory_search', arg_name='top_k', regex=r'^42$')
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_memory_search', 'input': {'top_k': 42}}])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}


class TestListValue:
    def test_list_serializes(self) -> None:
        outcome = _outcome(tool='memex_get_entities', arg_name='entity_ids', regex=r'"abc"')
        ans = AgentAnswer(
            tool_calls=[{'tool': 'memex_get_entities', 'input': {'entity_ids': ['abc', 'def']}}]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}


class TestMissingAndNone:
    def test_none_never_matches(self) -> None:
        outcome = _outcome(regex=r'.*')
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_kv_put', 'input': {'key': None}}])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_missing_arg_never_matches(self) -> None:
        outcome = _outcome(regex=r'.*')
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_kv_put', 'input': {'value': 'x'}}])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestExpectAbsent:
    def test_no_call_passes(self) -> None:
        outcome = _outcome(expect_absent=True)
        ans = AgentAnswer(tool_calls=[])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_call_with_other_tool_passes(self) -> None:
        outcome = _outcome(expect_absent=True)
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_note_search', 'input': {'query': 'x'}}])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_matching_call_present_fails(self) -> None:
        outcome = _outcome(expect_absent=True)
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_kv_put', 'input': {'key': 'user:x'}}])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}


class TestMinCount:
    def test_below_threshold(self) -> None:
        outcome = _outcome(min_count=2)
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_kv_put', 'input': {'key': 'user:a'}}])
        assert outcome.score(ans, _scenario()) == {'pass': 0.0}

    def test_at_threshold(self) -> None:
        outcome = _outcome(min_count=2)
        ans = AgentAnswer(
            tool_calls=[
                {'tool': 'memex_kv_put', 'input': {'key': 'user:a'}},
                {'tool': 'memex_kv_put', 'input': {'key': 'user:b'}},
            ]
        )
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}


class TestSubstitution:
    def test_format_substitution_via_context(self) -> None:
        handbook_id = '550e8400-e29b-41d4-a716-446655440000'
        outcome = _outcome(
            tool='memex_read_note',
            arg_name='note_id',
            regex=r'{engineering_handbook_id}',
            expect_absent=True,
        )
        ans = AgentAnswer(
            tool_calls=[{'tool': 'memex_read_note', 'input': {'note_id': handbook_id}}]
        )
        result = outcome.score(
            ans,
            _scenario(),
            context={'_note_id_by_key': {'engineering_handbook': handbook_id}},
        )
        assert result == {'pass': 0.0}

    def test_format_substitution_passes_when_other_note_read(self) -> None:
        handbook_id = '550e8400-e29b-41d4-a716-446655440000'
        other_id = '11111111-2222-3333-4444-555555555555'
        outcome = _outcome(
            tool='memex_read_note',
            arg_name='note_id',
            regex=r'{engineering_handbook_id}',
            expect_absent=True,
        )
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_read_note', 'input': {'note_id': other_id}}])
        result = outcome.score(
            ans,
            _scenario(),
            context={'_note_id_by_key': {'engineering_handbook': handbook_id}},
        )
        assert result == {'pass': 1.0}

    def test_static_regex_unchanged_without_substitutions(self) -> None:
        outcome = _outcome(regex=r'^user:')
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_kv_put', 'input': {'key': 'user:x'}}])
        assert outcome.score(ans, _scenario()) == {'pass': 1.0}

    def test_no_context_falls_back_gracefully(self) -> None:
        outcome = _outcome(regex=r'^user:')
        ans = AgentAnswer(tool_calls=[{'tool': 'memex_kv_put', 'input': {'key': 'user:x'}}])
        assert outcome.score(ans, _scenario(), context=None) == {'pass': 1.0}

    def test_substitution_uses_format_key_dict_shape_from_runner(self) -> None:
        """Regression: the runner's note_id_by_key is keyed by note_key
        (filename stem, e.g. ``engineering_handbook``), and
        ``ToolCallArgMatches`` builds format keys as ``f'{note_key}_id'``.
        Scenarios MUST use placeholders matching that shape (underscore
        suffix); a hyphenated note_key like ``engineering-handbook`` would
        produce ``engineering-handbook_id``, which Python's
        ``str.format()`` can't resolve via ``{engineering_handbook_id}``.
        This test pins the contract.
        """
        handbook_id = '550e8400-e29b-41d4-a716-446655440000'
        outcome = _outcome(
            tool='memex_read_note',
            arg_name='note_id',
            regex=r'{engineering_handbook_id}',
            expect_absent=True,
        )
        ans = AgentAnswer(
            tool_calls=[{'tool': 'memex_read_note', 'input': {'note_id': handbook_id}}]
        )
        result = outcome.score(
            ans,
            _scenario(),
            context={'_note_id_by_key': {'engineering_handbook': handbook_id}},
        )
        assert result == {'pass': 0.0}, (
            'substitution failed — placeholder did not resolve so '
            'expect_absent=True passed trivially'
        )
