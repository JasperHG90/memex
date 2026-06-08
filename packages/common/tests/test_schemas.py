from memex_common.schemas import NoteMetadata, NoteCreateDTO
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
    """Test NoteCreateDTO instantiation and type conversion."""

    # "Hello World" in Base64
    content_b64 = 'SGVsbG8gV29ybGQ='
    content_expected = b'Hello World'

    # "file content" in Base64
    file_b64 = 'ZmlsZSBjb250ZW50'
    file_expected = b'file content'

    # Test 1: Direct instantiation with bytes (internal usage)
    # NoteCreateDTO expects Base64 encoded bytes
    content_b64_bytes = content_b64.encode('utf-8')
    file_b64_bytes = file_b64.encode('utf-8')

    dto_direct = NoteCreateDTO(
        name='test.md',
        description='Desc',
        content=content_b64_bytes,
        files={'file1.png': file_b64_bytes},
        tags=['test'],
    )
    assert dto_direct.content == content_b64_bytes
    assert dto_direct.content_decoded == content_expected

    # Test 2: Validation from JSON-like dict (API usage)
    dto_json = NoteCreateDTO.model_validate(
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


# -- BlockSummaryDTO & NoteSearchResult --


def test_block_summary_dto_basic():
    from memex_common.schemas import BlockSummaryDTO

    s = BlockSummaryDTO(topic='Machine Learning', key_points=['Supervised', 'Unsupervised'])
    assert s.topic == 'Machine Learning'
    assert s.key_points == ['Supervised', 'Unsupervised']


def test_block_summary_dto_defaults():
    from memex_common.schemas import BlockSummaryDTO

    s = BlockSummaryDTO(topic='Overview')
    assert s.key_points == []


def test_block_summary_dto_serialization():
    from memex_common.schemas import BlockSummaryDTO

    s = BlockSummaryDTO(topic='Test', key_points=['A', 'B'])
    d = s.model_dump()
    assert d == {'topic': 'Test', 'key_points': ['A', 'B']}
    roundtrip = BlockSummaryDTO(**d)
    assert roundtrip == s


def test_note_search_result_with_summaries():
    from uuid import uuid4
    from memex_common.schemas import BlockSummaryDTO, NoteSearchResult

    result = NoteSearchResult(
        note_id=uuid4(),
        metadata={'title': 'Test'},
        summaries=[
            BlockSummaryDTO(topic='Intro', key_points=['Context']),
            BlockSummaryDTO(topic='Methods'),
        ],
        score=0.85,
    )
    assert len(result.summaries) == 2
    assert result.summaries[0].topic == 'Intro'
    assert result.summaries[1].key_points == []


def test_note_search_result_default_summaries():
    from uuid import uuid4
    from memex_common.schemas import NoteSearchResult

    result = NoteSearchResult(note_id=uuid4(), metadata={})
    assert result.summaries == []
    assert result.score == 0.0


def test_note_dto_binary_content_serialization():
    """NoteCreateDTO with filename and binary content serializes without crashing."""
    import base64

    # Arbitrary binary bytes (not valid UTF-8)
    binary_content = bytes(range(256))
    b64_content = base64.b64encode(binary_content).decode('ascii')

    dto = NoteCreateDTO.model_validate(
        {
            'name': 'report.pdf',
            'description': 'A PDF report',
            'content': b64_content,
            'filename': 'report.pdf',
            'tags': ['test'],
        }
    )

    assert dto.filename == 'report.pdf'
    assert dto.content_decoded == binary_content

    # Serialization must not crash
    dumped = dto.model_dump(mode='json')
    assert dumped['filename'] == 'report.pdf'
    assert isinstance(dumped['content'], str)


class TestSharedClassificationCoercers:
    """F25b — ``coerce_intent_class`` / ``coerce_risk_class`` are the single
    source of truth for default-on-fail coercion of LLM-emitted intent/risk
    strings. Both ``RawFact`` and ``ExtractedFact`` validators (in
    ``memex_core.memory.extraction.models``) delegate to these helpers so
    they cannot diverge if a future ``IntentClass`` / ``RiskClass`` value is
    added.
    """

    def test_intent_passthrough_for_valid_values(self) -> None:
        from memex_common.schemas import IntentClass, coerce_intent_class

        for c in IntentClass:
            assert coerce_intent_class(c.value) == c.value

    def test_risk_passthrough_for_valid_values(self) -> None:
        from memex_common.schemas import RiskClass, coerce_risk_class

        for c in RiskClass:
            assert coerce_risk_class(c.value) == c.value

    def test_intent_invalid_string_falls_back_to_default(self) -> None:
        from memex_common.schemas import IntentClass, coerce_intent_class

        for garbage in ('', 'forever', 'unknown', 'critical'):
            assert coerce_intent_class(garbage) == IntentClass.DURABLE.value

    def test_risk_invalid_string_falls_back_to_default(self) -> None:
        from memex_common.schemas import RiskClass, coerce_risk_class

        for garbage in ('', 'very-bad', 'redacted'):
            assert coerce_risk_class(garbage) == RiskClass.NONE.value

    def test_intent_non_string_garbage_falls_back_to_default(self) -> None:
        from memex_common.schemas import IntentClass, coerce_intent_class

        garbage_inputs: list[object] = [
            None,
            42,
            3.14,
            ['durable'],
            {'k': 'durable'},
            b'durable',
        ]
        for garbage in garbage_inputs:
            assert coerce_intent_class(garbage) == IntentClass.DURABLE.value

    def test_risk_non_string_garbage_falls_back_to_default(self) -> None:
        from memex_common.schemas import RiskClass, coerce_risk_class

        garbage_inputs: list[object] = [None, 0, [], {}, b'none']
        for garbage in garbage_inputs:
            assert coerce_risk_class(garbage) == RiskClass.NONE.value


class TestSectionAssetAndNodeDTO:
    """V5: NodeDTO/TOCNodeDTO carry block_id + per-section assets."""

    def test_section_asset_dto_roundtrip(self) -> None:
        from memex_common.schemas import SectionAssetDTO

        a = SectionAssetDTO(path='img/x.png', alt_text='a cat', filename='x.png')
        assert a.model_dump() == {'path': 'img/x.png', 'alt_text': 'a cat', 'filename': 'x.png'}

    def test_section_asset_alt_optional(self) -> None:
        from memex_common.schemas import SectionAssetDTO

        a = SectionAssetDTO(path='x.png', filename='x.png')
        assert a.alt_text is None

    def test_node_dto_defaults(self) -> None:
        import datetime as dt
        from uuid import uuid4

        from memex_common.schemas import NodeDTO

        node = NodeDTO(
            id=uuid4(),
            note_id=uuid4(),
            vault_id=uuid4(),
            title='T',
            text='body',
            level=1,
            seq=0,
            status='active',
            created_at=dt.datetime.now(dt.timezone.utc),
        )
        assert node.block_id is None
        assert node.assets == []

    def test_node_dto_validates_assets_from_dicts(self) -> None:
        import datetime as dt
        from uuid import uuid4

        from memex_common.schemas import NodeDTO, SectionAssetDTO

        block = uuid4()
        node = NodeDTO(
            id=uuid4(),
            note_id=uuid4(),
            vault_id=uuid4(),
            block_id=block,
            title='T',
            text='body',
            level=1,
            seq=0,
            status='active',
            created_at=dt.datetime.now(dt.timezone.utc),
            assets=[{'path': 'a.png', 'alt_text': 'cap', 'filename': 'a.png'}],
        )
        assert node.block_id == block
        assert node.assets == [SectionAssetDTO(path='a.png', alt_text='cap', filename='a.png')]

    def test_toc_node_dto_assets(self) -> None:
        from memex_common.schemas import TOCNodeDTO

        toc = TOCNodeDTO.model_validate(
            {
                'id': 'abc',
                'title': 'Section',
                'level': 2,
                'assets': [{'path': 'p.png', 'alt_text': None, 'filename': 'p.png'}],
            }
        )
        assert toc.assets[0].path == 'p.png'
        assert TOCNodeDTO(id='x', title='y', level=1).assets == []


def test_create_vault_request_kind_validator_accepts_known_values():
    """CreateVaultRequest must accept both 'content' and 'system' kinds."""
    from memex_common.schemas import CreateVaultRequest

    assert CreateVaultRequest(name='x').kind == 'content'
    assert CreateVaultRequest(name='x', kind='system').kind == 'system'
    assert CreateVaultRequest(name='x', kind='content').kind == 'content'


def test_create_vault_request_kind_validator_rejects_unknown():
    """Unknown kind values must raise ValidationError (422 at the API layer),
    not propagate to a 500 IntegrityError at the DB layer."""
    import pytest
    from pydantic import ValidationError
    from memex_common.schemas import CreateVaultRequest

    with pytest.raises(ValidationError) as exc_info:
        CreateVaultRequest(name='x', kind='archive')
    # The error mentions the field and the bad value so the operator
    # can act on it without chasing a stack trace.
    assert 'kind' in str(exc_info.value)
    assert 'archive' in str(exc_info.value)
