"""Source-note loading from disk + content hashing.

Each suite ships markdown notes under ``sources/*.md``; optional
frontmatter populates per-note metadata. The whole tree is
content-hashed so MLflow can detect dataset drift.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import frontmatter
from pydantic import BaseModel, ConfigDict, Field


class SourceNote(BaseModel):
    """A markdown note (loaded from sources/<name>.md)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    note_key: str  # filename stem; stable id for cross-references
    content: str
    title: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    assets: dict[str, Path] = Field(default_factory=dict)
    vault_name: str | None = None

    def asset_bytes_b64(self) -> dict[str, bytes]:
        """Read every asset from disk and base64-encode for the wire format
        expected by ``NoteCreateDTO.files``."""
        return {name: base64.b64encode(p.read_bytes()) for name, p in self.assets.items()}


class SuiteSources(BaseModel):
    notes: list[SourceNote] = Field(default_factory=list)

    @classmethod
    def from_directory(cls, sources_dir: Path) -> SuiteSources:
        """Load every ``*.md`` from ``sources_dir`` (non-recursive).

        Frontmatter (YAML between ``---`` markers) is parsed into the
        SourceNote fields ``vault_name``, ``tags``, ``description``,
        ``title``, ``assets``. Body becomes ``content``. Filename stem
        becomes ``note_key``. Sibling ``assets/`` directory contents are
        auto-attached if no explicit ``assets`` key is set.

        Raises ValueError on duplicate note_keys.
        """
        sources_dir = Path(sources_dir)
        if not sources_dir.is_dir():
            return cls(notes=[])

        notes: list[SourceNote] = []
        seen: set[str] = set()
        assets_dir = sources_dir / 'assets'

        for md_path in sorted(sources_dir.glob('*.md')):
            note_key = md_path.stem
            if note_key in seen:
                raise ValueError(
                    f'Duplicate note_key "{note_key}" in {sources_dir}; filename stems must be unique.'
                )
            seen.add(note_key)

            post = frontmatter.load(md_path)
            metadata: dict[str, Any] = dict(post.metadata or {})
            content = post.content

            assets_field = metadata.get('assets')
            if isinstance(assets_field, dict):
                resolved_assets = {
                    name: (sources_dir / rel).resolve() for name, rel in assets_field.items()
                }
            else:
                # Auto-attach: if a sibling directory `assets/` exists, attach all
                # files in it (referenced by basename).
                resolved_assets = {}
                if assets_dir.is_dir():
                    for asset_path in sorted(assets_dir.iterdir()):
                        if asset_path.is_file():
                            resolved_assets[asset_path.name] = asset_path.resolve()

            raw_tags = metadata.get('tags') or []
            tags = [str(t) for t in raw_tags]
            notes.append(
                SourceNote(
                    path=md_path.resolve(),
                    note_key=note_key,
                    content=content,
                    title=metadata.get('title'),
                    description=metadata.get('description'),
                    tags=tags,
                    assets=resolved_assets,
                    vault_name=metadata.get('vault_name'),
                )
            )

        return cls(notes=notes)

    def content_hash(self) -> str:
        """sha256 over sorted [(note_key, sha256(content))] + sorted [(asset_name, sha256(bytes))]."""
        h = hashlib.sha256()
        for note in sorted(self.notes, key=lambda n: n.note_key):
            content_hash = hashlib.sha256(note.content.encode('utf-8')).hexdigest()
            h.update(f'{note.note_key}:{content_hash}\n'.encode())
            for asset_name in sorted(note.assets):
                asset_hash = hashlib.sha256(note.assets[asset_name].read_bytes()).hexdigest()
                h.update(f'  {asset_name}:{asset_hash}\n'.encode())
        return h.hexdigest()

    def get(self, note_key: str) -> SourceNote | None:
        for n in self.notes:
            if n.note_key == note_key:
                return n
        return None

    @property
    def note_keys(self) -> set[str]:
        return {n.note_key for n in self.notes}


__all__ = ['SourceNote', 'SuiteSources']
