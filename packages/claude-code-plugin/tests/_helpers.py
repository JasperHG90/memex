"""Static helpers for the Claude Code plugin tests.

Lives outside conftest.py so tests can ``from _helpers import ...`` cleanly.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / 'scripts'
FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'


@dataclass
class MockMemex:
    """Handle to the per-test mock CLI state."""

    bin_dir: Path
    calls_file: Path
    kv_file: Path
    notes_dir: Path
    plugin_data: Path
    env: dict[str, str] = field(default_factory=dict)

    def set_kv(self, key: str, value: str) -> None:
        existing = self.kv_file.read_text() if self.kv_file.exists() else ''
        kept = '\n'.join(line for line in existing.splitlines() if not line.startswith(f'{key}='))
        body = (kept + ('\n' if kept else '') + f'{key}={value}\n').lstrip('\n')
        self.kv_file.write_text(body)

    def force_fail(self, *subcommands: str) -> None:
        """Force the given ``"<subcmd> <subsubcmd>"`` calls to exit non-zero."""
        self.env['MOCK_MEMEX_FAIL'] = ','.join(subcommands)

    def calls(self) -> list[dict[str, Any]]:
        if not self.calls_file.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.calls_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def calls_matching(self, *prefix: str) -> list[dict[str, Any]]:
        """Return calls whose argv starts with ``prefix``."""
        return [c for c in self.calls() if c.get('argv', [])[: len(prefix)] == list(prefix)]

    def note_add_files(self) -> list[Path]:
        return sorted(self.notes_dir.glob('note_add_*.txt'))

    def note_append_files(self) -> list[Path]:
        return sorted(self.notes_dir.glob('note_append_*.txt'))


def run_script(
    script_name: str,
    *,
    stdin: str = '',
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    check: bool = False,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Invoke a plugin script with isolated env and the given stdin payload."""
    base_env = {
        'HOME': os.environ.get('HOME', '/tmp'),
        'USER': os.environ.get('USER', 'tester'),
        'LANG': 'C.UTF-8',
        'TERM': 'dumb',
    }
    if env is not None:
        base_env.update(env)
    if extra_env is not None:
        base_env.update(extra_env)

    script_path = SCRIPTS_DIR / script_name
    return subprocess.run(
        ['bash', str(script_path)],
        input=stdin,
        env=base_env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )
