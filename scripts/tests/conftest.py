"""Shared fixtures for memex_export_local tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add scripts/ to path so we can import memex_export_local
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sample_notes():
    """Sample note rows as returned by Postgres."""
    return [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "vault_id": "ac9b6a45-d388-5ddb-9fa9-50d4e5bca511",
            "title": "Deployment Architecture Retrospective",
            "status": "active",
            "publish_date": datetime(2026, 3, 15, tzinfo=timezone.utc),
            "created_at": datetime(2026, 3, 15, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 3, 20, tzinfo=timezone.utc),
            "metadata": '{"author": "claude-code"}',
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "vault_id": "ac9b6a45-d388-5ddb-9fa9-50d4e5bca511",
            "title": "Ollama Integration Lessons",
            "status": "active",
            "publish_date": datetime(2026, 2, 10, tzinfo=timezone.utc),
            "created_at": datetime(2026, 2, 10, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 2, 15, tzinfo=timezone.utc),
            "metadata": '{}',
        },
    ]


@pytest.fixture
def sample_memory_units():
    """Sample memory unit rows."""
    return [
        {
            "id": "aaaa1111-1111-1111-1111-111111111111",
            "note_id": "11111111-1111-1111-1111-111111111111",
            "text": "The three-tier architecture separates bot, worker, and human layers",
            "fact_type": "observation",
            "event_date": datetime(2026, 3, 15, tzinfo=timezone.utc),
            "confidence": 0.95,
            "status": "active",
            "context": "Deployment architecture note",
            "created_at": datetime(2026, 3, 15, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 3, 15, tzinfo=timezone.utc),
        },
        {
            "id": "aaaa2222-2222-2222-2222-222222222222",
            "note_id": "22222222-2222-2222-2222-222222222222",
            "text": "Ollama gemma3 12b runs well on RTX 5060 Ti with 4-bit quantization",
            "fact_type": "world",
            "event_date": datetime(2026, 2, 10, tzinfo=timezone.utc),
            "confidence": 0.9,
            "status": "active",
            "context": "Ollama integration note",
            "created_at": datetime(2026, 2, 10, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 2, 10, tzinfo=timezone.utc),
        },
    ]


@pytest.fixture
def sample_entities():
    return [
        {"id": "eeee1111-1111-1111-1111-111111111111", "canonical_name": "Ollama", "entity_type": "tool", "mention_count": 42},
        {"id": "eeee2222-2222-2222-2222-222222222222", "canonical_name": "Claude Code", "entity_type": "tool", "mention_count": 85},
    ]


@pytest.fixture
def sample_cooccurrences():
    return [
        {"entity_id_1": "eeee1111-1111-1111-1111-111111111111", "entity_id_2": "eeee2222-2222-2222-2222-222222222222", "cooccurrence_count": 15},
    ]


@pytest.fixture
def sample_mental_models():
    return [
        {
            "id": "mmmm1111-1111-1111-1111-111111111111",
            "entity_id": "eeee1111-1111-1111-1111-111111111111",
            "name": "Ollama",
            "observations": '[{"text": "Runs locally on RTX 5060 Ti"}]',
            "version": 3,
        },
    ]


@pytest.fixture
def mock_pg_conn(sample_notes, sample_memory_units, sample_entities, sample_cooccurrences, sample_mental_models):
    """Mock Postgres connection that returns sample data."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn
