import pytest
import nest_asyncio
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from memex_core.memory.sql_models import MemoryUnit, Entity, EntityAlias, UnitEntity
from memex_core.memory.retrieval.engine import RetrievalEngine, RetrievalRequest
from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.utils import get_phonetic_code
import numpy as np

nest_asyncio.apply()


@pytest.fixture
async def setup_data(db_session: AsyncSession):
    # Create Entities
    # 1. Canonical: Elon Musk
    elon = Entity(
        canonical_name='Elon Musk',
        phonetic_code=get_phonetic_code('Elon Musk'),
        mention_count=10,
        last_seen=datetime.now(timezone.utc),
    )
    db_session.add(elon)

    # 2. Canonical: JavaScript (Alias: JS)
    js = Entity(
        canonical_name='JavaScript',
        phonetic_code=get_phonetic_code('JavaScript'),
        mention_count=5,
        last_seen=datetime.now(timezone.utc),
    )
    db_session.add(js)

    # 3. Canonical: Stephen (for phonetic test)
    stephen = Entity(
        canonical_name='Stephen',
        phonetic_code=get_phonetic_code('Stephen'),
        mention_count=2,
        last_seen=datetime.now(timezone.utc),
    )
    db_session.add(stephen)

    await db_session.commit()
    await db_session.refresh(elon)
    await db_session.refresh(js)
    await db_session.refresh(stephen)

    # Add Alias
    js_alias = EntityAlias(canonical_id=js.id, name='JS', phonetic_code=get_phonetic_code('JS'))
    db_session.add(js_alias)

    # Create Memories linked to these entities

    # Memory 1: Linked to Elon (Old)
    mem_elon_old = MemoryUnit(
        text='Elon founded SpaceX long ago.',
        event_date=datetime.now(timezone.utc) - timedelta(days=60),
        vault_id=GLOBAL_VAULT_ID,
        embedding=[0.0] * 384,
    )
    db_session.add(mem_elon_old)
    await db_session.commit()
    await db_session.refresh(mem_elon_old)
    db_session.add(UnitEntity(unit_id=mem_elon_old.id, entity_id=elon.id, vault_id=GLOBAL_VAULT_ID))

    # Memory 2: Linked to Elon (Recent)
    mem_elon_new = MemoryUnit(
        text='Elon Musk acquired Twitter recently.',
        event_date=datetime.now(timezone.utc) - timedelta(days=1),
        vault_id=GLOBAL_VAULT_ID,
        embedding=[0.0] * 384,
    )
    db_session.add(mem_elon_new)
    await db_session.commit()
    await db_session.refresh(mem_elon_new)
    db_session.add(UnitEntity(unit_id=mem_elon_new.id, entity_id=elon.id, vault_id=GLOBAL_VAULT_ID))

    # Memory 3: Linked to JS
    mem_js = MemoryUnit(
        text='JS is dynamic.',
        event_date=datetime.now(timezone.utc),
        vault_id=GLOBAL_VAULT_ID,
        embedding=[0.0] * 384,
    )
    db_session.add(mem_js)
    await db_session.commit()
    await db_session.refresh(mem_js)
    db_session.add(UnitEntity(unit_id=mem_js.id, entity_id=js.id, vault_id=GLOBAL_VAULT_ID))

    # Memory 4: Linked to Stephen
    mem_stephen = MemoryUnit(
        text='Stephen is here.',
        event_date=datetime.now(timezone.utc),
        vault_id=GLOBAL_VAULT_ID,
        embedding=[0.0] * 384,
    )
    db_session.add(mem_stephen)
    await db_session.commit()
    await db_session.refresh(mem_stephen)
    db_session.add(
        UnitEntity(unit_id=mem_stephen.id, entity_id=stephen.id, vault_id=GLOBAL_VAULT_ID)
    )

    await db_session.commit()

    return {
        'elon': elon,
        'js': js,
        'stephen': stephen,
        'mem_elon_old': mem_elon_old,
        'mem_elon_new': mem_elon_new,
        'mem_js': mem_js,
        'mem_stephen': mem_stephen,
    }


def get_engine(ner_model=None):
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [np.array([0.0] * 384)]  # Mock embedding as numpy array
    return RetrievalEngine(embedder=mock_embedder, reranker=None, ner_model=ner_model)


@pytest.mark.asyncio
async def test_retrieval_ner_canonical(db_session: AsyncSession, setup_data):
    """Test retrieval when NER extracts a canonical name."""
    # Mock NER to return 'Elon Musk'
    mock_ner = MagicMock()
    mock_ner.predict.return_value = [{'word': 'Elon Musk'}]

    engine = get_engine(ner_model=mock_ner)

    req = RetrievalRequest(query='What did Elon Musk do?', limit=10)
    results = await engine.retrieve(db_session, req)

    # Should find both Elon memories
    found_ids = [r.id for r in results]
    assert setup_data['mem_elon_new'].id in found_ids
    assert setup_data['mem_elon_old'].id in found_ids

    # Verify New scores higher than Old (Recency Decay)
    idx_new = found_ids.index(setup_data['mem_elon_new'].id)
    idx_old = found_ids.index(setup_data['mem_elon_old'].id)
    assert idx_new < idx_old, 'Recent memory should be ranked higher than old memory'


@pytest.mark.asyncio
async def test_retrieval_ner_alias(db_session: AsyncSession, setup_data):
    """Test retrieval when NER extracts an alias."""
    # Mock NER to return 'JS'
    mock_ner = MagicMock()
    mock_ner.predict.return_value = [{'word': 'JS'}]

    engine = get_engine(ner_model=mock_ner)

    req = RetrievalRequest(query='Tell me about JS', limit=10)
    results = await engine.retrieve(db_session, req)

    found_ids = [r.id for r in results]
    assert setup_data['mem_js'].id in found_ids


@pytest.mark.asyncio
async def test_retrieval_ner_phonetic(db_session: AsyncSession, setup_data):
    """Test retrieval when NER extracts a misspelled name (phonetic match)."""
    # Mock NER to return 'Stefen' (misspelled 'Stephen')
    mock_ner = MagicMock()
    mock_ner.predict.return_value = [{'word': 'Stefen'}]

    engine = get_engine(ner_model=mock_ner)

    req = RetrievalRequest(query='Where is Stefen?', limit=10)
    results = await engine.retrieve(db_session, req)

    found_ids = [r.id for r in results]
    assert setup_data['mem_stephen'].id in found_ids


@pytest.mark.asyncio
async def test_retrieval_fallback(db_session: AsyncSession, setup_data):
    """Test fallback when NER finds nothing."""
    # Mock NER to return nothing
    mock_ner = MagicMock()
    mock_ner.predict.return_value = []

    engine = get_engine(ner_model=mock_ner)

    # Query matching 'Elon' via trigram/ilike
    req = RetrievalRequest(query='Elon', limit=10)
    results = await engine.retrieve(db_session, req)

    found_ids = [r.id for r in results]
    assert setup_data['mem_elon_new'].id in found_ids
