from memex_core.api import Note
from memex_common.schemas import NoteDTO
import base64
import hashlib
from uuid import UUID


def test_calculate_uuid_from_dto_uses_key():
    """Test that calculate_uuid_from_dto uses document_key."""
    content = b'test content'
    encoded_content = base64.b64encode(content)

    # Use a valid UUID to avoid hashing for this test
    valid_uuid = '123e4567-e89b-12d3-a456-426614174000'

    dto = NoteDTO(
        name='test note',
        description='test description',
        content=encoded_content,
        document_key=valid_uuid,
    )

    uuid = Note.calculate_uuid_from_dto(dto)
    assert uuid == valid_uuid


def test_note_init_with_key():
    """Test that Note class accepts document_key."""
    content = b'test content'
    valid_uuid = '123e4567-e89b-12d3-a456-426614174000'

    note = Note(
        name='test note', description='test description', content=content, document_key=valid_uuid
    )

    assert note.document_key == valid_uuid
    assert note.uuid == valid_uuid


def test_document_key_hashing():
    """Test that arbitrary document_key is hashed to a UUID."""
    arbitrary_key = 'my-arbitrary-key'
    expected_hash = hashlib.md5(arbitrary_key.encode('utf-8')).hexdigest()

    note = Note(name='test', description='test', content=b'test', document_key=arbitrary_key)

    # Should be hashed
    assert note.document_key == expected_hash

    # Ensure it is parsable as UUID
    assert UUID(note.document_key)


def test_document_key_uuid_passthrough():
    """Test that valid UUID document_key is passed through."""
    valid_uuid = '123e4567-e89b-12d3-a456-426614174000'

    note = Note(name='test', description='test', content=b'test', document_key=valid_uuid)

    assert note.document_key == valid_uuid
