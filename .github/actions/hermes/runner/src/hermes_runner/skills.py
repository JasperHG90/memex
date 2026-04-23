"""Copy a directory of skills into the location Hermes auto-discovers."""

from __future__ import annotations

import shutil
from pathlib import Path


def copy_skills(src: Path, dst: Path) -> int:
    """Copy each subdirectory of ``src`` into ``dst``. Returns the count copied."""
    if not src.exists() or not src.is_dir():
        return 0

    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in sorted(src.iterdir()):
        if not entry.is_dir():
            continue
        target = dst / entry.name
        shutil.copytree(entry, target, dirs_exist_ok=True)
        copied += 1
    return copied
