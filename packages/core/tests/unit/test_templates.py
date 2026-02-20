import pathlib
from unittest.mock import patch
import aiofiles
import pytest
from pydantic import ValidationError
from memex_core.templates import MemexTemplateFromFile


def test_validation_path_not_exists(tmp_path: pathlib.Path) -> None:
    non_existent = tmp_path / 'ghost.md'
    with pytest.raises(ValidationError, match='Path does not exist'):
        MemexTemplateFromFile(path=non_existent)


def test_validation_dir_missing_note(tmp_path: pathlib.Path) -> None:
    empty_dir = tmp_path / 'empty_dir'
    empty_dir.mkdir()
    # Should fail looking for default NOTE.md
    with pytest.raises(ValidationError, match='does not contain NOTE.md'):
        MemexTemplateFromFile(path=empty_dir)


def test_validation_dir_missing_custom_note(tmp_path: pathlib.Path) -> None:
    d = tmp_path / 'custom_dir'
    d.mkdir()
    # Should fail looking for CUSTOM.md
    with pytest.raises(ValidationError, match='does not contain CUSTOM.md'):
        MemexTemplateFromFile(path=d, filename='CUSTOM.md')


@pytest.mark.asyncio
async def test_load_from_file_happy_path(tmp_path: pathlib.Path) -> None:
    # Arrange
    note_path = tmp_path / 'NOTE.md'
    content = b'---\nname: Test Template\ndescription: A test description\n---\n# Body Content'
    note_path.write_bytes(content)

    # Act
    template = MemexTemplateFromFile(path=note_path)

    # Assert
    assert template.is_dir is False
    assert await template.content == content

    fm = await template.frontmatter
    assert fm.metadata['name'] == 'Test Template'
    assert fm.metadata['description'] == 'A test description'

    assert await template.name == 'Test Template'
    assert await template.description == 'A test description'

    # Files should be empty for a single file template as per code logic
    assert await template.files == {}


@pytest.mark.asyncio
async def test_load_from_dir_happy_path(tmp_path: pathlib.Path) -> None:
    # Arrange
    template_dir = tmp_path / 'my_template'
    template_dir.mkdir()

    note_path = template_dir / 'NOTE.md'
    note_content = b'---\nname: Dir Template\n---\n# Dir Body'
    note_path.write_bytes(note_content)

    extra_file = template_dir / 'extra.txt'
    extra_file.write_bytes(b'extra content')

    sub_dir = template_dir / 'subdir'
    sub_dir.mkdir()
    (sub_dir / 'ignored.txt').write_bytes(b'ignored')

    # Act
    template = MemexTemplateFromFile(path=template_dir)

    # Assert
    assert template.is_dir is True
    assert await template.content == note_content

    files = await template.files
    assert 'extra.txt' in files
    assert files['extra.txt'] == b'extra content'
    assert 'NOTE.md' not in files
    assert 'subdir/ignored.txt' in files
    assert files['subdir/ignored.txt'] == b'ignored'


@pytest.mark.asyncio
async def test_properties_caching(tmp_path: pathlib.Path) -> None:
    # Arrange
    template_dir = tmp_path / 'cached_template'
    template_dir.mkdir()
    (template_dir / 'NOTE.md').write_bytes(b'---\nname: Cached\n---\nBody')
    (template_dir / 'extra.txt').write_bytes(b'data')

    template = MemexTemplateFromFile(path=template_dir)

    # Act & Assert
    with patch('aiofiles.open', wraps=aiofiles.open) as mock_open:
        # Access content twice
        c1 = await template.content
        c2 = await template.content
        assert c1 == c2
        # Should be called once for NOTE.md
        assert mock_open.call_count == 1

        # Access frontmatter (which uses cached content)
        f1 = await template.frontmatter
        _ = await template.frontmatter
        assert f1.metadata['name'] == 'Cached'
        # Accessing frontmatter should NOT trigger more file opens
        # because it uses await self.content (which is cached)
        assert mock_open.call_count == 1

        # Access files twice
        files1 = await template.files
        files2 = await template.files
        assert files1 == files2
        # files opens 'extra.txt'. It should only happen once.
        # Total opens: 1 (content) + 1 (extra.txt) = 2
        assert mock_open.call_count == 2
