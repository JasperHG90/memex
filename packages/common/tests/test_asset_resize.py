"""Tests for memex_common.asset_resize.resize_image."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from PIL import Image

from memex_common.asset_resize import resize_image


def _write_png(path: Path, size: tuple[int, int] = (2000, 2000)) -> int:
    img = Image.new('RGB', size, color=(120, 200, 80))
    img.save(path, format='PNG')
    return path.stat().st_size


def test_resize_writes_smaller_file(tmp_path: Path) -> None:
    src = tmp_path / 'big.png'
    src_size = _write_png(src, size=(2000, 2000))

    dest, dest_size = resize_image(src, max_width=256, max_height=256)

    assert dest.exists()
    assert dest != src
    assert dest.parent == src.parent
    assert dest_size < src_size
    with Image.open(dest) as out:
        assert max(out.size) <= 256


def test_resize_preserves_aspect_ratio(tmp_path: Path) -> None:
    src = tmp_path / 'rect.png'
    Image.new('RGB', (1000, 500), color=(0, 0, 0)).save(src, format='PNG')

    dest, _ = resize_image(src, max_width=200, max_height=200)

    with Image.open(dest) as out:
        # Aspect ratio (2:1) must be preserved within rounding.
        assert out.size[0] == 200
        assert out.size[1] == 100


def test_resize_default_dimensions() -> None:
    sig = inspect.signature(resize_image)
    assert sig.parameters['max_width'].default == 1280
    assert sig.parameters['max_height'].default == 1280
    assert sig.parameters['format'].default is None


def test_resize_rejects_svg(tmp_path: Path) -> None:
    src = tmp_path / 'icon.svg'
    src.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    with pytest.raises(ValueError, match='Unsupported image suffix'):
        resize_image(src)


def test_resize_rejects_pdf(tmp_path: Path) -> None:
    src = tmp_path / 'doc.pdf'
    src.write_bytes(b'%PDF-1.4 fake')

    with pytest.raises(ValueError, match='Unsupported image suffix'):
        resize_image(src)


def test_resize_rejects_wav(tmp_path: Path) -> None:
    src = tmp_path / 'sound.wav'
    src.write_bytes(b'RIFF....WAVEfmt ')

    with pytest.raises(ValueError, match='Unsupported image suffix'):
        resize_image(src)


def test_resize_rejects_disguised_format(tmp_path: Path) -> None:
    """A file with a .png suffix but not actually a decodable image is rejected."""
    src = tmp_path / 'fake.png'
    src.write_bytes(b'not really a png')

    with pytest.raises(ValueError):
        resize_image(src)


def test_resize_with_explicit_format(tmp_path: Path) -> None:
    src = tmp_path / 'big.png'
    _write_png(src, size=(800, 800))

    dest, _ = resize_image(src, max_width=200, max_height=200, format='JPEG')

    # Suffix must reflect the actual bytes — a JPEG override on a .png
    # source must not leave the dest with a .png extension.
    assert dest.suffix.lower() in {'.jpg', '.jpeg'}
    with Image.open(dest) as out:
        assert out.format == 'JPEG'


def test_resize_explicit_format_webp(tmp_path: Path) -> None:
    src = tmp_path / 'photo.png'
    _write_png(src, size=(800, 800))

    dest, _ = resize_image(src, max_width=200, max_height=200, format='WEBP')

    assert dest.suffix.lower() == '.webp'
    with Image.open(dest) as out:
        assert out.format == 'WEBP'


def test_resize_explicit_format_jpg_alias(tmp_path: Path) -> None:
    src = tmp_path / 'photo.png'
    _write_png(src, size=(800, 800))

    # ``'JPG'`` is a common-but-non-canonical alias and must canonicalize
    # to ``.jpg`` on disk and ``JPEG`` in the bytes.
    dest, _ = resize_image(src, max_width=200, max_height=200, format='JPG')

    assert dest.suffix.lower() in {'.jpg', '.jpeg'}
    with Image.open(dest) as out:
        assert out.format == 'JPEG'


def test_resize_jpeg_input(tmp_path: Path) -> None:
    src = tmp_path / 'photo.jpg'
    Image.new('RGB', (1500, 1500), color=(10, 20, 30)).save(src, format='JPEG')

    dest, dest_size = resize_image(src, max_width=300, max_height=300)

    assert dest.exists()
    assert dest_size > 0
    with Image.open(dest) as out:
        assert out.format == 'JPEG'
        assert max(out.size) <= 300
