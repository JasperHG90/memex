"""Image resize helper used by MCP and Hermes plugin tools.

Reads ``local_path`` with Pillow, applies ``Image.thumbnail`` (preserving
aspect ratio), and writes the resized copy beside the source. Path
confinement is the caller's responsibility — this helper only enforces a
format allowlist so SVG/PDF/audio cannot smuggle through Pillow plugins.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

_ALLOWED_INPUT_FORMATS: frozenset[str] = frozenset({'PNG', 'JPEG', 'JPG', 'WEBP', 'GIF'})

_SUFFIX_TO_FORMAT: dict[str, str] = {
    '.png': 'PNG',
    '.jpg': 'JPEG',
    '.jpeg': 'JPEG',
    '.webp': 'WEBP',
    '.gif': 'GIF',
}


def _allowed_formats_message() -> str:
    return 'allowed: ' + ', '.join(sorted(_ALLOWED_INPUT_FORMATS))


def resize_image(
    local_path: Path,
    *,
    max_width: int = 1280,
    max_height: int = 1280,
    format: str | None = None,
) -> tuple[Path, int]:
    """Resize ``local_path`` to fit within ``max_width`` x ``max_height``.

    Returns ``(dest_path, dest_size_bytes)``. The destination is written as a
    sibling of the source with a ``-{w}x{h}`` stem suffix and the same (or
    explicitly-overridden) format. Rejects any source whose suffix or
    Pillow-detected format is not in the allowlist.
    """
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

            output_format = (format or detected).upper()
            if output_format == 'JPG':
                output_format = 'JPEG'

            img.thumbnail((max_width, max_height))
            dest_path = local_path.with_stem(f'{local_path.stem}-{max_width}x{max_height}')

            save_img: Image.Image = img
            if output_format == 'JPEG' and img.mode not in ('RGB', 'L'):
                save_img = img.convert('RGB')

            save_img.save(dest_path, format=output_format)
    except UnidentifiedImageError as exc:
        raise ValueError(
            f'Could not decode image at {local_path}; {_allowed_formats_message()}'
        ) from exc

    return dest_path, dest_path.stat().st_size
