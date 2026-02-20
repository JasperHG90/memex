from memex_common.schemas import NoteMetadata, NoteDTO
from memex_common.types import MemexTypes


def test_note_metadata_defaults():
    meta = NoteMetadata()
    assert meta.type == MemexTypes.NOTE
    assert meta.date_created is not None
    assert meta.uuid is None


def test_note_metadata_custom_type():
    meta = NoteMetadata(type=MemexTypes.KNOWLEDGE)
    assert meta.type == MemexTypes.KNOWLEDGE


def test_note_dto_instantiation():
    """Test NoteDTO instantiation and type conversion."""

    # "Hello World" in Base64
    content_b64 = 'SGVsbG8gV29ybGQ='
    content_expected = b'Hello World'

    # "file content" in Base64
    file_b64 = 'ZmlsZSBjb250ZW50'
    file_expected = b'file content'

    # Test 1: Direct instantiation with bytes (internal usage)
    # NoteDTO expects Base64 encoded bytes
    content_b64_bytes = content_b64.encode('utf-8')
    file_b64_bytes = file_b64.encode('utf-8')

    dto_direct = NoteDTO(
        name='test.md',
        description='Desc',
        content=content_b64_bytes,
        files={'file1.png': file_b64_bytes},
        tags=['test'],
    )
    assert dto_direct.content == content_b64_bytes
    assert dto_direct.content_decoded == content_expected

    # Test 2: Validation from JSON-like dict (API usage)
    dto_json = NoteDTO.model_validate(
        {
            'name': 'test.md',
            'description': 'Desc',
            'content': content_b64,
            'files': {'file1.png': file_b64},
            'tags': ['test'],
        }
    )

    assert dto_json.content == content_b64_bytes
    assert dto_json.files['file1.png'] == file_b64_bytes
    assert dto_json.content_decoded == content_expected
    assert dto_json.files_decoded['file1.png'] == file_expected
