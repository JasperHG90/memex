"""Memex memory provider — Hermes plugin entry point.

Hermes discovers plugins by importing ``__init__.py`` and calling ``register``.

Memory provider registration has had two paths historically:

1. ``PluginContext.register_memory_provider()`` — used by older
   hermes-cli; was removed in current versions in favour of (2).
2. Config-driven: hermes-cli reads ``memory.provider`` from the user's
   ``config.yaml`` and imports the provider directly. This path is
   independent of ``register()`` and works on its own.

We call (1) only when the method exists so we don't log a scary
WARNING on newer hermes-cli; (2) handles registration either way.
"""

from __future__ import annotations

import logging

from .provider import MemexMemoryProvider


logger = logging.getLogger(__name__)


def register(ctx: object) -> None:
    """Register Memex as the active memory provider — best effort.

    Older hermes-cli exposes ``ctx.register_memory_provider``; the
    current API drops it and routes via ``memory.provider`` config
    instead. We call the method only if it exists so neither path
    raises.
    """
    register_method = getattr(ctx, 'register_memory_provider', None)
    if callable(register_method):
        register_method(MemexMemoryProvider())
    else:
        logger.debug(
            'PluginContext has no register_memory_provider — relying on '
            'config-driven memory.provider routing (memory.provider: memex).'
        )


__all__ = ['MemexMemoryProvider', 'register']
