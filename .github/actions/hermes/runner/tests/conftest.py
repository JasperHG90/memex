from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


_RUNNER_ENV_KEYS = [
    'HERMES_RUNNER_MODEL',
    'HERMES_RUNNER_PROVIDER',
    'HERMES_RUNNER_BASE_URL',
    'HERMES_RUNNER_API_KEY',
    'HERMES_RUNNER_SYSTEM_PROMPT_FILE',
    'HERMES_RUNNER_USER_MESSAGE',
    'HERMES_RUNNER_MAX_ITERATIONS',
    'HERMES_RUNNER_ENABLED_TOOLSETS',
    'HERMES_RUNNER_DISABLED_TOOLSETS',
    'HERMES_RUNNER_OUTPUT_FILE',
]


@pytest.fixture(autouse=True)
def _clean_runner_env() -> Iterator[None]:
    snapshot = {k: os.environ.pop(k, None) for k in _RUNNER_ENV_KEYS}
    try:
        yield
    finally:
        for k, v in snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
