"""CLI entry point invoked by the composite action."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .agent import build_agent, run
from .config import HermesRunnerSettings

_STDOUT_CHAR_LIMIT = 900_000


def _runner_temp() -> Path:
    return Path(os.environ.get('RUNNER_TEMP', '/tmp'))


def main() -> int:
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    agent = build_agent(settings)
    result = run(settings, agent)

    final = str(result.get('final_response', ''))
    temp = _runner_temp()
    temp.mkdir(parents=True, exist_ok=True)
    (temp / 'hermes-response.txt').write_text(final, encoding='utf-8')
    (temp / 'hermes-trajectory.json').write_text(
        json.dumps(result.get('messages', []), default=str),
        encoding='utf-8',
    )

    if settings.output_file is not None:
        settings.output_file.parent.mkdir(parents=True, exist_ok=True)
        settings.output_file.write_text(final, encoding='utf-8')

    sys.stdout.write(final[:_STDOUT_CHAR_LIMIT])
    if len(final) > _STDOUT_CHAR_LIMIT:
        sys.stdout.write(f'\n\n[stdout truncated at {_STDOUT_CHAR_LIMIT} chars]\n')
    sys.stdout.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
