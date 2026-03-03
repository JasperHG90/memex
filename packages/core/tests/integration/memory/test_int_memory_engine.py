import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.core import ExtractSemanticFacts
from memex_core.memory.extraction.models import RetainContent
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import MentalModel, TokenUsage
from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.config import (
    MemexConfig,
    ExtractionConfig,
    SimpleTextSplitting,
    ModelConfig,
    ServerConfig,
    MemoryConfig,
)
import dspy
import datetime as dt
from sqlmodel import select


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_memory_engine_lifecycle(session: AsyncSession, postgres_uri: str):
    """
    Integration test for the full MemoryEngine lifecycle:
    Retain -> Reflect (Triggered) -> Recall.
    """

    # 0. Setup Configuration & Dependencies
    # -------------------------------------
    from memex_core.config import PostgresMetaStoreConfig, PostgresInstanceConfig, SecretStr
    from urllib.parse import urlparse
    from memex_core.context import set_session_id

    # Set explicit session ID for tracing
    set_session_id('test_lifecycle_session')

    parsed = urlparse(postgres_uri)

    config = MemexConfig(
        server=ServerConfig(
            memory=MemoryConfig(
                extraction=ExtractionConfig(
                    model=ModelConfig(model='gemini/gemini-3-flash-preview'),
                    text_splitting=SimpleTextSplitting(
                        chunk_size_tokens=1000, chunk_overlap_tokens=100
                    ),
                    max_concurrency=10,
                ),
            ),
            meta_store=PostgresMetaStoreConfig(
                instance=PostgresInstanceConfig(
                    host=parsed.hostname or 'localhost',
                    port=parsed.port or 5432,
                    database=parsed.path.lstrip('/'),
                    user=parsed.username or 'postgres',
                    password=SecretStr(parsed.password or 'postgres'),
                )
            ),
        )
    )

    # Initialize LLM
    lm = dspy.LM(model=config.server.memory.extraction.model.model)

    with dspy.context(lm=lm):
        predictor = dspy.Predict(ExtractSemanticFacts)
        embedding_model = await get_embedding_model()
        entity_resolver = EntityResolver(resolution_threshold=0.65)

        # Initialize Sub-Engines
        extraction_engine = ExtractionEngine(
            config=config.server.memory.extraction,
            lm=lm,
            predictor=predictor,
            embedding_model=embedding_model,
            entity_resolver=entity_resolver,
        )

        retrieval_engine = RetrievalEngine(
            embedder=embedding_model,
            reranker=None,  # Skipping reranker for speed/dependency simplicity in this test
        )

        # Initialize Main Engine
        memory_engine = MemoryEngine(
            config=config,
            extraction_engine=extraction_engine,
            retrieval_engine=retrieval_engine,
        )

        # 1. Retain (Extraction + Persistence + Reflection)
        # -------------------------------------------------
        story_content = (
            'Project Aether was initiated on November 5, 2024, by Dr. Aris Thorne. '
            'The goal was to develop a high-efficiency atmospheric carbon capture system. '
            'Early tests in the Nevada desert showed promising results, capturing 40% more CO2 '
            'than traditional methods. However, funding issues delayed the Phase 2 expansion.'
        )

        retain_input = [
            RetainContent(
                content=story_content,
                event_date=dt.datetime(2024, 11, 5, tzinfo=dt.timezone.utc),
                payload={'source': 'lab_report', 'project': 'Aether'},
            )
        ]

        # We expect this to trigger reflection on "Project Aether" and "Dr. Aris Thorne"
        result = await memory_engine.retain(
            session=session,
            contents=retain_input,
            reflect_after=True,
            agent_name='integration_tester',
        )

        assert len(result['unit_ids']) > 0
        assert len(result['touched_entities']) > 0

        # Verify Reflection: Check if MentalModels were created
        # We wait a brief moment implicitly, but since retain awaits reflection, it should be done.
        stmt_mm = select(MentalModel)
        models = (await session.exec(stmt_mm)).all()

        # We expect at least one mental model (likely Project Aether or Aris Thorne)
        assert len(models) > 0
        model_names = [m.name for m in models]
        assert any('Aether' in name for name in model_names) or any(
            'Aris' in name for name in model_names
        )

        # 2. Recall (Retrieval)
        # ---------------------
        # Test retrieving facts about the project
        recall_request = RetrievalRequest(
            query='What were the results of the Project Aether tests?', limit=3
        )

        memories = await memory_engine.recall(session, recall_request)

        assert len(memories) > 0
        # Check relevance
        combined_text = ' '.join([m.text for m in memories])
        assert '40%' in combined_text or 'CO2' in combined_text

        # 3. Token Usage
        # ----------------------------
        token_usage = (await session.exec(select(TokenUsage))).all()
        jsonl = [tu.model_dump() for tu in token_usage]
        assert len(jsonl) > 0, 'Expected token usage logs to be recorded.'
