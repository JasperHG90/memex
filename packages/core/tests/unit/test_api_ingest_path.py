import base64
import re

import pytest
import yaml
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from memex_core.api import NoteInput, inject_user_notes
from memex_core.services.ingestion import IngestionService
from memex_core.processing.titles import _is_meaningful_name

_FM_RE = re.compile(r'\A---[ \t]*\n(.*?\n)---[ \t]*\n', re.DOTALL)


def _parse_frontmatter(text: str) -> dict:
    """Extract and parse YAML frontmatter from text."""
    m = _FM_RE.match(text)
    assert m, f'No frontmatter found in: {text[:200]}'
    data = yaml.safe_load(m.group(1)) or {}
    # YAML block scalars (|) always append a trailing newline; strip it for
    # easier assertions.
    for k, v in data.items():
        if isinstance(v, str) and v.endswith('\n'):
            data[k] = v.rstrip('\n')
    return data


@pytest.mark.asyncio
async def test_ingest_from_file_markdown(api, tmp_path):
    # Setup a dummy .md file
    md_file = tmp_path / 'test.md'
    md_file.write_text('# Test Content')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        resolved_vault = uuid4()
        mock_resolve.return_value = resolved_vault
        mock_note = MagicMock(spec=NoteInput)
        mock_from_file.return_value = mock_note
        mock_ingest.return_value = {'status': 'success'}

        result = await api.ingest_from_file(md_file)

        assert result['status'] == 'success'
        mock_from_file.assert_called_once_with(md_file, user_notes=None)
        mock_ingest.assert_called_once_with(mock_note, vault_id=resolved_vault)


@pytest.mark.asyncio
async def test_ingest_from_file_directory(api, tmp_path):
    # Setup a dummy directory
    note_dir = tmp_path / 'my_note'
    note_dir.mkdir()
    (note_dir / 'NOTE.md').write_text('# NoteInput')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        resolved_vault = uuid4()
        mock_resolve.return_value = resolved_vault
        mock_note = MagicMock(spec=NoteInput)
        mock_from_file.return_value = mock_note
        mock_ingest.return_value = {'status': 'success'}

        result = await api.ingest_from_file(note_dir)

        assert result['status'] == 'success'
        mock_from_file.assert_called_once_with(note_dir, user_notes=None)
        mock_ingest.assert_called_once_with(mock_note, vault_id=resolved_vault)


@pytest.mark.asyncio
async def test_ingest_from_file_markitdown(api, tmp_path):
    # Setup a dummy .docx file
    docx_file = tmp_path / 'test.docx'
    docx_file.write_text('dummy binary content')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        mock_resolve.return_value = uuid4()
        # Mock _file_processor.extract on the ingestion service
        api._ingestion._file_processor = MagicMock()
        extracted = MagicMock()
        extracted.content = 'Extracted Text'
        extracted.content_type = 'docx'
        extracted.source = str(docx_file)
        extracted.document_date = None
        extracted.images = {}
        extracted.metadata = {}
        api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

        with patch(
            'memex_core.services.ingestion.extract_document_date', new_callable=AsyncMock
        ) as mock_date:
            mock_date.return_value = None
            mock_ingest.return_value = {'status': 'success'}
            result = await api.ingest_from_file(docx_file)

        assert result['status'] == 'success'
        mock_from_file.assert_not_called()
        api._ingestion._file_processor.extract.assert_called_once_with(docx_file)
        assert mock_ingest.call_count == 1
        # The note name should be the file stem
        called_note = mock_ingest.call_args[0][0]
        assert called_note._metadata.name == 'test'
        assert called_note.source_uri == str(docx_file.absolute())


