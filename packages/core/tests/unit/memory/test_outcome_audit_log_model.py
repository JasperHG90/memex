"""DB-level invariants on the OutcomeAuditLog model.

SQLModel disables Pydantic validators on `table=True` classes; the real
shape guarantees live in the migration + the SQLAlchemy column types, so
those are what we pin here. The Pydantic validator + max_length declarations
remain in the model for callers that build the row via `.model_validate(...)`
(documentation + future-proofing).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String

from memex_core.memory.sql_models import OutcomeAuditLog


def test_outcome_audit_log_units_is_array_check_constraint_declared():
    """The `jsonb_typeof(units) = 'array'` CHECK lives on `__table_args__`."""
    check_constraints = [
        c for c in OutcomeAuditLog.__table_args__ if isinstance(c, CheckConstraint)
    ]
    matched = [c for c in check_constraints if c.name == 'outcome_audit_log_units_is_array']
    assert matched, 'CHECK constraint outcome_audit_log_units_is_array must be declared'
    sql = str(matched[0].sqltext)
    assert 'jsonb_typeof' in sql
    assert 'array' in sql


def test_outcome_audit_log_caller_id_column_caps_at_128_chars():
    """caller_id is `VARCHAR(128)` at the DB layer."""
    col = OutcomeAuditLog.__table__.c['caller_id']
    assert isinstance(col.type, String)
    assert col.type.length == 128


def test_outcome_audit_log_caller_id_max_length_field_metadata():
    """The Pydantic field also declares `max_length=128` for caller-side validation."""
    field_info = OutcomeAuditLog.model_fields['caller_id']
    metadata_lengths = [
        getattr(m, 'max_length', None) for m in field_info.metadata if hasattr(m, 'max_length')
    ]
    assert 128 in metadata_lengths
