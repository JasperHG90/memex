"""Unit tests for external lint-proposal validation (no database).

The request model is the door: reserved rule names, slug hygiene, enum
membership, evidence key/size fences, and proposed-action catalogue
compatibility all reject HERE so a bad proposal never reaches the insert.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from memex_core.services.lint_external import (
    RESERVED_RULE_NAMES,
    ExternalProposalRejected,
    ExternalProposalRequest,
    SubmissionItemResult,
    action_descriptor,
    validate_proposed_action,
)
from memex_core.services.lint import V1_RULES
from memex_core.services.proposal_actions import list_actions


class TestSharedShapeSSOT:
    """The wire shape lives once in memex_common; core only adds the
    reserved-name check. These pins stop the two surfaces from drifting."""

    def test_lint_types_parity_with_enum(self) -> None:
        from memex_common.lint import LINT_TYPES
        from memex_core.memory.sql_models import LintType

        assert set(LINT_TYPES) == {item.value for item in LintType}

    def test_request_inherits_common_shape(self) -> None:
        from memex_common.lint import LintProposal

        assert issubclass(ExternalProposalRequest, LintProposal)
        assert set(ExternalProposalRequest.model_fields) == set(LintProposal.model_fields)

    def test_proposed_action_is_the_common_object(self) -> None:
        from memex_common.lint import ProposedAction as CommonProposedAction
        from memex_core.services.lint_external import ProposedAction as CoreProposedAction

        assert CoreProposedAction is CommonProposedAction

    def test_client_model_defers_reserved_check_to_server(self) -> None:
        # The shared client model must NOT reject reserved names — that is
        # the server's authority (needs the live rule set). It only enforces
        # shape, so a reserved name passes here and is rejected by the server
        # subclass.
        from memex_common.lint import LintProposal

        ok = LintProposal(
            rule_name='composite_deprioritize_candidate',
            lint_type='quality',
            target_type='note',
            target_id='t',
            description='d',
            suggested_action='s',
        )
        assert ok.rule_name == 'composite_deprioritize_candidate'


def _request(**overrides: object) -> ExternalProposalRequest:
    base: dict[str, object] = {
        'vault_id': str(uuid4()),
        'rule_name': 'skill-misroute',
        'lint_type': 'routing',
        'target_type': 'note',
        'target_id': str(uuid4()),
        'description': 'classifier was confident but wrong',
        'suggested_action': 'route the note to the agentic vault',
    }
    base.update(overrides)
    return ExternalProposalRequest(**base)  # type: ignore[arg-type]


class TestReservedRuleNames:
    def test_reserved_set_tracks_v1_rules(self) -> None:
        assert {spec.name for spec in V1_RULES} <= RESERVED_RULE_NAMES

    @pytest.mark.parametrize('name', sorted(RESERVED_RULE_NAMES))
    def test_every_reserved_name_rejected(self, name: str) -> None:
        with pytest.raises(ValidationError, match='reserved'):
            _request(rule_name=name)

    def test_llm_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError, match='reserved'):
            _request(rule_name='llm_custom_check')


class TestRuleNameHygiene:
    @pytest.mark.parametrize('bad', ['Bad Name', 'UPPER', '9starts-with-digit', '-leading', ''])
    def test_non_slug_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            _request(rule_name=bad)

    def test_snake_and_kebab_accepted(self) -> None:
        assert _request(rule_name='my-skill-rule').rule_name == 'my-skill-rule'
        assert _request(rule_name='my_skill_rule').rule_name == 'my_skill_rule'

    def test_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            _request(rule_name='a' * 65)


class TestFieldValidation:
    def test_lint_type_must_be_enum_member(self) -> None:
        with pytest.raises(ValidationError, match='lint_type'):
            _request(lint_type='vibes')

    def test_target_type_hygiene(self) -> None:
        with pytest.raises(ValidationError):
            _request(target_type='Not Valid')

    @pytest.mark.parametrize('field', ['rule_name', 'target_type', 'target_id', 'description'])
    def test_mandatory_fields(self, field: str) -> None:
        with pytest.raises(ValidationError):
            _request(**{field: ''})

    def test_description_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            _request(description='x' * 501)


class TestEvidenceHygiene:
    @pytest.mark.parametrize(
        'key', ['resolution', 'rule_metadata', 'proposed_action', 'vaults_affected']
    )
    def test_server_owned_keys_rejected(self, key: str) -> None:
        with pytest.raises(ValidationError, match='reserved'):
            _request(evidence={key: {}})

    def test_oversized_evidence_rejected(self) -> None:
        with pytest.raises(ValidationError, match='too large'):
            _request(evidence={'blob': 'x' * 20_000})

    def test_normal_evidence_accepted(self) -> None:
        req = _request(evidence={'confidence': 0.93, 'candidates': ['a', 'b']})
        assert req.evidence['confidence'] == 0.93


class TestProposedActionValidation:
    def test_unknown_action_rejected(self) -> None:
        req = _request(proposed_action={'action_name': 'not_a_real_action', 'params': {}})
        with pytest.raises(ExternalProposalRejected, match='unknown'):
            validate_proposed_action(req)

    def test_target_type_mismatch_rejected(self) -> None:
        req = _request(
            target_type='kv',
            target_id='user:editor',
            proposed_action={'action_name': 'delete_note', 'params': {}},
        )
        with pytest.raises(ExternalProposalRejected, match='does not apply'):
            validate_proposed_action(req)

    def test_invalid_params_rejected(self) -> None:
        req = _request(proposed_action={'action_name': 'route_note_to_vault', 'params': {}})
        with pytest.raises(ExternalProposalRejected, match='params invalid'):
            validate_proposed_action(req)

    def test_valid_suggestion_passes(self) -> None:
        req = _request(
            proposed_action={
                'action_name': 'route_note_to_vault',
                'params': {'target_vault_id': str(uuid4())},
            }
        )
        validate_proposed_action(req)

    def test_absent_suggestion_passes(self) -> None:
        validate_proposed_action(_request())


class TestMetricsCardinality:
    def test_external_counter_labels_are_closed_literals(self) -> None:
        """rule_name (user-supplied free text) and vault_id would both mint
        unbounded series; the counter stays on the closed lint_type × result
        grid. Per-vault / per-rule attribution lives in the submission logs."""
        from memex_core import metrics

        assert set(metrics.LINT_EXTERNAL_PROPOSALS_TOTAL._labelnames) == {
            'lint_type',
            'result',
        }


class TestWireShapes:
    def test_action_descriptor_covers_every_registered_action(self) -> None:
        for action in list_actions():
            descriptor = action_descriptor(action)
            assert descriptor['id'] == action.id
            assert isinstance(descriptor['applicable_target_types'], list)
            assert descriptor['reversible'] == action.reversible
            assert 'params_schema' in descriptor

    def test_submission_result_as_dict_drops_empty_fields(self) -> None:
        bare = SubmissionItemResult(0, 'cooldown_suppressed')
        assert bare.as_dict() == {'index': 0, 'status': 'cooldown_suppressed'}
        full = SubmissionItemResult(1, 'created', finding_id='f-1', detail=None)
        assert full.as_dict() == {'index': 1, 'status': 'created', 'finding_id': 'f-1'}
