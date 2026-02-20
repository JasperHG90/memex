from memex_common.schemas import NoteDTO
import base64


def test_note_dto_with_document_key():
    """Test that NoteDTO can be instantiated with an optional document_key."""
    content = b'test content'
    encoded_content = base64.b64encode(content)

    # This should fail initially because document_key is not yet a field
    note = NoteDTO(
        name='test note',
        description='test description',
        content=encoded_content,
        document_key='my-stable-key',
    )

    assert note.document_key == 'my-stable-key'


def test_note_dto_without_document_key():
    """Test that NoteDTO works without document_key."""
    content = b'test content'
    encoded_content = base64.b64encode(content)

    note = NoteDTO(name='test note', description='test description', content=encoded_content)

    assert note.document_key is None
