"""Regression tests for virtual-unit serialization (GH #47: Ghost IDs).

Virtual MemoryUnits are synthesized at recall time from MentalModel
observations; they have no backing DB row. The serializer must flag them
clearly so agents do not attempt point-lookups on their synthetic ids.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from memex_hermes_plugin.memex.tools import (
    _serialize_memory_unit,
    _serialize_memory_unit_full,
)


def _virtual_unit(mm_id, evidence_ids):
    return SimpleNamespace(
        id=uuid4(),
        text='[Entity] Obs title: content',
        fact_type='observation',
        status='active',
        note_id=None,
        mentioned_at=None,
        metadata={
            'virtual': True,
            'observation': True,
            'mental_model_id': str(mm_id),
            'evidence_ids': [str(e) for e in evidence_ids],
            'trend': 'new',
        },
    )


def _real_unit():
    return SimpleNamespace(
        id=uuid4(),
        text='A real fact',
        fact_type='world',
        status='active',
        note_id=uuid4(),
        mentioned_at=None,
        metadata={},
    )


def test_serialize_memory_unit_flags_virtual():
    mm_id = uuid4()
    evidence_ids = [uuid4(), uuid4()]
    out = _serialize_memory_unit(_virtual_unit(mm_id, evidence_ids))

    assert out['virtual'] is True
    assert out['mental_model_id'] == str(mm_id)
    assert out['evidence_ids'] == [str(e) for e in evidence_ids]
    assert out['note_id'] is None


def test_serialize_memory_unit_omits_virtual_fields_for_real_units():
    out = _serialize_memory_unit(_real_unit())
    assert 'virtual' not in out
    assert 'mental_model_id' not in out
    assert 'evidence_ids' not in out
    assert out['note_id'] is not None


def test_serialize_memory_unit_full_flags_virtual():
    mm_id = uuid4()
    evidence_ids = [uuid4(), uuid4()]
    out = _serialize_memory_unit_full(_virtual_unit(mm_id, evidence_ids))

    assert out['virtual'] is True
    assert out['mental_model_id'] == str(mm_id)
    assert out['evidence_ids'] == [str(e) for e in evidence_ids]
    assert out['note_id'] is None


def test_serialize_memory_unit_full_omits_virtual_fields_for_real_units():
    out = _serialize_memory_unit_full(_real_unit())
    assert 'virtual' not in out
    assert 'mental_model_id' not in out
    assert 'evidence_ids' not in out