@pytest.mark.asyncio
async def test_ingest_from_file_llm_title_when_no_metadata_title(api, tmp_path):
    """When PDF metadata has no title and the file stem is not meaningful,
    LLM title extraction should be used instead of falling through to H1 regex."""
    # Use a temp-file-like name so _is_meaningful_name returns False
    pdf_file = tmp_path / 'tmpabcdef12.pdf'
    pdf_file.write_bytes(b'dummy pdf content')

    assert not _is_meaningful_name('tmpabcdef12')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as _mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        mock_resolve.return_value = uuid4()
        # Mock _file_processor.extract — no metadata title
        api._ingestion._file_processor = MagicMock()
        extracted = MagicMock()
        extracted.content = '# Add a Sub Title\n\nThis is a guide about Python testing.'
        extracted.content_type = 'pdf'
        extracted.source = str(pdf_file)
        extracted.document_date = None
        extracted.images = {}
        extracted.metadata = {}  # No 'title' key — simulates empty PDF metadata
        api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

        with (
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
            ) as mock_date,
            patch(
                'memex_core.services.ingestion.extract_title_via_llm',
                new_callable=AsyncMock,
            ) as mock_llm_title,
        ):
            mock_date.return_value = None
            mock_llm_title.return_value = 'Python Testing Guide'
            mock_ingest.return_value = {'status': 'success'}

            result = await api.ingest_from_file(pdf_file)

        assert result['status'] == 'success'
        # LLM title extraction should have been called
        mock_llm_title.assert_called_once()
        # The note should use the LLM-extracted title, not H1 or file stem
        called_note = mock_ingest.call_args[0][0]
        assert called_note._metadata.name == 'Python Testing Guide'


@pytest.mark.asyncio
async def test_ingest_from_file_skips_llm_title_when_metadata_title_present(api, tmp_path):
    """When PDF metadata provides a meaningful title, LLM title extraction is skipped."""
    pdf_file = tmp_path / 'tmpabcdef12.pdf'
    pdf_file.write_bytes(b'dummy pdf content')

    with (
        patch.object(IngestionService, 'ingest', new_callable=AsyncMock) as mock_ingest,
        patch.object(NoteInput, 'from_file', new_callable=AsyncMock) as _mock_from_file,
        patch.object(
            api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
        ) as mock_resolve,
    ):
        mock_resolve.return_value = uuid4()
        api._ingestion._file_processor = MagicMock()
        extracted = MagicMock()
        extracted.content = '# Add a Sub Title\n\nSome content.'
        extracted.content_type = 'pdf'
        extracted.source = str(pdf_file)
        extracted.document_date = None
        extracted.images = {}
        extracted.metadata = {'title': 'Real PDF Title'}  # Metadata has a title
        api._ingestion._file_processor.extract = AsyncMock(return_value=extracted)

        with (
            patch(
                'memex_core.services.ingestion.extract_document_date',
                new_callable=AsyncMock,
            ) as mock_date,
            patch(
                'memex_core.services.ingestion.extract_title_via_llm',
                new_callable=AsyncMock,
            ) as mock_llm_title,
        ):
            mock_date.return_value = None
            mock_ingest.return_value = {'status': 'success'}

            result = await api.ingest_from_file(pdf_file)

        assert result['status'] == 'success'
        # LLM title extraction should NOT have been called — metadata title is meaningful
        mock_llm_title.assert_not_called()
        called_note = mock_ingest.call_args[0][0]
        assert called_note._metadata.name == 'Real PDF Title'


def test_user_notes_injected_after_frontmatter():
    """User notes should appear as a user_notes field in YAML frontmatter."""
    content = b'---\ntitle: Test\n---\nBody text here.'
    note = NoteInput(
        name='Test',
        description='desc',
        content=content,
        user_notes='This is important context.',
    )
    text = note._content.decode('utf-8')
    fm = _parse_frontmatter(text)
    assert fm['user_notes'] == 'This is important context.'
    assert fm['title'] == 'Test'
    # Body should appear after frontmatter
    assert 'Body text here.' in text


def test_user_notes_injected_without_frontmatter():
    """User notes should create frontmatter when none exists."""
    content = b'Just some plain text.'
    note = NoteInput(
        name='Test',
        description='desc',
        content=content,
        user_notes='My commentary.',
    )
    text = note._content.decode('utf-8')
    fm = _parse_frontmatter(text)
    assert fm['user_notes'] == 'My commentary.'
    assert 'Just some plain text.' in text


def test_user_notes_none_leaves_content_unchanged():
    """When user_notes is None, content should be unchanged."""
    content = b'---\ntitle: Test\n---\nBody.'
    note = NoteInput(name='Test', description='desc', content=content, user_notes=None)
    assert note._content == content


def test_user_notes_empty_string_leaves_content_unchanged():
    """When user_notes is empty/whitespace, content should be unchanged."""
    content = b'---\ntitle: Test\n---\nBody.'
    note = NoteInput(name='Test', description='desc', content=content, user_notes='   ')
    assert note._content == content


