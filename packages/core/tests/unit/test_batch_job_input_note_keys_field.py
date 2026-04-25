"""Unit-level checks on the new `BatchJob.input_note_keys` field's column metadata.

Behavioural tests against a real Postgres are in
`tests/integration/test_int_alembic_021.py` and the upcoming
`tests/integration/test_int_batch_overlap.py`. The tests here only need to
introspect the SQLAlchemy column declared by the SQLModel.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import JSONB

from memex_core.memory.sql_models import BatchJob


def _column():
    """Return the SQLAlchemy `Column` object for `BatchJob.input_note_keys`."""
    return BatchJob.__table__.columns['input_note_keys']


def test_input_note_keys_column_exists():
    assert 'input_note_keys' in BatchJob.__table__.columns


def test_input_note_keys_column_type_is_jsonb():
    col = _column()
    assert isinstance(col.type, JSONB), f'Expected JSONB column, got {type(col.type)!r}.'


def test_input_note_keys_column_is_not_nullable():
    """AC-019 (a): the migration declares NOT NULL; the SQLModel must agree so
    inserts that omit the field still hit the server-side default."""
    assert _column().nullable is False


def test_input_note_keys_column_has_literal_jsonb_default():
    """AC-019 (c): the SQLModel field's `server_default` must be the literal
    `'[]'::jsonb` text — same shape as the migration so the column declaration
    and the migration agree."""
    col = _column()
    assert col.server_default is not None, 'Expected a server_default to be present.'
    text_arg = str(col.server_default.arg)
    assert "'[]'" in text_arg
    assert 'jsonb' in text_arg.lower()


def test_input_note_keys_default_is_empty_list():
    """An instantiated BatchJob with no explicit input_note_keys should report []."""
    from uuid import uuid4

    job = BatchJob(vault_id=uuid4(), notes_count=0)
    assert job.input_note_keys == []
