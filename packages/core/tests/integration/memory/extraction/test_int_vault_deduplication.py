import pytest
from uuid import uuid4
from datetime import datetime, timezone
from memex_common.types import FactTypes
from memex_core.memory.sql_models import Vault
from memex_core.memory.extraction.models import ProcessedFact
from memex_core.memory.extraction import storage
from memex_common.config import GLOBAL_VAULT_ID


@pytest.mark.integration
@pytest.mark.asyncio
async def test_int_vault_deduplication_isolation(session):
    """
    Test that deduplication is correctly scoped to vaults.
    A fact in Vault A should NOT be a duplicate for Vault B.
    """
    # 1. Setup Vaults
    vault_a = Vault(id=uuid4(), name='Vault A')
    vault_b = Vault(id=uuid4(), name='Vault B')
    session.add(vault_a)
    session.add(vault_b)
    await session.commit()

    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    text = 'The quick brown fox jumps over the lazy dog.'
    embedding = [0.1] * 384

    # 2. Insert fact into Vault A
    fact_a = ProcessedFact(
        fact_text=text,
        embedding=embedding,
        fact_type=FactTypes.WORLD,
        payload={},
        occurred_start=base_time,
        mentioned_at=base_time,
        vault_id=vault_a.id,
    )
    await storage.insert_facts_batch(session, [fact_a])

    # 3. Check duplicate in Vault B (Should be False)
    # Even though text matches, it is in a different vault
    results_b = await storage.check_duplicates_in_window(
        session, [text], [embedding], base_time, vault_ids=[vault_b.id]
    )
    assert results_b[0] is False, 'Fact in Vault A should NOT deduplicate in Vault B'

    # 4. Check duplicate in Vault A (Should be True)
    results_a = await storage.check_duplicates_in_window(
        session, [text], [embedding], base_time, vault_ids=[vault_a.id]
    )
    assert results_a[0] is True, 'Fact in Vault A SHOULD deduplicate in Vault A'

    # 5. Check Global fact (vault_id=None)
    # Insert global fact
    # NOTE: Use an embedding that is NOT collinear with the previous one to avoid semantic match
    # [0.1]*384 and [0.2]*384 have cosine similarity of 1.0!
    # We use negative values to ensure they are different.
    fact_g = ProcessedFact(
        fact_text='Global fact',
        embedding=[-0.1] * 384,
        fact_type=FactTypes.WORLD,
        payload={},
        occurred_start=base_time,
        mentioned_at=base_time,
        vault_id=GLOBAL_VAULT_ID,
    )
    await storage.insert_facts_batch(session, [fact_g])

    # Global fact should NOT deduplicate in Vault A (Strict Scoping)
    # Previously this was True (Fall-through), but now Vault A is strictly isolated.
    results_ga = await storage.check_duplicates_in_window(
        session, ['Global fact'], [[-0.1] * 384], base_time, vault_ids=[vault_a.id]
    )
    assert results_ga[0] is False, 'Global fact should NOT deduplicate in Strict Vault A'

    # Vault A fact should NOT deduplicate in Global
    # text="The quick brown...", embedding=[0.1]*384
    # Global has "Global fact", embedding=[-0.1]*384
    # Should be False.
    results_ag = await storage.check_duplicates_in_window(
        session, [text], [embedding], base_time, vault_ids=[GLOBAL_VAULT_ID]
    )
    assert results_ag[0] is False, 'Vault A fact should NOT deduplicate in Global Scope'