def test_user_notes_with_broken_frontmatter():
    """When content starts with --- but has no closing ---, treat as no frontmatter."""
    content = b'--- this is not real frontmatter\nSome text.'
    note = NoteInput(
        name='Test',
        description='desc',
        content=content,
        user_notes='My note.',
    )
    text = note._content.decode('utf-8')
    fm = _parse_frontmatter(text)
    assert fm['user_notes'] == 'My note.'
    assert '--- this is not real frontmatter' in text


# ---------------------------------------------------------------------------
# inject_user_notes standalone helper tests
# ---------------------------------------------------------------------------


def test_inject_user_notes_after_frontmatter():
    """Standalone helper adds user_notes field to existing frontmatter."""
    content = '---\ntitle: Test\n---\nBody text here.'
    result = inject_user_notes(content, 'Important context.')
    fm = _parse_frontmatter(result)
    assert fm['user_notes'] == 'Important context.'
    assert fm['title'] == 'Test'
    assert 'Body text here.' in result


def test_inject_user_notes_without_frontmatter():
    """Standalone helper creates frontmatter when none present."""
    content = 'Just some plain text.'
    result = inject_user_notes(content, 'My commentary.')
    fm = _parse_frontmatter(result)
    assert fm['user_notes'] == 'My commentary.'
    assert 'Just some plain text.' in result


def test_inject_user_notes_none_returns_unchanged():
    """None user_notes returns content unchanged."""
    content = '---\ntitle: Test\n---\nBody.'
    assert inject_user_notes(content, None) == content


def test_inject_user_notes_empty_returns_unchanged():
    """Empty/whitespace user_notes returns content unchanged."""
    content = '---\ntitle: Test\n---\nBody.'
    assert inject_user_notes(content, '') == content
    assert inject_user_notes(content, '   ') == content


def test_noteinput_uses_inject_helper():
    """NoteInput produces same result as calling inject_user_notes directly."""
    raw = '---\ntitle: Test\n---\nBody text.'
    user_notes = 'My commentary on this article.'
    expected = inject_user_notes(raw, user_notes)
    note = NoteInput(
        name='Test',
        description='desc',
        content=raw.encode('utf-8'),
        user_notes=user_notes,
    )
    assert note._content.decode('utf-8') == expected


# ---------------------------------------------------------------------------
# Batch ingestion user_notes regression test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_batch_internal_preserves_user_notes(api):
    """user_notes from NoteCreateDTO must appear in content passed to memory.retain().

    This is the critical regression test: the background ingestion path
    (ingest_batch_internal) previously bypassed NoteInput and silently dropped
    user_notes.
    """
    from memex_common.schemas import NoteCreateDTO

    note_content = '---\nsource_url: https://example.com\n---\nArticle body.'
    user_notes_text = 'This is my personal commentary on the article.'

    dto = NoteCreateDTO(
        name='Test Note',
        description='Test description',
        content=base64.b64encode(note_content.encode('utf-8')),
        tags=['test'],
        user_notes=user_notes_text,
    )

    # Track what content is passed to memory.retain()
    captured_contents = []

    async def capturing_retain(session, contents, note_id, reflect_after=True, agent_name=None):
        captured_contents.extend(contents)
        return {'status': 'success'}

    api._ingestion.memory.retain = AsyncMock(side_effect=capturing_retain)

    # Mock vault resolution
    vault_id = uuid4()
    with patch.object(
        api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
    ) as mock_resolve:
        mock_resolve.return_value = vault_id

        # Mock session for vault lookup and idempotency check
        mock_vault = MagicMock()
        mock_vault.name = 'test-vault'

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_vault)
        mock_exec_result = MagicMock()
        mock_exec_result.all.return_value = []  # No existing docs
        mock_session.exec = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        api._ingestion.metastore.session.return_value = ctx

        # Mock AsyncTransaction
        mock_txn = AsyncMock()
        mock_txn.db_session = mock_session
        mock_txn.save_file = AsyncMock()

        with (
            patch(
                'memex_core.services.ingestion.AsyncTransaction',
            ) as mock_txn_cls,
            patch(
                'memex_core.services.ingestion.resolve_document_title',
                new_callable=AsyncMock,
                return_value='Test Note',
            ),
        ):
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=mock_txn)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            # Consume the async generator
            async for _result in api._ingestion.ingest_batch_internal(
                notes=[dto], vault_id=vault_id
            ):
                pass

    # Verify user_notes was injected into frontmatter
    assert len(captured_contents) == 1, 'Expected exactly one RetainContent'
    retained_content = captured_contents[0].content
    fm = _parse_frontmatter(retained_content)
    assert fm.get('user_notes') == user_notes_text, (
        'user_notes field missing from batch-ingested frontmatter'
    )
    assert fm.get('source_url') == 'https://example.com', (
        'existing frontmatter fields should be preserved'
    )


