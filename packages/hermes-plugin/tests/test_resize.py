"""Tests for the ``memex_resize_image`` tool handler.

Resize lives in ``memex_common.asset_resize`` (covered by its own suite); the
Hermes-side tests here focus on the tool plumbing — argument validation,
path-confinement to the session asset cache, and the JSON response shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from memex_common.asset_cache import SessionAssetCache
from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.tools import dispatch


@pytest.fixture
def config() -> HermesMemexConfig:
    return HermesMemexConfig()


@pytest.fixture
def vault_id():
    return uuid4()


@pytest.fixture
def asset_cache(tmp_path: Path):
    cache = SessionAssetCache(tempdir=tmp_path / 'cache')
    yield cache
    cache.cleanup()


def _write_png(path: Path, *, size: tuple[int, int] = (640, 480)) -> Path:
    """Write a deterministic PNG of the given size into ``path``."""
    img = Image.new('RGB', size, color=(255, 0, 0))
    img.save(path, format='PNG')
    return path


def test_resize_writes_smaller_file(config, vault_id, asset_cache, tmp_path):
    """Happy path: a 640x480 PNG inside the cache resized to 64x64 produces a
    sibling file with byte-size strictly less than the source."""
    src = _write_png(asset_cache.tempdir / 'sample.png', size=(640, 480))
    src_size = src.stat().st_size

    out = dispatch(
        'memex_resize_image',
        {'local_path': str(src), 'max_width': 64, 'max_height': 64},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' not in data
    dest = Path(data['local_path'])
    assert dest.exists()
    assert dest.parent == asset_cache.tempdir
    assert data['size_bytes'] == dest.stat().st_size
    assert dest.stat().st_size < src_size

    with Image.open(dest) as img:
        assert img.width <= 64 and img.height <= 64


def test_resize_rejects_path_outside_tempdir(config, vault_id, asset_cache):
    """AC-009: ``/etc/passwd`` is well outside the session tempdir → tool_error."""
    out = dispatch(
        'memex_resize_image',
        {'local_path': '/etc/passwd'},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'session asset cache' in data['error'].lower() or 'inside' in data['error'].lower()


def test_resize_rejects_path_via_relative_traversal(config, vault_id, asset_cache, tmp_path):
    """AC-009: a path like ``<tempdir>/../escape.png`` resolves outside the
    cache and must be rejected, even though the prefix matches before
    resolution."""
    # Drop a real PNG OUTSIDE the cache (sibling to the cache dir) so the
    # resolved target exists; otherwise the helper would short-circuit on
    # FileNotFoundError before the confinement check.
    escape_dir = tmp_path / 'escape'
    escape_dir.mkdir()
    _write_png(escape_dir / 'evil.png')
    sneaky = asset_cache.tempdir / '..' / 'escape' / 'evil.png'

    out = dispatch(
        'memex_resize_image',
        {'local_path': str(sneaky)},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'inside' in data['error'].lower() or 'cache' in data['error'].lower()


def test_resize_rejects_unsupported_format(config, vault_id, asset_cache):
    """AC-010: SVG (or any other non-allowlisted suffix) inside the cache is
    rejected with a clear error before Pillow is invoked."""
    svg_path = asset_cache.tempdir / 'diagram.svg'
    svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')

    out = dispatch(
        'memex_resize_image',
        {'local_path': str(svg_path)},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    assert '.svg' in data['error'].lower() or 'unsupported' in data['error'].lower()


def test_resize_rejects_missing_local_path(config, vault_id, asset_cache):
    """Argument validation: omitting ``local_path`` is a tool_error."""
    out = dispatch(
        'memex_resize_image',
        {},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'local_path' in data['error']


def test_resize_rejects_nonexistent_path(config, vault_id, asset_cache):
    """``Path.resolve(strict=True)`` on a missing file surfaces a clean
    tool_error rather than letting an OSError escape through dispatch."""
    out = dispatch(
        'memex_resize_image',
        {'local_path': str(asset_cache.tempdir / 'does-not-exist.png')},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'does not exist' in data['error'].lower() or 'not exist' in data['error'].lower()


def test_resize_without_cache_returns_error(config, vault_id):
    """Defensive: handler refuses to run when no cache is wired in."""
    out = dispatch(
        'memex_resize_image',
        {'local_path': '/tmp/whatever.png'},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=None,
    )
    data = json.loads(out)
    assert 'error' in data


def test_resize_rejects_negative_dimensions(config, vault_id, asset_cache):
    """Defensive: negative or zero width/height is a tool_error."""
    src = _write_png(asset_cache.tempdir / 'tiny.png', size=(32, 32))
    out = dispatch(
        'memex_resize_image',
        {'local_path': str(src), 'max_width': 0, 'max_height': 64},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'positive' in data['error'].lower() or 'must be' in data['error'].lower()


def test_resize_registers_dest_in_cache(config, vault_id, asset_cache, tmp_path):
    """Finding 4: a successful resize must insert the destination into the
    LRU so the resized sibling participates in eviction and session cleanup
    (otherwise it leaks until the session tempdir is torn down)."""
    src = _write_png(asset_cache.tempdir / 'sample.png', size=(640, 480))

    out = dispatch(
        'memex_resize_image',
        {'local_path': str(src), 'max_width': 64, 'max_height': 64},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' not in data
    dest = Path(data['local_path'])
    assert str(dest) in asset_cache


def test_resize_rejects_dest_path_escape(monkeypatch, config, vault_id, asset_cache, tmp_path):
    """Finding 10: defense-in-depth post-resize TOCTOU check. If the
    destination resolves outside the cache root (e.g. via a symlink swap
    while ``resize_image`` was running), the handler must unlink the file
    and return a ``tool_error``."""
    src = _write_png(asset_cache.tempdir / 'sample.png', size=(640, 480))

    # Anchor a fake target outside the cache root that ``resolve`` will
    # return in place of the real destination's resolution. The decoy needs
    # to exist so ``resolve(strict=True)`` does not raise.
    escape_dir = tmp_path / 'escape'
    escape_dir.mkdir()
    decoy = escape_dir / 'escaped.png'
    _write_png(decoy)

    real_resolve = Path.resolve

    def fake_resolve(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Only redirect resolution for paths produced by ``resize_image`` —
        # those land in the cache tempdir with the deterministic
        # ``-{w}x{h}.<suffix>`` stem. Everything else (the cache root, the
        # input path) keeps its real resolution so the input-side checks
        # still pass.
        if self.parent == asset_cache.tempdir and '-64x64' in self.name:
            return decoy
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', fake_resolve)

    # Capture the dest path the handler will actually unlink (the real
    # in-cache file) so we can assert the cleanup branch ran.
    expected_dest = asset_cache.tempdir / f'{src.stem}-64x64.png'

    out = dispatch(
        'memex_resize_image',
        {'local_path': str(src), 'max_width': 64, 'max_height': 64},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'escaped' in data['error'].lower() or 'cache' in data['error'].lower()
    # The handler unlinks ``dest_path`` (the original, in-cache target) —
    # not the resolved decoy — so absence of the in-cache file proves the
    # cleanup branch ran.
    assert not expected_dest.exists()


def test_resize_accepts_output_format_kwarg(config, vault_id, asset_cache):
    """Finding 11: the renamed kwarg flows through to ``resize_image`` and
    yields a destination with the requested suffix."""
    src = _write_png(asset_cache.tempdir / 'sample.png', size=(128, 128))

    out = dispatch(
        'memex_resize_image',
        {
            'local_path': str(src),
            'max_width': 64,
            'max_height': 64,
            'output_format': 'JPEG',
        },
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' not in data
    assert Path(data['local_path']).suffix == '.jpg'


def test_resize_rejects_decompression_bomb(monkeypatch, config, vault_id, asset_cache):
    """An image whose pixel count exceeds the shared ``_MAX_DECODED_PIXELS``
    budget must be rejected before any pixels are decoded. The helper raises
    ``ValueError`` and the handler maps that to a ``tool_error``."""
    # 200x200 = 40k pixels, well above the patched cap below.
    src = _write_png(asset_cache.tempdir / 'huge.png', size=(200, 200))

    monkeypatch.setattr(
        'memex_common.asset_resize._MAX_DECODED_PIXELS',
        1000,
    )

    out = dispatch(
        'memex_resize_image',
        {'local_path': str(src), 'max_width': 64, 'max_height': 64},
        api=None,  # type: ignore[arg-type]
        config=config,
        vault_id=vault_id,
        asset_cache=asset_cache,
    )
    data = json.loads(out)
    assert 'error' in data
    err = data['error'].lower()
    assert 'too large to safely decode' in err
