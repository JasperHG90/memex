"""Suite-private setup actions for ``agent_integration``.

Currently exposes ``seed_mental_model_observation``: a direct-DB seed
that materializes the minimum reflection state the V21 400-recovery
contract needs (Entity + MentalModel + one Observation citing two
already-ingested memory units as evidence). The two cited MUs come
from a source note that the runner ingested via the normal pipeline
(so they have real embeddings and surface in semantic search); the
handler only DB-seeds the reflection layer the production async
worker would otherwise produce minutes later.

The handler matches the ``direct-db-setup-action`` contract from
``eval-suites.md``:

- ``required = True`` so a seed failure errors the scenario.
- ``reusable_under_reuse_vault = True`` because every INSERT uses a
  deterministic UUIDv5 id with ``ON CONFLICT DO NOTHING`` so a
  second run sees identical state.
- The handler returns ``{'observation_id', 'source_mu_ids',
  'entity_id'}`` which the runner auto-prefixes with the handler
  name (``seed_mental_model_observation.observation_id`` etc.) and
  threads into ``outcome.score(context=...)``.

Per the eval-suites direct-db rule the handler resolves a fresh
async engine against the same DSN the suite reset / teardown use,
and writes via SQLModel session — never via the HTTP api client.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.dialects.postgresql import insert as pg_insert

from memex_eval.suite.setup_actions import SetupActionHandler, register_setup_action

if TYPE_CHECKING:
    from memex_common.client import RemoteMemexAPI

logger = logging.getLogger('memex_eval.suites.agent_integration.setup_actions')


_SEED_NAMESPACE = uuid5(NAMESPACE_URL, 'memex_eval/agent_integration/seed_mental_model_observation')


def _det_uuid(*parts: str) -> UUID:
    """Deterministic UUIDv5 across handler invocations.

    Uses ``\\x00`` as field separator (per the eval-suites rule:
    ``|`` collides on any field that contains a pipe).
    """
    return uuid5(_SEED_NAMESPACE, '\x00'.join(parts))


@register_setup_action('seed_mental_model_observation')
class _SeedMentalModelObservation(SetupActionHandler):
    """Seed an Entity + MentalModel + Observation for the V21 400-recovery test.

    Params (all optional with documented defaults):
    - ``scenario_token``: short string mixed into every UUIDv5 so two
      scenarios that use this handler stay isolated. Defaults to the
      ``note_key`` so the typical "one seed per source note" case
      Just Works.
    - ``note_key``: source note whose ingested MUs we'll cite as
      evidence. Must already be in the suite's ``sources/`` and
      have produced ≥2 memory units by the time this action runs.
      Default ``'kafka-batching-strategy'``.
    - ``entity_name``: canonical entity name on the seeded
      ``Entity``. Default ``'Kafka Batching Strategy'``.
    - ``observation_title`` / ``observation_content``: payload of the
      seeded Observation. Defaults describe the Kafka batching
      decision so the agent's deprio query can latch onto something
      semantically.
    - ``max_evidence_mus``: cap on how many of the source note's MUs
      we cite as evidence (default 2, the minimum to exercise the
      multi-MU recovery path).
    """

    required: ClassVar[bool] = True
    reusable_under_reuse_vault: ClassVar[bool] = True

    async def run(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        from datetime import datetime, timezone

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlmodel.ext.asyncio.session import AsyncSession

        from memex_core.memory.sql_models import Entity, MentalModel
        from memex_eval.suite.db_reset import _resolve_db_dsn

        note_key = str(params.get('note_key') or 'kafka-batching-strategy')
        scenario_token = str(params.get('scenario_token') or note_key)
        entity_name = str(params.get('entity_name') or 'Kafka Batching Strategy')
        observation_title = str(
            params.get('observation_title')
            or 'Kafka producers use 250ms time-based batching windows'
        )
        observation_content = str(
            params.get('observation_content')
            or (
                'Project Alpha standardised on a 250 ms time-based Kafka batching '
                'window with a 500-message soft cap. Sarah Chen owns the decision '
                'and is running a soak test before promoting the setting to other '
                'producers. The previous size-only batching at 1000 messages '
                'blew the dashboard real-time SLA on low-traffic windows.'
            )
        )
        max_evidence_mus = int(params.get('max_evidence_mus') or 2)

        nk_map: dict[str, list[str]] = params.get('_note_key_to_unit_ids') or {}
        mu_ids_str = nk_map.get(note_key) or []
        if len(mu_ids_str) < 2:
            raise RuntimeError(
                f'seed_mental_model_observation: note_key={note_key!r} resolved to '
                f'{len(mu_ids_str)} memory units; need ≥2. Either the note was not '
                f'ingested, the extractor produced fewer units than expected, or '
                f'the note_key is wrong. Available keys: {sorted(nk_map)[:5]}...'
            )
        source_mu_ids: list[UUID] = [UUID(s) for s in mu_ids_str[:max_evidence_mus]]

        entity_id = _det_uuid('entity', scenario_token, entity_name)
        observation_id = _det_uuid('observation', scenario_token, observation_title)
        mental_model_id = _det_uuid('mental_model', scenario_token, str(entity_id))

        observation_doc: dict[str, Any] = {
            'id': str(observation_id),
            'title': observation_title,
            'content': observation_content,
            'trend': 'new',
            'evidence': [
                {
                    'memory_id': str(mu_id),
                    'quote': None,
                    'relevance': 1.0,
                    'explanation': 'Seeded by agent_integration setup action.',
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                }
                for mu_id in source_mu_ids
            ],
        }

        dsn = _resolve_db_dsn()
        engine = create_async_engine(dsn, future=True)
        try:
            async with AsyncSession(engine) as session:
                entity_stmt = (
                    pg_insert(Entity.__table__)
                    .values(
                        id=entity_id,
                        canonical_name=entity_name,
                        entity_type='Concept',
                        first_seen=datetime.now(timezone.utc),
                        last_seen=datetime.now(timezone.utc),
                        mention_count=max_evidence_mus,
                        retrieval_count=0,
                    )
                    .on_conflict_do_nothing(index_elements=['id'])
                )
                await session.exec(entity_stmt)  # type: ignore[call-overload]

                mm_stmt = (
                    pg_insert(MentalModel.__table__)
                    .values(
                        id=mental_model_id,
                        vault_id=vault_id,
                        entity_id=entity_id,
                        name=entity_name,
                        observations=[observation_doc],
                        entity_metadata={},
                        last_refreshed=datetime.now(timezone.utc),
                        version=1,
                    )
                    .on_conflict_do_nothing(index_elements=['id'])
                )
                await session.exec(mm_stmt)  # type: ignore[call-overload]

                await session.commit()
        finally:
            await engine.dispose()

        logger.info(
            'seeded MentalModel %s (entity=%s) with observation %s citing %d MUs from note_key=%s',
            mental_model_id,
            entity_id,
            observation_id,
            len(source_mu_ids),
            note_key,
        )

        return {
            'observation_id': str(observation_id),
            'source_mu_ids': [str(m) for m in source_mu_ids],
            'entity_id': str(entity_id),
            'mental_model_id': str(mental_model_id),
            'note_key': note_key,
        }

    async def teardown(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
        setup_context: dict[str, Any] | None,
    ) -> None:
        """Delete the seeded Entity + MentalModel.

        Idempotent: relies on the deterministic UUIDs the run() side
        wrote. Failures inside teardown are logged but do not abort
        sibling teardowns (per the framework contract). The schema
        wipe at end-of-run is the ultimate safety net — this
        teardown exists so consecutive scenarios in the same run see
        a clean reflection layer.
        """
        from sqlalchemy import delete
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlmodel.ext.asyncio.session import AsyncSession

        from memex_core.memory.sql_models import Entity, MentalModel
        from memex_eval.suite.db_reset import _resolve_db_dsn

        ctx = setup_context or {}
        mm_id = ctx.get('mental_model_id') or ctx.get(
            'seed_mental_model_observation.mental_model_id'
        )
        ent_id = ctx.get('entity_id') or ctx.get('seed_mental_model_observation.entity_id')
        if not mm_id and not ent_id:
            return

        dsn = _resolve_db_dsn()
        engine = create_async_engine(dsn, future=True)
        try:
            async with AsyncSession(engine) as session:
                if mm_id:
                    await session.exec(
                        delete(MentalModel).where(MentalModel.id == UUID(str(mm_id)))  # type: ignore[arg-type]
                    )
                if ent_id:
                    await session.exec(
                        delete(Entity).where(Entity.id == UUID(str(ent_id)))  # type: ignore[arg-type]
                    )
                await session.commit()
        except Exception as exc:
            logger.warning(
                'seed_mental_model_observation teardown failed (mm=%s, ent=%s): %s. '
                'End-of-run schema wipe will reclaim the rows.',
                mm_id,
                ent_id,
                exc,
            )
        finally:
            await engine.dispose()
