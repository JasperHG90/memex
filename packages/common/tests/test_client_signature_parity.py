"""Strict signature parity between RemoteMemexAPI and MemexAPI.

Mismatched kwargs are silent at type-check time because the MCP / Hermes /
CLI layer goes through a ``MemexAPIProtocol`` with ``**kwargs: Any``. This
test catches the gap by introspecting both classes and asserting zero
drift on shared method names.

New kwargs on either side MUST land on the other (or the method dropped
from one side). There is no allow-list — every gap is a TODO refactor in
disguise, and an allow-list rots into license for the next gap. See
::

    memex note   # 8fc24362-81b0-403c-8b2f-4d2554e710cf

for the corrected framing.
"""

from __future__ import annotations

import inspect

import pytest

from memex_common.client import RemoteMemexAPI
from memex_core.api import MemexAPI


def _kwarg_names(fn: object) -> set[str]:
    try:
        sig = inspect.signature(fn)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return set()
    return {
        name
        for name, param in sig.parameters.items()
        if param.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        and name not in ('self', 'args', 'kwargs')
    }


def _shared_methods() -> list[str]:
    """Methods that exist on both classes — candidates for parity."""
    return sorted(
        name
        for name in dir(RemoteMemexAPI)
        if not name.startswith('_')
        and callable(getattr(RemoteMemexAPI, name, None))
        and callable(getattr(MemexAPI, name, None))
    )


def test_zero_signature_drift() -> None:
    """Every shared method must have identical kwargs on both classes.

    If you arrived here from a CI failure: a kwarg landed on one of the
    two surfaces without the other. Pick one:

    1. Mirror the kwarg on the other class (preferred).
    2. Drop the method from one side if it's genuinely client-only or
       server-only.
    3. If you believe the gap *must* exist (e.g., FastAPI BackgroundTasks
       cannot cross HTTP), implement a parity stub that accepts the
       parameter and raises ``NotImplementedError`` when the in-process
       semantics cannot be honoured — same pattern as
       ``RemoteMemexAPI.deprioritize_memory_unit(background_tasks=…)``.

    There is intentionally **no allow-list** here. Any "intentional drift"
    is in practice a TODO refactor — allow-list entries rot fast and
    silently soften the test into documentation.
    """
    failures: list[str] = []
    for name in _shared_methods():
        remote_kw = _kwarg_names(getattr(RemoteMemexAPI, name))
        local_kw = _kwarg_names(getattr(MemexAPI, name))
        if remote_kw == local_kw:
            continue

        local_only = local_kw - remote_kw
        remote_only = remote_kw - local_kw
        failures.append(
            f'{name}:\n'
            f'  MemexAPI accepts but RemoteMemexAPI does not: '
            f'{sorted(local_only)}\n'
            f'  RemoteMemexAPI accepts but MemexAPI does not: '
            f'{sorted(remote_only)}'
        )

    if failures:
        msg = (
            'RemoteMemexAPI ↔ MemexAPI signature parity broken in '
            f'{len(failures)} method(s):\n\n'
            + '\n\n'.join(failures)
            + '\n\nSee the docstring of `test_zero_signature_drift` for the '
            'three accepted resolutions. Do NOT add an allow-list.'
        )
        pytest.fail(msg)
