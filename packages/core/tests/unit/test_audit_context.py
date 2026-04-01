"""Unit tests for actor context variable propagation (AC-001, AC-002)."""

import asyncio

import pytest

from memex_core.context import get_actor, set_actor, background_session


class TestActorContextVar:
    """Tests for _actor_ctx ContextVar, get_actor(), and set_actor()."""

    def test_default_actor_is_anonymous(self) -> None:
        """Fresh context returns 'anonymous'."""

        # Run in a fresh task to ensure clean context
        async def _check() -> str:
            return get_actor()

        result = asyncio.run(_check())
        assert result == 'anonymous'

    def test_set_and_get_actor(self) -> None:
        """set_actor() / get_actor() round-trip."""

        async def _check() -> str:
            set_actor('test-user')
            return get_actor()

        result = asyncio.run(_check())
        assert result == 'test-user'

    def test_set_actor_returns_value(self) -> None:
        """set_actor() returns the actor string."""

        async def _check() -> str:
            return set_actor('returned-actor')

        result = asyncio.run(_check())
        assert result == 'returned-actor'

    def test_actor_isolation_between_tasks(self) -> None:
        """Each asyncio task gets its own context copy."""

        async def _run() -> tuple[str, str]:
            set_actor('parent')

            async def _child() -> str:
                # Child inherits parent context but mutations are isolated
                set_actor('child')
                return get_actor()

            child_result = await asyncio.create_task(_child())
            parent_result = get_actor()
            return parent_result, child_result

        parent, child = asyncio.run(_run())
        assert parent == 'parent'
        assert child == 'child'


class TestBackgroundSessionActor:
    """Tests for background_session() actor parameter (AC-002)."""

    @pytest.mark.asyncio
    async def test_background_session_default_actor(self) -> None:
        """background_session() without actor arg sets actor to 'system'."""
        async with background_session(label='test'):
            assert get_actor() == 'system'

    @pytest.mark.asyncio
    async def test_background_session_custom_actor(self) -> None:
        """background_session(actor='scheduler') sets actor to 'scheduler'."""
        async with background_session(label='test', actor='scheduler'):
            assert get_actor() == 'scheduler'

    @pytest.mark.asyncio
    async def test_background_session_still_sets_session_id(self) -> None:
        """background_session() still sets session_id (no regression)."""
        from memex_core.context import get_session_id

        async with background_session(label='mytest') as sid:
            assert sid.startswith('mytest-')
            assert get_session_id() == sid
