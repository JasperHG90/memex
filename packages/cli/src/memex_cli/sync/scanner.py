from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import unquote

from pydantic import BaseModel, Field

from .config import AssetConfig, ExcludeConfig

DEFAULT_ASSET_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', '.webp'}

# YAML frontmatter block
_FRONTMATTER_RE = re.compile(r'\A---\s*\n(.*?)\n---', re.DOTALL)

# Obsidian wiki-link embeds: ![[filename.png]] or ![[path/to/file.png]]
_WIKILINK_EMBED_RE = re.compile(r'!\[\[([^\]|]+?)(?:\|[^\]]*?)?\]\]')

# Standard markdown images: ![alt](path/to/image.png) or ![alt](path/to/image.png "title")
_MD_IMAGE_RE = re.compile(r'!\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')


class VaultAsset(BaseModel):
    """An asset file (image, PDF, etc.) referenced by an Obsidian note."""

    model_config = {'arbitrary_types_allowed': True}

    path: Path = Field(description='Absolute path to the asset file.')
    relative_path: str = Field(description='Path relative to the vault root.')
    size: int = Field(description='File size in bytes.')


class VaultNote(BaseModel):
    """A Markdown note discovered in an Obsidian vault."""

    model_config = {'arbitrary_types_allowed': True}

    path: Path = Field(description='Absolute path to the .md file.')
    relative_path: str = Field(description='Path relative to the vault root.')
    mtime: float = Field(description='File modification time (Unix timestamp).')
    size: int = Field(description='File size in bytes.')
    assets: list[VaultAsset] = Field(
        default_factory=list,
        description='Assets (images, PDFs, etc.) referenced by this note.',
    )


def _is_excluded(path: Path, vault_path: Path, exclude: ExcludeConfig) -> bool:
    """Check if a path should be excluded based on configured patterns."""
    rel = path.relative_to(vault_path)
    rel_str = str(rel)

    # Check ignore_folders — exact folder name match at any depth
    if exclude.ignore_folders:
        for part in rel.parent.parts:
            if part in exclude.ignore_folders:
                return True

    for pattern in exclude.all_patterns:
        # Match directory names at any level
        for part in rel.parts:
            if fnmatch(part, pattern):
                return True
        # Match full relative path
        if fnmatch(rel_str, pattern):
            return True
    return False


def _should_skip_frontmatter(content: str, exclude: ExcludeConfig) -> bool:
    """Check if a note's frontmatter contains the skip marker.

    Returns True if the frontmatter has the configured skip key set to the
    skip value. For example, with key="agents" and value="skip":

        ---
        agents: skip
        ---

    The check is case-insensitive on the value.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return False

    fm_block = m.group(1)
    key = exclude.frontmatter_skip_key
    skip_val = exclude.frontmatter_skip_value.lower()

    for line in fm_block.splitlines():
        line = line.strip()
        if ':' not in line:
            continue
        k, _, v = line.partition(':')
        if k.strip() == key and v.strip().strip('"\'').lower() == skip_val:
            return True

    return False


def _allowed_asset_extensions(asset_config: AssetConfig) -> set[str]:
    """Get the full set of allowed asset extensions."""
    extensions = set(DEFAULT_ASSET_EXTENSIONS)
    for ext in asset_config.extends_include:
        extensions.add(ext if ext.startswith('.') else f'.{ext}')
    return extensions


def resolve_assets(
    note_path: Path,
    vault_path: Path,
    asset_config: AssetConfig,
) -> list[VaultAsset]:
    """Parse note for embedded asset references and resolve to file paths.

    Handles:
    - Obsidian wiki-link embeds: ![[image.png]]
    - Standard markdown images: ![alt](path/to/image.png)
    """
    if not asset_config.enabled:
        return []

    allowed_extensions = _allowed_asset_extensions(asset_config)
    max_bytes = asset_config.max_size_mb * 1024 * 1024
    content = note_path.read_text(encoding='utf-8', errors='replace')

    refs: list[str] = []
    refs.extend(_WIKILINK_EMBED_RE.findall(content))
    refs.extend(_MD_IMAGE_RE.findall(content))

    seen: set[str] = set()
    assets: list[VaultAsset] = []

    for ref in refs:
        ref = unquote(ref).strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)

        # Skip URLs
        if ref.startswith(('http://', 'https://', 'data:')):
            continue

        suffix = Path(ref).suffix.lower()
        if suffix not in allowed_extensions:
            continue

        # Try resolving: relative to note, then relative to vault root
        candidates = [
            note_path.parent / ref,
            vault_path / ref,
        ]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_file() and resolved.is_relative_to(vault_path.resolve()):
                size = resolved.stat().st_size
                if size > max_bytes:
                    continue
                rel_path = str(resolved.relative_to(vault_path.resolve()))
                assets.append(
                    VaultAsset(
                        path=resolved,
                        relative_path=rel_path,
                        size=size,
                    )
                )
                break

    return assets


def scan_vault(
    vault_path: Path,
    exclude: ExcludeConfig,
    asset_config: AssetConfig,
    include_extensions: list[str] | None = None,
) -> list[VaultNote]:
    """Scan a vault for notes matching configured file extensions.

    Args:
        vault_path: Root directory to scan.
        exclude: Exclusion rules (patterns, frontmatter skip).
        asset_config: Asset resolution settings (only used for .md files).
        include_extensions: File extensions to include (e.g. ['.md', '.pdf']).
            Defaults to ['.md'] when not provided.
    """
    vault_path = vault_path.resolve()
    if not vault_path.is_dir():
        raise FileNotFoundError(f'Vault path does not exist: {vault_path}')

    if include_extensions is None:
        include_extensions = ['.md']
    ext_set = {ext.lower() for ext in include_extensions}

    # Collect matching files across all extensions
    matched_files: set[Path] = set()
    for ext in ext_set:
        pattern = f'*{ext}'
        matched_files.update(vault_path.rglob(pattern))

    notes: list[VaultNote] = []
    for file_path in sorted(matched_files):
        if _is_excluded(file_path, vault_path, exclude):
            continue

        is_markdown = file_path.suffix.lower() == '.md'

        # Check frontmatter skip marker (only for markdown files)
        if is_markdown:
            content = file_path.read_text(encoding='utf-8', errors='replace')
            if _should_skip_frontmatter(content, exclude):
                continue

        stat = file_path.stat()
        rel_path = str(file_path.relative_to(vault_path))

        # Only resolve assets for markdown files (binary files have no wiki-links)
        assets = resolve_assets(file_path, vault_path, asset_config) if is_markdown else []

        notes.append(
            VaultNote(
                path=file_path,
                relative_path=rel_path,
                mtime=stat.st_mtime,
                size=stat.st_size,
                assets=assets,
            )
        )

    return notes
