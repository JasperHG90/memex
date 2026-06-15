"""`memex submit --file` — deterministic case-from-markdown parsing."""

from __future__ import annotations

from memex_cli.procedural import parse_case_markdown


def test_parse_full_template_with_frontmatter():
    md = """---
note_key: my-case
role: case
outcome: success
tags: [retrieval, ranking]
---

## Trigger
Search returned zero results.

## Situation
The corpus has 11 near-identical docs.

## Actions
1. repro on 2 docs
2. repro on full corpus

## Outcome / Lesson
Success — root cause was missing IDF.

**Lessons:**
1. demand the composition
2. neither retriever does IDF
"""
    p = parse_case_markdown(md)
    assert p['title'] == 'my-case'
    assert p['outcome'] == 'success'
    assert p['trigger'].startswith('Search returned')
    assert p['situation'].startswith('The corpus')
    assert p['actions'] == ['repro on 2 docs', 'repro on full corpus']
    assert 'demand the composition' in p['lesson']
    assert p['tags'] == ['retrieval', 'ranking']


def test_roundtrip_compose_then_parse():
    """compose_case_markdown (server render) → parse_case_markdown recovers the
    structured fields — the two are inverses."""
    from memex_common.procedural_schemas import CaseSubmit
    from memex_core.services.case_service import compose_case_markdown

    payload = CaseSubmit(
        title='t',
        trigger='when X happens',
        situation='prior state',
        actions=['step one', 'step two'],
        outcome='mixed',
        lesson='do Y next time',
    )
    p = parse_case_markdown(compose_case_markdown(payload))
    assert p['trigger'] == 'when X happens'
    assert p['situation'] == 'prior state'
    assert p['actions'] == ['step one', 'step two']
    assert p['outcome'] == 'mixed'
    assert 'do Y next time' in p['lesson']


def test_outcome_parsed_from_section_when_no_frontmatter():
    md = '## Trigger\nt\n\n## Outcome / Lesson\nfailure. **Lesson:** be careful\n'
    p = parse_case_markdown(md)
    assert p['outcome'] == 'failure'
    assert 'be careful' in p['lesson']


def test_none_recorded_situation_is_empty():
    md = '## Trigger\nt\n\n## Situation\n_not recorded_\n\n## Actions\n_none recorded_\n'
    p = parse_case_markdown(md)
    assert p['situation'] == ''
    assert p['actions'] == []


def test_outcome_token_is_exact_not_prefix():
    """A lesson whose Outcome section opens with a word that merely STARTS
    with an outcome token ("Successfully …") must not be misread as that
    outcome — only an exact leading token (success/failure/mixed) counts."""
    md = (
        '## Trigger\nt\n\n## Outcome / Lesson\n'
        'Successfully avoided the outage. **Lesson:** watch the port\n'
    )
    p = parse_case_markdown(md)
    # 'Successfully' is not the exact token 'success' → no outcome recovered
    # (parse_case_markdown omits fields it cannot recover).
    assert p.get('outcome') is None
    assert 'watch the port' in p['lesson']
