"""Suite-private outcomes for ``procedural_plane``.

Two outcomes pin the procedural-plane contract:

* ``procedural_entry_roundtrip`` — exercises a write→read cycle
  (create, upsert, get_by_identity). The DirectApiBackend issues the
  call and packs the result DTO into ``answer.units`` (single element)
  and any error into ``answer.error``. The outcome asserts the call
  outcome (success/conflict/not_found) and optionally matches a field
  (kind, scope, verb) on the returned DTO.

* ``procedural_search_results`` — exercises a read call
  (search, briefing_cards). The DirectApiBackend issues the call and
  packs the hits into ``answer.units``. The outcome asserts the hit
  list meets the expected cardinality and matches at least one entry
  on the (kind, scope, verb) tuple.

Both outcomes are suite-private. They live in the suite package (not
in ``memex_eval.suite.base``) because procedural-plane behaviour
is a single-suite concern today — promotion to core happens when a
SECOND suite needs the same outcome shape.
"""

from __future__ import annotations

from typing import Literal

from memex_eval.suite.agents import AgentAnswer
from memex_eval.suite.base import ExpectedOutcomeBase, register_outcome


# ---------------------------------------------------------------------------
# procedural_entry_roundtrip — write/read on a single entry
# ---------------------------------------------------------------------------


@register_outcome('procedural_entry_roundtrip')
class ProceduralEntryRoundtrip(ExpectedOutcomeBase):
    """Assert a single procedural-plane write/read call met the spec.

    The DirectApiBackend dispatches on the outcome's ``operation`` to
    call the right ``api.procedural_*`` method, packs the returned DTO
    into ``answer.units[0]`` (or leaves it empty on miss), and any
    raised error into ``answer.error``.

    Pass criteria:
    - ``expect_status='success'``  — ``answer.error`` is None and
      ``answer.units`` has at least one entry.
    - ``expect_status='conflict'`` — ``answer.error`` indicates a
      409 / identity-anchor collision.
    - ``expect_status='not_found'``— ``answer.error`` indicates a
      404 / miss.
    - ``expect_status='any'``      — call completed (success or
      expected error) without an unexpected exception.

    When ``expect_kind`` (or ``expect_scope`` / ``expect_verb``) is set,
    the outcome additionally asserts the returned DTO carries that
    field. ``expect_verb`` is optional because the procedural contract permits
    ``verb`` to be NULL for ``case``-kind entries.
    """

    type: Literal['procedural_entry_roundtrip']
    # Which API call to make. Drives DirectApiBackend dispatch.
    operation: Literal['create', 'upsert', 'get_by_identity']
    # The identity-anchor: kind, scope, (verb, context) for
    # procedure/strategy; just (kind, scope) for case-kind entries.
    # DirectApiBackend builds the payload from these.
    kind: Literal['procedure', 'strategy']
    scope: str
    verb: str | None = None
    context: str | None = None
    # Case-kind entries carry a trigger signal, NOT verb/context.
    # The DirectApiBackend forward-fills it on the create/upsert
    # payload so the case-shape DTO validates.
    trigger: str | None = None
    # What we expect to come back.
    expect_status: Literal['success', 'conflict', 'not_found', 'any'] = 'success'
    # Field-level match on the returned DTO (None = don't check).
    expect_kind: str | None = None
    expect_scope: str | None = None
    expect_verb: str | None = None
    # Free-form title for create/upsert (no semantic meaning; just
    # varies across scenarios to keep the test corpus distinct).
    title: str = 'procedural-suite-entry'

    def score(
        self,
        answer: AgentAnswer,
        scenario,
        **_kw,
    ) -> dict[str, float]:
        outcome_status = _classify_outcome(answer)
        passed = outcome_status == self.expect_status

        # Field-level match (only when the call succeeded AND we
        # asked for field matching).
        match_ok = True
        if passed and answer.units and self.expect_status == 'success':
            dto = answer.units[0]
            if self.expect_kind is not None and getattr(dto, 'kind', None) != self.expect_kind:
                match_ok = False
            if self.expect_scope is not None and getattr(dto, 'scope', None) != self.expect_scope:
                match_ok = False
            if self.expect_verb is not None and getattr(dto, 'verb', None) != self.expect_verb:
                match_ok = False

        return {
            'pass': 1.0 if (passed and match_ok) else 0.0,
            'outcome_status_match': 1.0 if passed else 0.0,
            'field_match': 1.0 if match_ok else 0.0,
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'outcome_status_match', 'field_match']


# ---------------------------------------------------------------------------
# procedural_search_results — read calls that return a list
# ---------------------------------------------------------------------------


