"""Image resize helper used by MCP and Hermes plugin tools.

Reads ``local_path`` with Pillow, applies ``Image.thumbnail`` (preserving
aspect ratio), and writes the resized copy beside the source. Path
confinement is the caller's responsibility — this helper only enforces a
format allowlist so SVG/PDF/audio cannot smuggle through Pillow plugins.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:
    from memex_common.asset_cache import SessionAssetCache

_ALLOWED_INPUT_FORMATS: frozenset[str] = frozenset({'PNG', 'JPEG', 'WEBP', 'GIF'})

# Decompression-bomb cap, checked from the lazy ``Image.open`` header
# before pixel decode. Pillow's stock ``MAX_IMAGE_PIXELS`` only warns and
# its hard-error path is at 2× that; we want a deterministic refusal.
_MAX_DECODED_PIXELS: int = 32 * 1024 * 1024  # 32 megapixels

# Align Pillow's own threshold so its DecompressionBombError fires at the
# same point as our explicit pre-check.
Image.MAX_IMAGE_PIXELS = _MAX_DECODED_PIXELS

_SUFFIX_TO_FORMAT: dict[str, str] = {
    '.png': 'PNG',
    '.jpg': 'JPEG',
    '.jpeg': 'JPEG',
    '.webp': 'WEBP',
    '.gif': 'GIF',
}

_FORMAT_TO_SUFFIX: dict[str, str] = {
    'PNG': '.png',
    'JPEG': '.jpg',
    'WEBP': '.webp',
    'GIF': '.gif',
}


def _allowed_formats_message() -> str:
    return 'allowed: ' + ', '.join(sorted(_ALLOWED_INPUT_FORMATS))


def resize_image(
    local_path: Path,
    *,
    max_width: int = 1280,
    max_height: int = 1280,
    output_format: str | None = None,
) -> tuple[Path, int]:
    """Resize ``local_path`` to fit within ``max_width`` x ``max_height``.

    Returns ``(dest_path, dest_size_bytes)``. The destination is written as a
    sibling of the source with a ``-{w}x{h}`` stem suffix and the same (or
    explicitly-overridden) format. Rejects any source whose suffix or
    Pillow-detected format is not in the allowlist.
    """
    if max_width <= 0 or max_height <= 0:
        raise ValueError('max_width and max_height must be positive')

    suffix = local_path.suffix.lower()
    suffix_format = _SUFFIX_TO_FORMAT.get(suffix)
    if suffix_format is None:
        raise ValueError(f'Unsupported image suffix {suffix!r}; {_allowed_formats_message()}')

    try:
        with Image.open(local_path) as img:
            detected = (img.format or '').upper()
            if detected not in _ALLOWED_INPUT_FORMATS:
                raise ValueError(
                    f'Unsupported image format {detected!r}; {_allowed_formats_message()}'
                )

            width, height = img.size
            if width * height > _MAX_DECODED_PIXELS:
                raise ValueError('Image is too large to safely decode')

            resolved_format = (output_format or detected).upper()
            if resolved_format == 'JPG':
                resolved_format = 'JPEG'
            if resolved_format not in _FORMAT_TO_SUFFIX:
                raise ValueError(
                    f'Unsupported output format {resolved_format!r}; {_allowed_formats_message()}'
                )

            img.thumbnail((max_width, max_height))
            dest_suffix = _FORMAT_TO_SUFFIX[resolved_format]
            dest_path = local_path.with_name(
                f'{local_path.stem}-{max_width}x{max_height}{dest_suffix}'
            )

            save_img: Image.Image = img
            if resolved_format == 'JPEG' and img.mode not in ('RGB', 'L'):
                save_img = img.convert('RGB')

            save_img.save(dest_path, format=resolved_format)
    except UnidentifiedImageError as exc:
        raise ValueError(
            f'Could not decode image at {local_path}; {_allowed_formats_message()}'
        ) from exc

    try:
        size = dest_path.stat().st_size
    except OSError as exc:
        raise ValueError(f'Resize destination is unavailable: {exc}') from exc
    return dest_path, size


def validate_and_resize(
    cache: SessionAssetCache,
    local_path_str: str,
    *,
    max_width: int,
    max_height: int,
    output_format: str | None,
) -> tuple[Path, int]:
    """Resize an image confined to ``cache.tempdir``.

    Resolves ``local_path_str`` strictly, refuses anything outside the
    cache, runs :func:`resize_image`, re-checks the destination resolves
    inside the cache (TOCTOU defense), and registers the result so it
    participates in LRU eviction. Raises :class:`ValueError` on any
    rejection — callers translate to their own error type.
    """
    if max_width <= 0 or max_height <= 0:
        raise ValueError('max_width and max_height must be positive')

    try:
        cache_root = cache.tempdir.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f'Asset cache tempdir is unavailable: {exc}') from exc

    try:
        resolved_input = Path(local_path_str).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f'local_path does not exist: {local_path_str}') from exc
    except OSError as exc:
        raise ValueError(f'Could not resolve local_path: {exc}') from exc

    if not resolved_input.is_relative_to(cache_root):
        raise ValueError('local_path must point inside the session asset cache')

    dest_path, size = resize_image(
        resolved_input,
        max_width=max_width,
        max_height=max_height,
        output_format=output_format,
    )

    try:
        resolved_dest = dest_path.resolve(strict=True)
    except OSError as exc:
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(dest_path)
        raise ValueError('Resize destination escaped session cache') from exc

    if not resolved_dest.is_relative_to(cache_root):
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(dest_path)
        raise ValueError('Resize destination escaped session cache')

    cache.register(dest_path)
    return dest_path, size
