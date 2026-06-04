"""Tests for the client-facing lint-proposal models (memex_common.lint).

These are the shape + core-independent hygiene checks an external tool
relies on locally before submitting. The reserved-internal-name check is
deliberately NOT here — it needs the live rule set and lives server-side.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex_common.lint import (
    LINT_TYPES,
    LintProposal,
    LintRule,
    ProposedAction,
)


def _valid(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        'rule_name': 'skill-misroute',
        'lint_type': 'routing',
        'target_type': 'note',
        'target_id': 'abc-123',
        'description': 'why it fired',
        'suggested_action': 'route it',
    }
    base.update(overrides)
    return base


class TestLintProposalShape:
    def test_minimal_valid(self) -> None:
        p = LintProposal(**_valid())
        assert p.vault_id is None and p.evidence == {} and p.proposed_action is None

    @pytest.mark.parametrize('bad', ['Bad Name', 'UPPER', '9lead', '-lead', ''])
    def test_rule_name_must_be_slug(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            LintProposal(**_valid(rule_name=bad))

    def test_llm_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError, match='llm_'):
            LintProposal(**_valid(rule_name='llm_custom'))

    def test_snake_and_kebab_accepted(self) -> None:
        assert LintProposal(**_valid(rule_name='my_rule')).rule_name == 'my_rule'
        assert LintProposal(**_valid(rule_name='my-rule')).rule_name == 'my-rule'

    @pytest.mark.parametrize('lt', LINT_TYPES)
    def test_every_lint_type_accepted(self, lt: str) -> None:
        assert LintProposal(**_valid(lint_type=lt)).lint_type == lt

    def test_lint_type_must_be_member(self) -> None:
        with pytest.raises(ValidationError, match='lint_type'):
            LintProposal(**_valid(lint_type='vibes'))

    def test_target_type_hygiene(self) -> None:
        with pytest.raises(ValidationError):
            LintProposal(**_valid(target_type='Not Valid'))

    def test_description_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            LintProposal(**_valid(description='x' * 501))

    @pytest.mark.parametrize(
        'key', ['resolution', 'rule_metadata', 'proposed_action', 'vaults_affected']
    )
    def test_reserved_evidence_keys_rejected(self, key: str) -> None:
        with pytest.raises(ValidationError, match='reserved'):
            LintProposal(**_valid(evidence={key: {}}))

    def test_oversized_evidence_rejected(self) -> None:
        with pytest.raises(ValidationError, match='too large'):
            LintProposal(**_valid(evidence={'blob': 'x' * 20_000}))

    def test_proposed_action_typed(self) -> None:
        p = LintProposal(**_valid(proposed_action=ProposedAction(action_name='no_op', params={})))
        assert p.proposed_action is not None
        assert p.proposed_action.action_name == 'no_op'


class TestLintRuleSubclass:
    def test_subclass_carries_metadata_into_build(self) -> None:
        class DecommissionedSkillRef(LintRule):
            rule_name: str = 'decommissioned-skill-ref'
            lint_type: str = 'governance'
            description: str = 'Unit cites a skill retired in the 2026-05 cleanup.'

        rule = DecommissionedSkillRef()
        proposal = rule.build(
            vault_id='hermes',
            target_type='memory_unit',
            target_id='u1',
            suggested_action='Deprioritise the unit.',
            evidence={'skill': 'old-router'},
            proposed_action=ProposedAction(
                action_name='deprioritize_unit', params={'reason': 'decommissioned'}
            ),
        )
        assert isinstance(proposal, LintProposal)
        assert proposal.rule_name == 'decommissioned-skill-ref'
        assert proposal.lint_type == 'governance'
        assert proposal.description.startswith('Unit cites')
        assert proposal.target_id == 'u1'
        assert proposal.proposed_action.action_name == 'deprioritize_unit'

    def test_rule_validates_its_own_metadata(self) -> None:
        with pytest.raises(ValidationError):

            class _Bad(LintRule):
                rule_name: str = 'Bad Name'
                lint_type: str = 'routing'
                description: str = 'x'

            _Bad()

    def test_build_round_trips_to_dict_for_the_wire(self) -> None:
        rule = LintRule(rule_name='adhoc-rule', lint_type='quality', description='ad hoc')
        proposal = rule.build(target_type='note', target_id='n1', suggested_action='look at it')
        wire = proposal.model_dump(mode='json')
        assert wire['rule_name'] == 'adhoc-rule'
        assert wire['vault_id'] is None
        assert wire['proposed_action'] is None
