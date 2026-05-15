"""Surgical observation-refresh worker tests.

V21 closes the deprioritization leak by refreshing observations after an
MU is deprioritized. The refresh worker re-synthesizes an observation on
the surviving evidence, or drops it if no evidence survives — with a
``min_evidence_for_obs_retention`` guardrail against LLM false-positive
``should_drop=True``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4


from memex_core.memory.reflect.prompts import RefreshedObservation
from memex_core.memory.reflect.reflection import _drop_observation_in_place
from memex_core.memory.sql_models import MentalModel


def _mk_mm(observations: list[dict]) -> MentalModel:
    return MentalModel(
        id=uuid4(),
        entity_id=uuid4(),
        vault_id=uuid4(),
        name='Entity',
        observations=observations,
        last_refreshed=datetime.now(timezone.utc),
        version=1,
    )


def test_drop_observation_in_place_removes_matching_dict_entry():
    target_id = uuid4()
    keep_id = uuid4()
    mm = _mk_mm(
        [
            {'id': str(target_id), 'title': 'A', 'content': '', 'evidence': []},
            {'id': str(keep_id), 'title': 'B', 'content': '', 'evidence': []},
        ]
    )
    _drop_observation_in_place(mm, target_id)
    remaining_ids = {o['id'] for o in mm.observations}
    assert remaining_ids == {str(keep_id)}


def test_drop_observation_in_place_handles_pydantic_observation_instance():
    """Mid-Phase-4 reconstruction may put Observation instances on mm.observations."""
    from memex_core.memory.sql_models import Observation

    target_id = uuid4()
    keep_id = uuid4()
    mm = _mk_mm(
        [
            Observation(id=target_id, title='A', content='', evidence=[]),
            Observation(id=keep_id, title='B', content='', evidence=[]),
        ]  # type: ignore[list-item]
    )
    _drop_observation_in_place(mm, target_id)
    remaining_ids = {(o.id if hasattr(o, 'id') else UUID(o['id'])) for o in mm.observations}
    assert remaining_ids == {keep_id}


def test_drop_observation_in_place_no_match_is_noop():
    other_id = uuid4()
    mm = _mk_mm([{'id': str(other_id), 'title': 'A', 'content': '', 'evidence': []}])
    _drop_observation_in_place(mm, uuid4())
    assert len(mm.observations) == 1


def test_refresh_signature_should_drop_default_false():
    """RefreshedObservation.should_drop defaults to False so a sparse LLM output
    doesn't accidentally drop an observation."""
    r = RefreshedObservation(content='c', title='t')
    assert r.should_drop is False
    assert r.dropped_reason is None


def test_refresh_signature_allows_explicit_drop_with_reason():
    r = RefreshedObservation(
        content='', title='', should_drop=True, dropped_reason='no surviving evidence'
    )
    assert r.should_drop is True
    assert r.dropped_reason == 'no surviving evidence'
