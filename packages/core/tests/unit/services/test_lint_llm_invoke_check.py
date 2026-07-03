"""Unit tests for F10b ``_invoke_check`` dispatch.

The service introspects ``run_llm_check`` once via ``inspect.signature`` and
caches the result, so a context-aware check receives the ``CheckContext`` and
a legacy 3-arg check is called without it. A ``TypeError`` raised inside the
check body must propagate (no silent retry).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memex_core.memory.lint_llm.types import CheckContext
from memex_core.services.lint_llm import (
    _CONTEXT_AWARE_CHECK_CACHE,
    _check_accepts_context,
    _invoke_check,
)


@pytest.fixture(autouse=True)
def _clear_check_cache():
    _CONTEXT_AWARE_CHECK_CACHE.clear()
    yield
    _CONTEXT_AWARE_CHECK_CACHE.clear()


class TestCheckAcceptsContext:
    def test_three_arg_check_does_not_accept_context(self):
        async def legacy(unit_id, vault_id, session):
            return None

        assert _check_accepts_context(legacy) is False

    def test_check_with_explicit_context_kwarg_accepts(self):
        async def modern(unit_id, vault_id, session, *, context):
            return None

        assert _check_accepts_context(modern) is True

    def test_check_with_var_keywords_accepts(self):
        async def flexible(unit_id, vault_id, session, **kwargs):
            return None

        assert _check_accepts_context(flexible) is True

    def test_result_is_cached(self):
        async def legacy(unit_id, vault_id, session):
            return None

        _check_accepts_context(legacy)
        assert legacy in _CONTEXT_AWARE_CHECK_CACHE
        assert _CONTEXT_AWARE_CHECK_CACHE[legacy] is False


class TestInvokeCheckDispatch:
    @pytest.mark.asyncio
    async def test_legacy_check_called_without_context(self):
        seen: dict = {}

        async def legacy(unit_id, vault_id, session):
            seen['called'] = True
            seen['kwargs'] = False
            return None

        await _invoke_check(legacy, uuid4(), uuid4(), AsyncMock(), CheckContext())
        assert seen == {'called': True, 'kwargs': False}

    @pytest.mark.asyncio
    async def test_context_aware_check_receives_context(self):
        captured: dict = {}

        async def modern(unit_id, vault_id, session, *, context):
            captured['context'] = context
            return None

        ctx = CheckContext()
        await _invoke_check(modern, uuid4(), uuid4(), AsyncMock(), ctx)
        assert captured['context'] is ctx

    @pytest.mark.asyncio
    async def test_typeerror_inside_check_body_propagates(self):
        """A TypeError raised inside the check body must not be silently swallowed
        as if it were an unexpected-kwarg signature mismatch."""

        async def buggy(unit_id, vault_id, session, *, context):
            raise TypeError('genuine bug inside check body')

        with pytest.raises(TypeError, match='genuine bug inside check body'):
            await _invoke_check(buggy, uuid4(), uuid4(), AsyncMock(), CheckContext())
