"""Unit tests for `OverlapError` exception in `memex_core.processing.batch`.

`OverlapError` is raised by `JobManager.create_job` when the incoming batch
overlaps an in-flight job. The HTTP layer translates it to 409 + Location header.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from memex_core.processing.batch import OverlapError


def test_overlap_error_carries_existing_id_and_status():
    eid = uuid4()
    err = OverlapError(existing_id=eid, status='processing')
    assert err.existing_id == eid
    assert err.status == 'processing'


def test_overlap_error_default_overlapping_keys_is_empty_list():
    """RFC-002 §A4: `overlapping_keys` is forward-compatible. The exception
    signature accepts the field; subset computation is deferred so the value
    today is always `[]`. Default must not be a shared mutable default."""
    err1 = OverlapError(existing_id=uuid4(), status='pending')
    err2 = OverlapError(existing_id=uuid4(), status='pending')
    assert err1.overlapping_keys == []
    assert err2.overlapping_keys == []
    err1.overlapping_keys.append('k1')
    # Mutating one instance must not affect another (no shared mutable default).
    assert err2.overlapping_keys == []


def test_overlap_error_accepts_explicit_keys_list():
    err = OverlapError(
        existing_id=uuid4(),
        status='processing',
        overlapping_keys=['k1', 'k2'],
    )
    assert err.overlapping_keys == ['k1', 'k2']


def test_overlap_error_explicit_none_becomes_empty_list():
    """Caller passing `overlapping_keys=None` must be coerced to `[]`."""
    err = OverlapError(existing_id=uuid4(), status='pending', overlapping_keys=None)
    assert err.overlapping_keys == []


def test_overlap_error_message_includes_id_status_and_count():
    eid = uuid4()
    err = OverlapError(
        existing_id=eid,
        status='processing',
        overlapping_keys=['a', 'b', 'c'],
    )
    msg = str(err)
    assert str(eid) in msg
    assert 'processing' in msg
    assert '3 keys' in msg


def test_overlap_error_is_exception_subclass():
    err = OverlapError(existing_id=uuid4(), status='pending')
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# F16: existing_id is coerced to UUID regardless of input shape
# ---------------------------------------------------------------------------


def test_overlap_error_existing_id_is_always_uuid_when_given_uuid():
    """F16: the field type is documented as `UUID`. Direct UUID input passes
    through as-is."""
    eid = uuid4()
    err = OverlapError(existing_id=eid, status='pending')
    assert isinstance(err.existing_id, UUID)
    assert err.existing_id == eid


def test_overlap_error_existing_id_is_coerced_from_str():
    """F16: a stringified UUID (which can sneak in from raw asyncpg rows or a
    future serialization layer) is coerced to a real `UUID` so HTTP-layer
    callers building the `Location` header don't accidentally serialize the
    raw string twice."""
    eid = uuid4()
    err = OverlapError(existing_id=str(eid), status='pending')
    assert isinstance(err.existing_id, UUID)
    assert err.existing_id == eid


def test_overlap_error_existing_id_coercion_rejects_garbage():
    """F16: an obviously-invalid input still surfaces as a clear ValueError
    from `UUID()` rather than producing a corrupt OverlapError that would
    fail later when the HTTP layer tries to build the Location header."""
    import pytest

    with pytest.raises((ValueError, TypeError)):
        OverlapError(existing_id='not-a-uuid', status='pending')
