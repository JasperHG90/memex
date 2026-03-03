"""Tests for build_note_dto in server/common.py."""

import datetime as dt
from types import SimpleNamespace
from uuid import uuid4

from memex_core.server.common import build_note_dto


class TestBuildNoteDtoDict:
    """Tests for the dict construction path."""

    def test_includes_assets(self):
        doc = {
            'id': uuid4(),
            'title': 'Test Note',
            'original_text': 'hello',
            'created_at': dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            'vault_id': uuid4(),
            'doc_metadata': {},
            'assets': ['assets/img.png', 'assets/doc.pdf'],
        }

        dto = build_note_dto(doc)

        assert dto.assets == ['assets/img.png', 'assets/doc.pdf']

    def test_assets_defaults_to_empty_list(self):
        doc = {
            'id': uuid4(),
            'title': 'Test Note',
            'original_text': 'hello',
            'created_at': dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            'vault_id': uuid4(),
            'doc_metadata': {},
        }

        dto = build_note_dto(doc)

        assert dto.assets == []


class TestBuildNoteDtoORM:
    """Tests for the ORM object construction path."""

    def test_includes_assets(self):
        doc = SimpleNamespace(
            id=uuid4(),
            title='Test Note',
            original_text='hello',
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            vault_id=uuid4(),
            doc_metadata={},
            assets=['assets/img.png', 'assets/doc.pdf'],
        )

        dto = build_note_dto(doc)

        assert dto.assets == ['assets/img.png', 'assets/doc.pdf']

    def test_assets_defaults_to_empty_list(self):
        doc = SimpleNamespace(
            id=uuid4(),
            title='Test Note',
            original_text='hello',
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            vault_id=uuid4(),
            doc_metadata={},
        )

        dto = build_note_dto(doc)

        assert dto.assets == []

    def test_assets_none_becomes_empty_list(self):
        doc = SimpleNamespace(
            id=uuid4(),
            title='Test Note',
            original_text='hello',
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            vault_id=uuid4(),
            doc_metadata={},
            assets=None,
        )

        dto = build_note_dto(doc)

        assert dto.assets == []
