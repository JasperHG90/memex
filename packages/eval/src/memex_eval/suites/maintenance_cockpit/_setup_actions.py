"""Suite-private setup actions for the maintenance_cockpit eval suite.

Provides ``seed_proposals`` — inserts synthetic maintenance_proposals rows
via the ``POST /api/v1/lint/findings/seed`` endpoint so scenarios have
findings to resolve/dismiss/calibrate against without depending on the
lint pipeline to organically produce them (the lint pipeline requires
aged data, embeddings, and LLM API access that eval environments may
not have).

The seed endpoint is gated by ``MEMEX_EVAL_MODE=1`` on the server.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from memex_eval.suite.setup_actions import SetupActionHandler, register_setup_action

logger = logging.getLogger(__name__)


@register_setup_action('seed_proposals')
class SeedProposals(SetupActionHandler):
    """Insert N synthetic maintenance_proposals rows via the HTTP API.

    Params:
        count (int): number of proposals to seed (default 10).
        rule_name (str): rule_name for all rows (default 'llm_semantic_contradiction').
        source (str): 'llm' or 'rule' (default 'llm').
    """

    required = True
    reusable_under_reuse_vault = False

    async def run(self, api: Any, vault_id: UUID, params: dict[str, Any]) -> dict[str, Any] | None:
        count = int(params.get('count', 10))
        rule_name = str(params.get('rule_name', 'llm_semantic_contradiction'))
        source = str(params.get('source', 'llm'))

        finding_ids: list[str] = []
        for i in range(count):
            evidence = {
                'check_type': 'semantic_contradiction',
                'explanation': f'Synthetic finding #{i + 1} for eval suite.',
                'surprise_score': 0.5 + (i % 5) * 0.1,
                'related_unit_ids': [str(uuid4())],
            }
            try:
                result = await api.lint_seed_finding(
                    vault_id=vault_id,
                    rule_name=rule_name,
                    source=source,
                    evidence=evidence,
                    suggested_action=f'Eval suite seed #{i + 1}',
                )
                finding_ids.append(result['id'])
            except Exception:
                logger.exception('Failed to seed proposal %d', i)

        logger.info(
            'Seeded %d proposals for rule %s in vault %s',
            len(finding_ids),
            rule_name,
            vault_id,
        )
        return {'finding_ids': finding_ids}
