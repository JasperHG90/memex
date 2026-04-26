"""Image resize helper used by MCP and Hermes plugin tools.

Reads ``local_path`` with Pillow, applies ``Image.thumbnail`` (preserving
aspect ratio), and writes the resized copy beside the source. Path
confinement is the caller's responsibility — this helper only enforces a
format allowlist so SVG/PDF/audio cannot smuggle through Pillow plugins.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

_ALLOWED_INPUT_FORMATS: frozenset[str] = frozenset({'PNG', 'JPEG', 'WEBP', 'GIF'})

# Decompression-bomb cap. Pillow's stock ``MAX_IMAGE_PIXELS`` (~89 MP) only
# emits a warning, and its hard-error path is at 2× that. We enforce a
# stricter, deterministic 32 MP budget by checking ``img.size`` after
# ``Image.open`` (which reads only the header) and before ``thumbnail()``
# triggers the actual decode. No process-global Pillow state is mutated, so
# concurrent callers don't race and unrelated Pillow users elsewhere in the
# process are unaffected.
_MAX_DECODED_PIXELS: int = 32 * 1024 * 1024  # 32 megapixels

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

            # ``Image.open`` is lazy — only the header is read, so ``img.size``
            # is reliable before any pixels are decoded. Reject oversize
            # inputs here so a pathological 100k×100k PNG can't exhaust
            # memory inside ``thumbnail()``.
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

    return dest_path, dest_path.stat().st_size
