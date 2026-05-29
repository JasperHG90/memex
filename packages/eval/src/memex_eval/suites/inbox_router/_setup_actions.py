"""Suite-private setup action for ``inbox_router``.

``seed_inbox_router_corpus`` builds a tiny labelled routing scenario end-to-end
against a live server, then triggers a dry-run triage and returns the router's
per-note predictions alongside the ground-truth labels for the outcome to score.

Flow:
1. Create two topically-distinct target vaults and ingest a few on-topic notes
   into each (real embeddings via the ingest pipeline).
2. Ensure the inbox vault exists and ingest the labelled notes-to-route.
3. Wait for extraction in every vault so chunks/embeddings/entities are ready.
4. Resolve each inbox note's real UUID (``find_notes_by_title``) — the ingest
   response returns an idempotency hash, not ``notes.id``.
5. Trigger a DRY-RUN triage (``RemoteMemexAPI.trigger_inbox_triage``) so nothing
   is mutated; read the per-note top-candidate predictions.
6. Publish ``{predictions, expected}`` (keyed by note UUID) into the scenario
   context for ``inbox_route_accuracy``.

The handler talks to the server over the HTTP client (the documented setup-action
surface); the router tables are SQLModel models, so the server's create_all DB
already has them and no direct DDL is needed.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from memex_common.schemas import NoteCreateDTO
from memex_eval.helpers import wait_for_extraction
from memex_eval.suite.setup_actions import SetupActionHandler, register_setup_action

if TYPE_CHECKING:
    from memex_common.client import RemoteMemexAPI

logger = logging.getLogger('memex_eval.suites.inbox_router.setup_actions')

# Two clearly-separable topics. Target vaults are prefixed so they don't collide
# with a real operator vault and are easy to clean up.
_COOKING = 'eval-router-cooking'
_GARDENING = 'eval-router-gardening'

_TARGET_CORPUS: dict[str, list[str]] = {
    _COOKING: [
        'Fresh pasta dough: combine 00 flour and eggs, knead ten minutes, rest, '
        'then roll thin and cut into tagliatelle. Salt the boiling water well.',
        'A proper risotto needs warm stock added one ladle at a time, constant '
        'stirring to release the rice starch, and a final beat of butter and parmesan.',
        'To temper chocolate, melt to 45C, cool to 27C over a cold bowl, then warm '
        'back to 31C so it sets glossy and snaps cleanly.',
    ],
    _GARDENING: [
        'Start tomato seeds indoors six weeks before the last frost; harden the '
        'seedlings off gradually before transplanting into rich, well-drained soil.',
        'Prune fruit trees in late winter while dormant: remove crossing branches '
        'and open the canopy so light and air reach the inner growth.',
        'Compost needs a balance of green nitrogen (kitchen scraps) and brown carbon '
        '(dry leaves), turned regularly to keep the pile aerobic and hot.',
    ],
}

# (note_key, body, expected_vault) — each clearly belongs to one target vault.
_INBOX_CORPUS: list[tuple[str, str, str]] = [
    (
        'route-carbonara',
        'How do I make a silky carbonara? Whisk egg yolks with pecorino, toss off the '
        'heat with the hot guanciale fat and a little pasta water so it does not scramble.',
        _COOKING,
    ),
    (
        'route-sourdough',
        'My sourdough starter doubles in four hours now. Best hydration and bulk-'
        'fermentation time for an open crumb when baking in a dutch oven?',
        _COOKING,
    ),
    (
        'route-raised-beds',
        'Planning raised beds for next spring. Companion planting for tomatoes, basil '
        'and marigolds, and how deep the soil should be for good root growth.',
        _GARDENING,
    ),
    (
        'route-pruning',
        'When should I prune my apple tree, and how much of the canopy can I safely '
        'remove in one dormant season without stressing the tree?',
        _GARDENING,
    ),
]

_INBOX_VAULT = 'inbox'


def _b64(text: str) -> bytes:
    return base64.b64encode(text.encode('utf-8'))


@register_setup_action('seed_inbox_router_corpus')
class _SeedInboxRouterCorpus(SetupActionHandler):
    # A seed failure must error the scenario, not be soft-logged.
    required: ClassVar[bool] = True
    # Ingestion + triage are stateful side effects; re-running against a reused
    # vault would double-ingest and bias the predictions.
    reusable_under_reuse_vault: ClassVar[bool] = False

    async def run(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        # 1. Target vaults + on-topic content.
        for vname, bodies in _TARGET_CORPUS.items():
            await self._ensure_vault(api, vname, f'Inbox-router eval target: {vname}')
            for i, body in enumerate(bodies):
                await self._ingest(api, vname, f'{vname}-{i}', body)

        # 2. Inbox vault + labelled notes to route.
        await self._ensure_vault(api, _INBOX_VAULT, 'Inbox-router eval: notes to route')
        for note_key, body, _expected in _INBOX_CORPUS:
            await self._ingest(api, _INBOX_VAULT, note_key, body)

        # 3. Wait for extraction so chunks/embeddings/entities are ready.
        vault_names = [*_TARGET_CORPUS.keys(), _INBOX_VAULT]
        for vname in vault_names:
            vid = await api.resolve_vault_identifier(vname)
            await wait_for_extraction(api, vid, poll_timeout=180.0)

        # 4. Resolve each inbox note's real UUID (ingest returns an idempotency
        #    hash, not notes.id) so predictions and labels share one key space.
        inbox_id = await api.resolve_vault_identifier(_INBOX_VAULT)
        expected: dict[str, str] = {}
        for note_key, _body, expected_vault in _INBOX_CORPUS:
            matches = await api.find_notes_by_title(note_key, vault_ids=[inbox_id], limit=3)
            if not matches:
                raise RuntimeError(f'seed_inbox_router_corpus: inbox note {note_key!r} not found')
            expected[str(matches[0].note_id)] = expected_vault

        # 5. Dry-run triage — score + decide, mutate nothing.
        triage = await api.trigger_inbox_triage(dry_run=True)
        predictions: dict[str, str] = {}
        for d in triage.get('decisions', []):
            note_id = d.get('note_id')
            if note_id is not None:
                predictions[str(note_id)] = d.get('top_vault_name')

        return {'predictions': predictions, 'expected': expected}

    @staticmethod
    async def _ensure_vault(api: 'RemoteMemexAPI', name: str, description: str) -> UUID:
        try:
            return await api.resolve_vault_identifier(name)
        except Exception:  # noqa: BLE001 - not found; fall through to create
            pass
        try:
            v = await api.create_vault(name, description)
            return v.id
        except Exception:  # noqa: BLE001 - created concurrently; resolve again
            return await api.resolve_vault_identifier(name)

    @staticmethod
    async def _ingest(api: 'RemoteMemexAPI', vault_name: str, note_key: str, body: str) -> None:
        vid = await api.resolve_vault_identifier(vault_name)
        dto = NoteCreateDTO(
            name=note_key,
            note_key=f'eval-inbox-router-{note_key}',
            description=f'Inbox-router eval note: {note_key}',
            content=_b64(body),
            files=[],
            tags=[],
            vault_id=str(vid),
        )
        await api.ingest(dto)