@pytest.mark.asyncio
async def test_ingest_batch_internal_no_user_notes_unchanged(api):
    """When user_notes is None, batch ingestion should not inject anything."""
    from memex_common.schemas import NoteCreateDTO

    note_content = '---\nsource_url: https://example.com\n---\nArticle body.'

    dto = NoteCreateDTO(
        name='Test Note',
        description='Test description',
        content=base64.b64encode(note_content.encode('utf-8')),
        tags=['test'],
        # user_notes deliberately omitted (defaults to None)
    )

    captured_contents = []

    async def capturing_retain(session, contents, note_id, reflect_after=True, agent_name=None):
        captured_contents.extend(contents)
        return {'status': 'success'}

    api._ingestion.memory.retain = AsyncMock(side_effect=capturing_retain)

    vault_id = uuid4()
    with patch.object(
        api._vaults, 'resolve_vault_identifier', new_callable=AsyncMock
    ) as mock_resolve:
        mock_resolve.return_value = vault_id

        mock_vault = MagicMock()
        mock_vault.name = 'test-vault'

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_vault)
        mock_exec_result = MagicMock()
        mock_exec_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        api._ingestion.metastore.session.return_value = ctx

        mock_txn = AsyncMock()
        mock_txn.db_session = mock_session
        mock_txn.save_file = AsyncMock()

        with (
            patch(
                'memex_core.services.ingestion.AsyncTransaction',
            ) as mock_txn_cls,
            patch(
                'memex_core.services.ingestion.resolve_document_title',
                new_callable=AsyncMock,
                return_value='Test Note',
            ),
        ):
            mock_txn_cls.return_value.__aenter__ = AsyncMock(return_value=mock_txn)
            mock_txn_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            async for _result in api._ingestion.ingest_batch_internal(
                notes=[dto], vault_id=vault_id
            ):
                pass

    assert len(captured_contents) == 1
    retained_content = captured_contents[0].content
    assert 'user_notes' not in retained_content
    assert retained_content == note_content


# ---------------------------------------------------------------------------
# inject_user_notes edge-case tests
# ---------------------------------------------------------------------------


def test_inject_user_notes_preserves_existing_fields_exactly():
    """Existing frontmatter fields must not be modified (no YAML round-trip)."""
    content = '---\ntitle: Test\nauthor: \nsource_url: https://example.com\n---\nBody.'
    result = inject_user_notes(content, 'My note.')
    # Original fields preserved verbatim (no null substitution)
    assert 'title: Test\n' in result
    assert 'author: \n' in result
    assert 'source_url: https://example.com\n' in result
    fm = _parse_frontmatter(result)
    assert fm['user_notes'] == 'My note.'


def test_inject_user_notes_multiline():
    """Multi-line user notes produce a valid YAML block scalar."""
    content = '---\ntitle: Test\n---\nBody.'
    result = inject_user_notes(content, 'Line one\nLine two\nLine three')
    fm = _parse_frontmatter(result)
    assert fm['user_notes'] == 'Line one\nLine two\nLine three'
    assert 'user_notes: |\n' in result


def test_inject_user_notes_special_characters():
    """User notes with YAML-special characters are safe inside block scalar."""
    content = '---\ntitle: Test\n---\nBody.'
    notes = 'Key: value with "quotes" and \'apostrophes\'\n---\nMore text'
    result = inject_user_notes(content, notes)
    fm = _parse_frontmatter(result)
    assert fm['user_notes'] == notes
    assert 'Body.' in result


def test_inject_user_notes_replaces_existing():
    """When frontmatter already has user_notes, the new value replaces it."""
    content = '---\ntitle: Test\nuser_notes: |\n  Old note.\n---\nBody.'
    result = inject_user_notes(content, 'New note.')
    fm = _parse_frontmatter(result)
    assert fm['user_notes'] == 'New note.'
    assert 'Old note.' not in result


def test_inject_user_notes_empty_lines_preserved():
    """Empty lines within user notes are preserved."""
    content = '---\ntitle: Test\n---\nBody.'
    notes = 'Para one\n\nPara two'
    result = inject_user_notes(content, notes)
    fm = _parse_frontmatter(result)
    assert fm['user_notes'] == 'Para one\n\nPara two'
