"""
Reasoning Engine for the Hindsight "CARA" component.

Handles the formation of new opinions and beliefs based on user interactions
and retrieved evidence.
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Any
import dspy
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.reflect.prompts import (
    OpinionFormationSignature,
    OpinionRelationshipSignature,
)
from memex_core.memory.reflect.models import OpinionFormationRequest
from memex_core.memory.extraction.models import ProcessedFact
from memex_core.memory.extraction import storage
from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.models.embedding import FastEmbedder
from memex_core.memory.reflect.utils import create_citation_map
from memex_core.llm import run_dspy_operation
from memex_core.memory.confidence import ConfidenceEngine
from memex_core.memory.sql_models import MemoryUnit
from memex_core.memory.retrieval.engine import get_retrieval_engine
from memex_core.memory.retrieval.models import RetrievalRequest


logger = logging.getLogger('memex.core.memory.reflect.reasoning')


async def get_reasoning_engine(
    session: AsyncSession,
    lm: dspy.LM,
    embedding_model: FastEmbedder,
) -> 'ReasoningEngine':
    """
    Factory method to create a ReasoningEngine with dependencies.
    """

    retrieval_engine = await get_retrieval_engine()
    return ReasoningEngine(
        session=session,
        lm=lm,
        embedding_model=embedding_model,
        retrieval_engine=retrieval_engine,
    )


class ReasoningEngine:
    """
    Orchestrates the reasoning and opinion formation process.
    """

    def __init__(
        self,
        session: AsyncSession,
        lm: dspy.LM,
        embedding_model: FastEmbedder,
        retrieval_engine: Any,
    ):
        self.session = session
        self.lm = lm
        self.embedding_model = embedding_model
        self.confidence_engine = ConfidenceEngine()
        self.retrieval_engine = retrieval_engine

    async def form_opinions(
        self,
        request: OpinionFormationRequest,
    ) -> list[str]:
        """
        Analyze the interaction to form new opinions.
        Persists them to the database.
        Returns a list of created Unit IDs.
        """
        query = request.query
        answer = request.answer
        agent_name = request.agent_name

        uuid_to_int: dict[str, int] = {}
        int_to_uuid: dict[int, str] = {}

        all_ids = [str(unit.id) for unit in request.context]
        uuid_to_int, int_to_uuid = create_citation_map(all_ids)

        logger.info('Analyzing interaction for opinion formation...')

        predictor = dspy.ChainOfThought(OpinionFormationSignature)

        # Prepare context as list of strings for LLM
        # We explicitly tag them as historical to avoid the "Context Echo" loop
        context_strs = [
            f'[Historical Context]: {unit.formatted_fact_text}' for unit in request.context
        ]

        # Use run_dspy_operation for unified token usage tracking
        result, token_usage = await run_dspy_operation(
            lm=self.lm,
            predictor=predictor,
            input_kwargs={'query': query, 'context': context_strs, 'answer': answer},
            session=self.session,
            context_metadata={'operation': 'form_opinions'},
            vault_id=request.vault_id or GLOBAL_VAULT_ID,
        )

        formed_opinions = result.formed_opinions
        if not formed_opinions:
            logger.info('No new opinions formed.')
            return []

        # Restore UUIDs from aliases
        for op in formed_opinions:
            real_ids = []
            for idx in op.evidence_indices:
                # 1. Resolve Integer Alias
                if idx in int_to_uuid:
                    real_ids.append(int_to_uuid[idx])
                else:
                    logger.warning(f'Discarding invalid evidence reference: {idx}')

            op.evidence_indices = real_ids

        logger.info(f'Formed {len(formed_opinions)} new opinions.')

        # Convert to ProcessedFact for storage
        facts_to_store = []
        unit_ids = []
        current_time = datetime.now(timezone.utc)

        # Predictor for relationship check
        relationship_predictor = dspy.Predict(OpinionRelationshipSignature)

        for op in formed_opinions:
            # Generate embedding for the opinion statement
            # We embed ONLY the statement to ensure reliable deduplication/revision.
            # Reasoning is stored in payload and shouldn't skew the vector identity of the belief.
            text_to_embed = op.statement
            embedding_np = await asyncio.to_thread(self.embedding_model.encode, [text_to_embed])
            embedding = embedding_np[0].tolist()

            # Map confidence score to Beta distribution (informative prior)
            mass = 2.0
            alpha = op.confidence_score * mass
            beta = (1 - op.confidence_score) * mass

            # Deduplication: Retrieve-then-Verify Strategy
            # We use RetrievalEngine (RRF) to find candidates via Vector, Keyword, and Graph search.
            # This is robust to low vector similarity (e.g. contradictions).

            retrieval_req = RetrievalRequest(
                query=op.statement,
                limit=5,
                filters={'fact_type': 'opinion'},
                vault_ids=[request.vault_id] if request.vault_id else None,
            )

            candidates = await self.retrieval_engine.retrieve(self.session, retrieval_req)

            is_merged = False
            if candidates:
                # 2. Verify relationship with EACH candidate
                for existing_unit in candidates:
                    # Skip if not a MemoryUnit (e.g. MentalModel) or self
                    # Also skip virtual "observation" units which are not persisted in memory_unit table
                    if (
                        not isinstance(existing_unit, MemoryUnit)
                        or not existing_unit.text
                        or existing_unit.unit_metadata.get('observation')
                    ):
                        continue

                    # Call LLM to check relationship
                    rel_result, _ = await run_dspy_operation(
                        lm=self.lm,
                        predictor=relationship_predictor,
                        input_kwargs={
                            'existing_statement': existing_unit.text,
                            'new_statement': op.statement,
                        },
                        session=self.session,
                        context_metadata={'operation': 'opinion_rel'},
                        vault_id=request.vault_id or GLOBAL_VAULT_ID,
                    )

                    rel_type = rel_result.relationship.lower()

                    if 'reinforces' in rel_type:
                        logger.info(
                            f"Reinforcing opinion {existing_unit.id} with '{op.statement[:30]}...' "
                            f'(reasoning={rel_result.reasoning})'
                        )
                        # Reinforce: Add Alpha
                        await self.confidence_engine.apply_custom_update(
                            self.session,
                            existing_unit.id,
                            alpha_delta=alpha,
                            beta_delta=beta,  # Keep beta update small/proportional
                            evidence_type='opinion_reinforced',
                            description=f"Reinforced by: '{op.statement[:50]}...'",
                        )
                        unit_ids.append(str(existing_unit.id))
                        is_merged = True
                        break  # Stop checking other candidates once merged

                    elif 'contradicts' in rel_type:
                        logger.info(
                            f"Contradicting opinion {existing_unit.id} with '{op.statement[:30]}...' "
                            f'(reasoning={rel_result.reasoning})'
                        )
                        # Contradict: Add New Alpha to Old Beta (Evidence AGAINST old belief)
                        await self.confidence_engine.apply_custom_update(
                            self.session,
                            existing_unit.id,
                            alpha_delta=0,
                            beta_delta=alpha,  # Use the mass of the new belief as counter-evidence
                            evidence_type='opinion_contradicted',
                            description=f"Contradicted by: '{op.statement[:50]}...'",
                        )
                        # Do NOT mark as merged; we still want to insert the correction.

            if not is_merged:
                # Create new opinion
                pf = ProcessedFact(
                    fact_text=op.statement,
                    fact_type='opinion',
                    embedding=embedding,
                    occurred_start=current_time,  # Opinions are formed "now"
                    occurred_end=None,
                    mentioned_at=current_time,
                    confidence_alpha=alpha,
                    confidence_beta=beta,
                    payload={
                        'reasoning': op.reasoning,
                        'evidence_indices': op.evidence_indices,
                        'entities': op.entities,
                        'agent': agent_name,
                    },
                    entities=[],
                    causal_relations=[],
                    context=f'Formed from query: {query}',
                    vault_id=request.vault_id if request.vault_id else GLOBAL_VAULT_ID,
                )
                facts_to_store.append(pf)

        # Store in DB
        if facts_to_store:
            new_ids = await storage.insert_facts_batch(self.session, facts_to_store)
            unit_ids.extend(new_ids)

        return unit_ids
