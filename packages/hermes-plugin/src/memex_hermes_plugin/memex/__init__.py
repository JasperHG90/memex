"""Memex memory provider — Hermes plugin entry point.

Hermes discovers plugins by importing ``__init__.py`` and calling ``register``.
"""

from __future__ import annotations

from .provider import MemexMemoryProvider


def register(ctx: object) -> None:
    """Register Memex as the active memory provider."""
    ctx.register_memory_provider(MemexMemoryProvider())  # type: ignore[attr-defined]


__all__ = ['MemexMemoryProvider', 'register']
