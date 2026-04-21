"""Stub for hermes-agent's ``tools.registry`` module.

Provides ``tool_error`` used by the plugin at import time.
Refresh from https://github.com/NousResearch/hermes-agent/blob/main/tools/registry.py
if the upstream signature changes.
"""

from __future__ import annotations

import json
from typing import Any


def tool_error(message: str, **extra: Any) -> str:
    """Format a tool error as a JSON string.

    Mirrors the upstream Hermes helper. The real implementation includes more
    structure; we keep the minimum surface the plugin relies on.
    """
    payload: dict[str, Any] = {'error': message}
    payload.update(extra)
    return json.dumps(payload)
