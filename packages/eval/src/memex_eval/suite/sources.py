"""Source-note loading from disk + content hashing.

Each suite ships markdown notes under ``sources/*.md``; optional
frontmatter populates per-note metadata. The whole tree is
content-hashed so MLflow can detect dataset drift.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import frontmatter
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger('memex_eval.suite.sources')

# Filename-stem convention shared by SourceNote (the markdown file's stem)
# and InlineNote.note_key. Allows digit-leading stems (e.g.
# ``2023-historical.md``) — many real-world suites date-prefix sources.
NOTE_KEY_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


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
            # Underscore-prefixed files (e.g. ``_shared.md``) are intentional
            # convention for "internal" markdown that ships in sources/ but
            # isn't itself a note (templates, fixtures, partials).
            if note_key.startswith('_'):
                logger.debug('Skipping underscore-prefixed file %s', md_path)
                continue
            # Filenames that don't match the note-key shape (e.g. ``README.md``,
            # capitalized titles, names with spaces) are skipped with a
            # warning rather than raised. People drop READMEs into sources/
            # by reflex; we shouldn't take down the whole suite for that.
            if not NOTE_KEY_RE.match(note_key):
                logger.warning(
                    'Skipping %s: stem %r does not match %r '
                    '(rename to lowercase + hyphens/underscores or move out of sources/)',
                    md_path,
                    note_key,
                    NOTE_KEY_RE.pattern,
                )
                continue
            if note_key in seen:
                raise ValueError(
                    f'Duplicate note_key "{note_key}" in {sources_dir}; filename stems must be unique.'
                )
            seen.add(note_key)

            post = frontmatter.load(md_path)
            metadata: dict[str, Any] = dict(post.metadata or {})
            content = post.content

            assets_field = metadata.get('assets')
            resolved_assets: dict[str, Path] = {}
            if isinstance(assets_field, dict):
                # Explicit per-note frontmatter binding wins.
                resolved_assets = {
                    name: (sources_dir / rel).resolve() for name, rel in assets_field.items()
                }
            elif assets_dir.is_dir():
                # Per-note subdirectory convention: sources/assets/<note-key>/*
                # attaches only to <note-key>.md. Files placed directly under
                # sources/assets/ (no per-note subdir) are NOT auto-attached;
                # surface them via frontmatter or move them into a per-note dir.
                per_note_dir = assets_dir / note_key
                if per_note_dir.is_dir():
                    for asset_path in sorted(per_note_dir.iterdir()):
                        # Refuse symlinks: a symlink under sources/assets/<key>/
                        # could point anywhere on the filesystem (e.g.
                        # /etc/passwd) and we'd silently upload its bytes as
                        # part of the run artifact.
                        if asset_path.is_symlink():
                            logger.warning(
                                'Refusing symlinked asset %s; only regular files '
                                'are auto-attached.',
                                asset_path,
                            )
                            continue
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

        # Surface unreferenced bare files in sources/assets/ — they aren't
        # attached to anything. Help the user understand why their image
        # didn't appear in the eval run.
        if assets_dir.is_dir():
            note_keys_seen = {n.note_key for n in notes}
            stragglers: list[str] = []
            for entry in sorted(assets_dir.iterdir()):
                if entry.is_file():
                    stragglers.append(entry.name)
                elif entry.is_dir() and entry.name not in note_keys_seen:
                    stragglers.append(f'{entry.name}/ (no matching <note-key>.md)')
            if stragglers:
                logger.warning(
                    'Unattached files/dirs in %s: %s. Per-note convention is '
                    'sources/assets/<note-key>/*; bare files at sources/assets/* '
                    'are NOT auto-attached. Use frontmatter `assets:` to bind '
                    'an asset to a specific note, or move it under '
                    'sources/assets/<note-key>/.',
                    assets_dir,
                    ', '.join(stragglers),
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