@register_outcome('procedural_search_results')
class ProceduralSearchResults(ExpectedOutcomeBase):
    """Assert a procedural-plane read call returned the expected hits.

    The DirectApiBackend dispatches on ``operation='search'`` or
    ``operation='briefing_cards'`` to call the right
    ``api.procedural_*`` method, packs the hits into
    ``answer.units`` (one element per hit), and any error into
    ``answer.error``.

    Pass criteria (all must hold):
    - ``answer.error`` is None (the call itself succeeded).
    - ``len(answer.units) >= min_hits`` (cardinality floor).
    - If ``expect_kind`` (or any matching field) is set, at least one
      hit must carry that field. A regression that re-typed the hits
      to the wrong kind (e.g. ``case`` instead of ``procedure``)
      would surface here.
    """

    type: Literal['procedural_search_results']
    operation: Literal['search', 'briefing_cards']
    # The query / pin context to dispatch on. DirectApiBackend maps
    # these into the right API call shape.
    query: str | None = None
    context_keys: list[str] | None = None
    # Cardinality floor — the procedural contract guarantees at least this
    # many hits in the seeded scenario.
    min_hits: int = 1
    # Field-level match (at least one hit must carry these).
    expect_kind: str | None = None
    expect_scope: str | None = None
    expect_verb: str | None = None
    # Optional: pin position contract for briefing_cards. The card
    # list is sorted by pin order, so ``expect_first_pin_pos``=0
    # asserts the first card is the most-general pin (global).
    expect_first_pin_pos: int | None = None

    def score(
        self,
        answer: AgentAnswer,
        scenario,
        **_kw,
    ) -> dict[str, float]:
        if answer.error is not None:
            return {
                'pass': 0.0,
                'error': 0.0,
                'cardinality': 0.0,
                'field_match': 0.0,
            }

        cardinality_ok = len(answer.units) >= self.min_hits

        field_match_ok = True
        if self.expect_kind is not None or self.expect_scope is not None:
            for hit in answer.units:
                if self.expect_kind is not None and getattr(hit, 'kind', None) == self.expect_kind:
                    break
                if (
                    self.expect_scope is not None
                    and getattr(hit, 'scope', None) == self.expect_scope
                ):
                    break
            else:
                # No hit matched ANY of the expected fields.
                if self.expect_kind is not None and not any(
                    getattr(h, 'kind', None) == self.expect_kind for h in answer.units
                ):
                    field_match_ok = False
                if self.expect_scope is not None and not any(
                    getattr(h, 'scope', None) == self.expect_scope for h in answer.units
                ):
                    field_match_ok = False

        if self.expect_verb is not None and not any(
            getattr(h, 'verb', None) == self.expect_verb for h in answer.units
        ):
            field_match_ok = False

        pin_pos_ok = True
        if self.expect_first_pin_pos is not None and answer.units:
            # For briefing_cards, the DTOs are ProceduralBriefingCard
            # which expose ``pin_position``. For search, the DTOs are
            # ProceduralSearchHit which don't — only meaningful for
            # briefing_cards.
            first_pos = getattr(answer.units[0], 'pin_position', None)
            pin_pos_ok = first_pos == self.expect_first_pin_pos

        passed = cardinality_ok and field_match_ok and pin_pos_ok
        return {
            'pass': 1.0 if passed else 0.0,
            'error': 1.0 if answer.error is None else 0.0,
            'cardinality': 1.0 if cardinality_ok else 0.0,
            'field_match': 1.0 if field_match_ok else 0.0,
            'pin_position': 1.0 if pin_pos_ok else 0.0,
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'error', 'cardinality', 'field_match', 'pin_position']


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_outcome(answer: AgentAnswer) -> str:
    """Map an AgentAnswer to one of success / conflict / not_found / any_error.

    The procedural API uses HTTP semantics; the client surfaces 409
    on identity-anchor collision and 404 on miss / vault-mismatch. The
    error string the client raises carries the status code; we
    classify by substring match against the canonical shapes.
    """
    if answer.error is None:
        if answer.units:
            return 'success'
        return 'not_found'
    err = answer.error.lower()
    if '409' in err or 'conflict' in err:
        return 'conflict'
    if '404' in err or 'not found' in err:
        return 'not_found'
    return 'any_error'


@register_outcome('case_submit_roundtrip')
class CaseSubmitRoundtrip(ExpectedOutcomeBase):
    """Gate the case_submit path: a worked episode files as a NOTE in
    the hidden system vault and the assignment resolves to one of the
    accepted modes.

    Determinism note: the eval gives ``case_of`` explicitly (resolved
    from a seeded anchor by the backend) so the LLM judge never runs —
    ``assignment.mode='explicit'`` is the deterministic expectation.
    The backend packs the ``CaseSubmitResult`` into ``answer.units[0]``.
    """

    type: Literal['case_submit_roundtrip'] = 'case_submit_roundtrip'

    title: str = 'procedural-suite-case'
    trigger: str = 'eval case trigger'
    outcome_value: Literal['success', 'failure', 'mixed'] = 'success'
    # Anchor of the seeded procedure to attach the case to (resolved to
    # an entry id by the backend via get_by_identity → case_of).
    case_of_verb: str | None = None
    case_of_context: str | None = None
    case_of_scope: str = 'global'
    expect_assignment_modes: list[str] = ['explicit']

    def metric_keys(self) -> list[str]:
        return ['pass', 'assignment_mode_match']

    def score(self, answer, *, scenario, context=None, **_kw) -> dict[str, float]:
        if answer.error or not answer.units:
            return {'pass': 0.0, 'assignment_mode_match': 0.0}
        result = answer.units[0]
        note_id = getattr(result, 'note_id', None)
        assignment = getattr(result, 'assignment', None)
        mode = getattr(assignment, 'mode', None)
        mode_ok = 1.0 if mode in self.expect_assignment_modes else 0.0
        return {
            'pass': 1.0 if (note_id is not None and mode_ok) else 0.0,
            'assignment_mode_match': mode_ok,
        }
