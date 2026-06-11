"""Unit tests for the procedural distillation rendering (§9 / §18.8)."""

from __future__ import annotations

from memex_core.memory.procedural_distillation import (
    DISTILLATION_DISCIPLINE,
    DistilledStep,
    _render_steps_markdown,
    render_cases_markdown,
)


def test_render_steps_includes_conditions_cites_and_skill_hint():
    steps = [
        DistilledStep(order=1, action='Roll the canary at 10%', source_cases=['c1', 'c2']),
        DistilledStep(
            order=2,
            action='Roll back',
            condition='the canary errors',
            source_cases=['c3'],
            skill_hint='a skill that can bump, tag, and push a release',
        ),
    ]
    body = _render_steps_markdown(steps, notes='Excluded the team-notify step (no case did it).')

    assert '## Steps' in body
    assert '1. Roll the canary at 10%' in body
    assert '10%' in body  # quantitative anchor preserved verbatim
    assert '— when the canary errors' in body  # condition inline
    assert '(cases: c1, c2)' in body
    assert '[skill: a skill that can bump, tag, and push a release]' in body  # §18.8
    assert '## Notes' in body


def test_skill_hint_defaults_none_and_is_omitted():
    body = _render_steps_markdown([DistilledStep(order=1, action='Do the thing')], notes='')
    assert '[skill:' not in body  # no hint → no skill annotation


def test_discipline_carries_all_seven_rules():
    # The 6 §9/§19.5 rules + the §18.8 skill-hint rule.
    for n in range(1, 8):
        assert f'{n}.' in DISTILLATION_DISCIPLINE
    assert 'QUANTITATIVE ANCHORS VERBATIM' in DISTILLATION_DISCIPLINE
    assert 'CAPABILITY DESCRIPTIONS' in DISTILLATION_DISCIPLINE


def test_render_cases_markdown_labels_each_case():
    md = render_cases_markdown([('case-a', 'first episode'), ('case-b', 'second episode')])
    assert '### case: case-a' in md
    assert '### case: case-b' in md
    assert 'first episode' in md
