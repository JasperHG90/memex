from __future__ import annotations

from pathlib import Path

import pytest

from memex_obsidian_sync.config import AssetConfig, ExcludeConfig
from memex_obsidian_sync.scanner import (
    DEFAULT_ASSET_EXTENSIONS,
    _allowed_asset_extensions,
    _is_excluded,
    _should_skip_frontmatter,
    resolve_assets,
    scan_vault,
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Create a minimal Obsidian vault structure."""
    # Notes
    (tmp_path / 'note1.md').write_text('# Note 1\nSome content')
    (tmp_path / 'subfolder').mkdir()
    (tmp_path / 'subfolder' / 'note2.md').write_text('# Note 2\nMore content')

    # Obsidian internals (should be excluded)
    (tmp_path / '.obsidian').mkdir()
    (tmp_path / '.obsidian' / 'config.json').write_text('{}')
    (tmp_path / '.obsidian' / 'notes.md').write_text('should be excluded')

    # Trash (should be excluded)
    (tmp_path / '.trash').mkdir()
    (tmp_path / '.trash' / 'deleted.md').write_text('deleted note')

    return tmp_path


@pytest.fixture
def vault_with_assets(vault: Path) -> Path:
    """Vault with image and PDF assets."""
    # Create assets
    (vault / 'images').mkdir()
    (vault / 'images' / 'photo.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 100)
    (vault / 'images' / 'diagram.svg').write_text('<svg></svg>')
    (vault / 'doc.pdf').write_bytes(b'%PDF-1.4' + b'\x00' * 100)

    # Note with wiki-link embeds
    (vault / 'note_with_assets.md').write_text(
        '# Note with assets\n![[images/photo.png]]\nSome text\n![[doc.pdf]]\n'
    )

    # Note with standard markdown images
    (vault / 'note_md_images.md').write_text(
        '# Note with markdown images\n'
        '![A photo](images/photo.png)\n'
        '![Diagram](images/diagram.svg "My diagram")\n'
    )

    # Note with external URL (should be ignored)
    (vault / 'note_external.md').write_text(
        '# External\n![External](https://example.com/image.png)\n'
    )

    return vault


class TestIsExcluded:
    def test_excludes_obsidian_dir(self, vault: Path) -> None:
        exclude = ExcludeConfig()
        assert _is_excluded(vault / '.obsidian' / 'notes.md', vault, exclude) is True

    def test_excludes_trash(self, vault: Path) -> None:
        exclude = ExcludeConfig()
        assert _is_excluded(vault / '.trash' / 'deleted.md', vault, exclude) is True

    def test_allows_normal_files(self, vault: Path) -> None:
        exclude = ExcludeConfig()
        assert _is_excluded(vault / 'note1.md', vault, exclude) is False
        assert _is_excluded(vault / 'subfolder' / 'note2.md', vault, exclude) is False

    def test_extends_exclude(self, vault: Path) -> None:
        (vault / 'templates').mkdir()
        (vault / 'templates' / 'daily.md').write_text('template')

        exclude = ExcludeConfig(extends_exclude=['templates'])
        assert _is_excluded(vault / 'templates' / 'daily.md', vault, exclude) is True

    def test_glob_pattern_exclude(self, vault: Path) -> None:
        (vault / 'archive').mkdir()
        (vault / 'archive' / 'old.md').write_text('old')

        exclude = ExcludeConfig(extends_exclude=['archive/*'])
        assert _is_excluded(vault / 'archive' / 'old.md', vault, exclude) is True


class TestAllowedAssetExtensions:
    def test_defaults(self) -> None:
        config = AssetConfig()
        result = _allowed_asset_extensions(config)
        assert result == DEFAULT_ASSET_EXTENSIONS

    def test_extends_include(self) -> None:
        config = AssetConfig(extends_include=['.mp3', 'wav'])
        result = _allowed_asset_extensions(config)
        assert '.mp3' in result
        assert '.wav' in result
        assert '.png' in result  # defaults still present


class TestResolveAssets:
    def test_wikilink_embeds(self, vault_with_assets: Path) -> None:
        note_path = vault_with_assets / 'note_with_assets.md'
        assets = resolve_assets(note_path, vault_with_assets, AssetConfig())

        rel_paths = {a.relative_path for a in assets}
        assert 'images/photo.png' in rel_paths
        assert 'doc.pdf' in rel_paths

    def test_markdown_images(self, vault_with_assets: Path) -> None:
        note_path = vault_with_assets / 'note_md_images.md'
        assets = resolve_assets(note_path, vault_with_assets, AssetConfig())

        rel_paths = {a.relative_path for a in assets}
        assert 'images/photo.png' in rel_paths
        assert 'images/diagram.svg' in rel_paths

    def test_skips_external_urls(self, vault_with_assets: Path) -> None:
        note_path = vault_with_assets / 'note_external.md'
        assets = resolve_assets(note_path, vault_with_assets, AssetConfig())
        assert len(assets) == 0

    def test_skips_when_disabled(self, vault_with_assets: Path) -> None:
        note_path = vault_with_assets / 'note_with_assets.md'
        assets = resolve_assets(note_path, vault_with_assets, AssetConfig(enabled=False))
        assert len(assets) == 0

    def test_respects_max_size(self, vault_with_assets: Path) -> None:
        note_path = vault_with_assets / 'note_with_assets.md'
        # Set max to 0 MB — should skip everything
        assets = resolve_assets(note_path, vault_with_assets, AssetConfig(max_size_mb=0))
        assert len(assets) == 0

    def test_filters_by_extension(self, vault_with_assets: Path) -> None:
        # Create a .txt file and reference it
        (vault_with_assets / 'data.txt').write_text('hello')
        (vault_with_assets / 'note_txt.md').write_text('![[data.txt]]')

        note_path = vault_with_assets / 'note_txt.md'
        assets = resolve_assets(note_path, vault_with_assets, AssetConfig())
        # .txt not in default extensions
        assert len(assets) == 0

    def test_deduplicates(self, vault_with_assets: Path) -> None:
        # Note referencing same asset twice
        (vault_with_assets / 'dupe.md').write_text('![[images/photo.png]]\n![](images/photo.png)\n')
        note_path = vault_with_assets / 'dupe.md'
        assets = resolve_assets(note_path, vault_with_assets, AssetConfig())
        assert len(assets) == 1


class TestScanVault:
    def test_finds_all_notes(self, vault: Path) -> None:
        notes = scan_vault(vault, ExcludeConfig(), AssetConfig())
        rel_paths = {n.relative_path for n in notes}
        assert 'note1.md' in rel_paths
        assert 'subfolder/note2.md' in rel_paths

    def test_excludes_obsidian_and_trash(self, vault: Path) -> None:
        notes = scan_vault(vault, ExcludeConfig(), AssetConfig())
        rel_paths = {n.relative_path for n in notes}
        assert not any('.obsidian' in p for p in rel_paths)
        assert not any('.trash' in p for p in rel_paths)

    def test_resolves_assets(self, vault_with_assets: Path) -> None:
        notes = scan_vault(vault_with_assets, ExcludeConfig(), AssetConfig())
        asset_note = next(n for n in notes if n.relative_path == 'note_with_assets.md')
        assert len(asset_note.assets) >= 1

    def test_nonexistent_vault_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            scan_vault(tmp_path / 'no-such-dir', ExcludeConfig(), AssetConfig())

    def test_empty_vault(self, tmp_path: Path) -> None:
        notes = scan_vault(tmp_path, ExcludeConfig(), AssetConfig())
        assert notes == []

    def test_mtime_populated(self, vault: Path) -> None:
        notes = scan_vault(vault, ExcludeConfig(), AssetConfig())
        for note in notes:
            assert note.mtime > 0
            assert note.size > 0

    def test_ignore_folders(self, vault: Path) -> None:
        (vault / 'private').mkdir()
        (vault / 'private' / 'secret.md').write_text('# Secret')
        (vault / 'private' / 'nested').mkdir()
        (vault / 'private' / 'nested' / 'deep.md').write_text('# Deep secret')

        exclude = ExcludeConfig(ignore_folders=['private'])
        notes = scan_vault(vault, exclude, AssetConfig())
        rel_paths = {n.relative_path for n in notes}
        assert 'private/secret.md' not in rel_paths
        assert 'private/nested/deep.md' not in rel_paths
        # Other notes still present
        assert 'note1.md' in rel_paths

    def test_frontmatter_skip(self, vault: Path) -> None:
        (vault / 'skipped.md').write_text('---\nagents: skip\n---\n\n# Should be skipped\n')
        (vault / 'included.md').write_text('---\nagents: sync\n---\n\n# Should be included\n')

        notes = scan_vault(vault, ExcludeConfig(), AssetConfig())
        rel_paths = {n.relative_path for n in notes}
        assert 'skipped.md' not in rel_paths
        assert 'included.md' in rel_paths

    def test_frontmatter_skip_no_frontmatter(self, vault: Path) -> None:
        """Notes without frontmatter should not be skipped."""
        notes = scan_vault(vault, ExcludeConfig(), AssetConfig())
        assert len(notes) >= 2  # note1.md, subfolder/note2.md


class TestShouldSkipFrontmatter:
    def test_skip_marker_present(self) -> None:
        content = '---\ntitle: Test\nagents: skip\n---\n\n# Content'
        assert _should_skip_frontmatter(content, ExcludeConfig()) is True

    def test_skip_marker_absent(self) -> None:
        content = '---\ntitle: Test\n---\n\n# Content'
        assert _should_skip_frontmatter(content, ExcludeConfig()) is False

    def test_no_frontmatter(self) -> None:
        content = '# Just a heading\nSome content'
        assert _should_skip_frontmatter(content, ExcludeConfig()) is False

    def test_different_value_not_skipped(self) -> None:
        content = '---\nagents: sync\n---\n\n# Content'
        assert _should_skip_frontmatter(content, ExcludeConfig()) is False

    def test_case_insensitive_value(self) -> None:
        content = '---\nagents: SKIP\n---\n\n# Content'
        assert _should_skip_frontmatter(content, ExcludeConfig()) is True

    def test_quoted_value(self) -> None:
        content = '---\nagents: "skip"\n---\n\n# Content'
        assert _should_skip_frontmatter(content, ExcludeConfig()) is True

    def test_custom_key_value(self) -> None:
        content = '---\nmemex: ignore\n---\n\n# Content'
        exclude = ExcludeConfig(frontmatter_skip_key='memex', frontmatter_skip_value='ignore')
        assert _should_skip_frontmatter(content, exclude) is True

    def test_empty_file(self) -> None:
        assert _should_skip_frontmatter('', ExcludeConfig()) is False


class TestIgnoreFolders:
    def test_exact_folder_match(self, vault: Path) -> None:
        exclude = ExcludeConfig(ignore_folders=['subfolder'])
        assert _is_excluded(vault / 'subfolder' / 'note2.md', vault, exclude) is True

    def test_no_match(self, vault: Path) -> None:
        exclude = ExcludeConfig(ignore_folders=['nonexistent'])
        assert _is_excluded(vault / 'subfolder' / 'note2.md', vault, exclude) is False

    def test_root_file_not_affected(self, vault: Path) -> None:
        exclude = ExcludeConfig(ignore_folders=['subfolder'])
        assert _is_excluded(vault / 'note1.md', vault, exclude) is False

    def test_nested_folder(self, tmp_path: Path) -> None:
        (tmp_path / 'a' / 'private' / 'b').mkdir(parents=True)
        note = tmp_path / 'a' / 'private' / 'b' / 'note.md'
        note.write_text('# test')
        exclude = ExcludeConfig(ignore_folders=['private'])
        assert _is_excluded(note, tmp_path, exclude) is True
