"""HTTP 400 + structured detail for ``memex_memory_deprioritize`` on an Observation.id.

V21 contract: passing an observation UUID to the deprio endpoint returns
HTTP 400 with body ``{error, source_memory_units}``. Two paths must hold
this contract:

1. The route handler at ``server/memories.py`` catches
   ``ObservationReadOnlyError`` explicitly BEFORE ``MemexError`` and emits
   a structured 400.
2. The fallback ``_handle_error`` in ``server/common.py`` ALSO checks for
   ``ObservationReadOnlyError`` first — so any future call site that
   routes through it cannot regress the contract to a flattened string.
"""

from __future__ import annotations

from uuid import uuid4


from memex_common.exceptions import (
    MemexError,
    MemoryUnitNotFoundError,
    ObservationReadOnlyError,
)
from memex_core.server.common import _handle_error


def test_observation_read_only_error_carries_source_memory_units():
    mu_a, mu_b = uuid4(), uuid4()
    e = ObservationReadOnlyError([mu_a, mu_b])
    assert isinstance(e, MemexError)
    assert e.source_memory_units == [mu_a, mu_b]
    assert e.details['source_memory_units'] == [str(mu_a), str(mu_b)]


def test_handle_error_returns_structured_400_for_observation_read_only():
    mu_a, mu_b = uuid4(), uuid4()
    exc = ObservationReadOnlyError([mu_a, mu_b])
    response = _handle_error(exc, 'context')
    assert response.status_code == 400
    assert isinstance(response.detail, dict), (
        f'expected dict detail, got {type(response.detail)}: {response.detail!r}'
    )
    assert response.detail['error'] == 'observations are read-only'
    assert response.detail['source_memory_units'] == [str(mu_a), str(mu_b)]


def test_handle_error_does_not_flatten_observation_read_only_to_string():
    """Defence-in-depth: even if the route handler forgets the explicit catch,
    _handle_error must not let the generic MemexError branch swallow this exception
    and flatten the structured detail to ``str(e)``."""
    exc = ObservationReadOnlyError([uuid4()])
    response = _handle_error(exc, 'context')
    assert not isinstance(response.detail, str)


def test_handle_error_still_returns_404_for_memory_unit_not_found():
    """Regression guard: the new ObservationReadOnlyError clause must precede
    the existing ResourceNotFoundError branch but not shadow it."""
    exc = MemoryUnitNotFoundError('Memory unit not found.')
    response = _handle_error(exc, 'context')
    assert response.status_code == 404


def test_observation_read_only_is_memex_error_subclass():
    """MRO invariant: ObservationReadOnlyError MUST subclass MemexError.

    The dispatch ordering in _handle_error and the route handler is built
    on this relationship — both check the specific class FIRST so the
    structured-detail branch wins over the generic ``MemexError`` branch
    that flattens detail to ``str(e)``. A future refactor that breaks the
    subclass relationship (or reorders the ``except`` branches) would
    silently regress the 400 contract to a string detail.
    """
    assert issubclass(ObservationReadOnlyError, MemexError), (
        'ObservationReadOnlyError must subclass MemexError; the route handler '
        'and _handle_error depend on this for dispatch.'
    )
    assert MemexError in ObservationReadOnlyError.__mro__


def test_handle_error_specific_branch_wins_over_generic_memex_error():
    """Dispatch-order invariant: a generic ``MemexError`` flattens to a string
    detail, while ``ObservationReadOnlyError`` (subclass) returns a dict.

    If a future refactor reorders ``_handle_error`` so that
    ``isinstance(e, MemexError)`` is checked BEFORE
    ``isinstance(e, ObservationReadOnlyError)``, this test fails — the
    subclass would be swallowed by the broader branch and the dict detail
    would be replaced with ``str(e)``.
    """
    generic_response = _handle_error(MemexError('generic'), 'ctx')
    assert isinstance(generic_response.detail, str), (
        'baseline: generic MemexError should flatten to string detail'
    )
    specific_response = _handle_error(ObservationReadOnlyError([uuid4()]), 'ctx')
    assert isinstance(specific_response.detail, dict), (
        'ObservationReadOnlyError must NOT be swallowed by the generic '
        'MemexError branch — dispatch order must check the specific class first.'
    )
