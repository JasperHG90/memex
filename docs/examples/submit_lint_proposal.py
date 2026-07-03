"""Submit a custom lint rule's findings via the typed Python interface.

A lint *rule* is pure metadata that travels with each proposal — there is
no server-side rule registration. You define the rule once by subclassing
``LintRule`` (or build a ``LintProposal`` directly), run your own detection
logic, and submit the findings. A human reviews them in the maintenance
cockpit and picks an action from the closed catalogue.

Run against a local server:

    uv run python docs/examples/submit_lint_proposal.py

Requires the V5 lint-proposals endpoints (PR #212). Set MEMEX_API_KEY if
the server has auth enabled.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from memex_common.client import RemoteMemexAPI
from memex_common.lint import LintProposal, LintRule, ProposedAction

BASE_URL = 'http://localhost:8000/api/v1/'
VAULT = 'global'  # vault UUID or name; required by default


# 1. Define a custom rule as reusable metadata. Malformed metadata
#    (bad slug, unknown lint_type) fails HERE, at class instantiation —
#    not after a round-trip to the server.
class DecommissionedSkillRef(LintRule):
    rule_name: str = 'decommissioned-skill-ref'
    lint_type: str = 'governance'
    description: str = 'Memory unit cites a skill retired in the 2026-05 cleanup.'


async def main() -> None:
    headers = {}
    if api_key := os.environ.get('MEMEX_API_KEY'):
        headers['X-API-Key'] = api_key

    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=30.0) as client:
        api = RemoteMemexAPI(client)

        # 2. (optional) Discover the closed action catalogue so you can pair
        #    your rule with a valid remediation the reviewer sees first.
        catalogue = await api.list_lint_actions()
        unit_actions = [
            a['id'] for a in catalogue['actions'] if 'memory_unit' in a['applicable_target_types']
        ]
        print(f'actions applicable to memory_unit: {unit_actions}')

        # 3. Your detection logic found these offending units. Build one
        #    proposal per finding from the rule's metadata.
        rule = DecommissionedSkillRef()
        offending_unit_ids = ['11111111-1111-1111-1111-111111111111']  # ← your matches

        proposals: list[LintProposal] = [
            rule.build(
                vault_id=VAULT,
                target_type='memory_unit',
                target_id=unit_id,
                suggested_action='Deprioritise the unit; the skill no longer exists.',
                evidence={'skill': 'old-router', 'confidence': 0.97},
                proposed_action=ProposedAction(
                    action_name='deprioritize_unit',
                    params={'reason': 'references decommissioned skill'},
                ),
            )
            for unit_id in offending_unit_ids
        ]

        # 4. Submit the batch. Accepts LintProposal models (shown) or raw
        #    dicts. Partial-success: each item resolves independently.
        result = await api.submit_lint_proposals(proposals)
        for item in result['results']:
            if item['status'] == 'created':
                print(f'[{item["index"]}] filed pending finding {item["finding_id"]}')
            elif item['status'] == 'deduplicated':
                print(f'[{item["index"]}] already open as {item["finding_id"]} (idempotent)')
            elif item['status'] == 'cooldown_suppressed':
                print(f'[{item["index"]}] a human dismissed this recently — not re-filed')
            else:  # rejected
                print(f'[{item["index"]}] rejected: {item["detail"]}')

        # 5. A one-off finding without a reusable rule class: construct a
        #    LintProposal directly (same validation).
        ad_hoc = LintProposal(
            vault_id=VAULT,
            rule_name='stale-kv-convention',
            lint_type='quality',
            target_type='kv',
            target_id='global:deploy:default-target',
            description='KV convention references a deprecated deploy path.',
            suggested_action='Delete the stale KV entry.',
            proposed_action=ProposedAction(action_name='kv_delete'),
        )
        print(await api.submit_lint_proposals([ad_hoc]))


if __name__ == '__main__':
    asyncio.run(main())
