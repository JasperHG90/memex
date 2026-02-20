from uuid import uuid4
from memex_cli.documents import app as doc_app
from memex_common.schemas import DocumentDTO
from datetime import datetime, timezone


def test_doc_list(runner, mock_api, monkeypatch):
    mock_api.list_documents.return_value = [
        DocumentDTO(
            id=uuid4(), name='My Doc', created_at=datetime.now(timezone.utc), vault_id=uuid4()
        )
    ]
    monkeypatch.setattr('memex_cli.documents.get_api_context', lambda config: mock_api)

    result = runner.invoke(doc_app, ['list'])
    assert result.exit_code == 0
    assert 'My Doc' in result.stdout


def test_doc_view(runner, mock_api, monkeypatch):
    d_id = uuid4()
    mock_api.get_document.return_value = DocumentDTO(
        id=d_id,
        name='Deep Dive',
        created_at=datetime.now(timezone.utc),
        vault_id=uuid4(),
        original_text='# Content Here',
    )
    monkeypatch.setattr('memex_cli.documents.get_api_context', lambda config: mock_api)

    result = runner.invoke(doc_app, ['view', str(d_id)])
    assert result.exit_code == 0
    assert 'Deep Dive' in result.stdout
    assert 'Content Here' in result.stdout


def test_doc_page_index_with_data(runner, mock_api, monkeypatch):
    """page-index command renders a Rich tree when page_index is present."""
    d_id = uuid4()
    mock_api.get_document_page_index.return_value = {
        'toc': [
            {
                'level': 1,
                'title': 'Introduction',
                'token_estimate': 80,
                'summary': {'what': 'An overview section'},
                'children': [],
            }
        ]
    }
    monkeypatch.setattr('memex_cli.documents.get_api_context', lambda config: mock_api)

    result = runner.invoke(doc_app, ['page-index', str(d_id)])
    assert result.exit_code == 0
    assert 'Introduction' in result.stdout
    assert 'Page Index' in result.stdout


def test_doc_page_index_none(runner, mock_api, monkeypatch):
    """page-index command prints a warning when the document has no page index."""
    d_id = uuid4()
    mock_api.get_document_page_index.return_value = None
    monkeypatch.setattr('memex_cli.documents.get_api_context', lambda config: mock_api)

    result = runner.invoke(doc_app, ['page-index', str(d_id)])
    assert result.exit_code == 0
    assert 'no page index' in result.stdout


def test_doc_page_index_invalid_uuid(runner, mock_api, monkeypatch):
    """page-index command shows an error for non-UUID arguments."""
    monkeypatch.setattr('memex_cli.documents.get_api_context', lambda config: mock_api)

    result = runner.invoke(doc_app, ['page-index', 'not-a-uuid'])
    assert result.exit_code == 0
    assert 'Invalid UUID' in result.stdout
