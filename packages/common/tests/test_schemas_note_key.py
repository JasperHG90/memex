from memex_common.schemas import NoteCreateDTO
import base64


def test_note_dto_with_note_key():
    """Test that NoteCreateDTO can be instantiated with an optional note_key."""
    content = b'test content'
    encoded_content = base64.b64encode(content)

    # This should work because note_key is now a field
    note = NoteCreateDTO(
        name='test note',
        description='test description',
        content=encoded_content,
        note_key='my-stable-key',
    )

    assert note.note_key == 'my-stable-key'


def test_note_dto_without_note_key():
    """Test that NoteCreateDTO works without note_key."""
    content = b'test content'
    encoded_content = base64.b64encode(content)

    note = NoteCreateDTO(name='test note', description='test description', content=encoded_content)

    assert note.note_key is None
