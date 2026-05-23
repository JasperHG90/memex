"""Data layer behind the Textual cockpit.

The app calls into `CockpitController` for fetches and verdicts;
`CockpitController` wraps a `MemexClient`-shaped object. The controller is
deliberately UI-agnostic so it stays unit-testable without a Textual `Pilot`.

The mapping of rule_name → canned followup options lives here as a static
dict — for MVP, the server does not yet emit `evidence.proposed_followups`.
When the server starts populating that field, this controller will prefer
evidence-carried options and fall back to the static dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CockpitOption:
    """A single canned remediation the cockpit can present to the user."""

    action_id: str  # registered proposal_actions id; '' for dismiss/no_action sentinel
    label: str
    summary: str
    effect: str
    reversible: bool
    recommended: bool = False
    # When True the option always demands a reviewer note (used for LLM-custom
    # suggestions we haven't yet implemented — for MVP this is unset).
    requires_review: bool = False
    # Verb the controller sends to the server: 'resolve' or 'dismiss'.
    verb: str = 'resolve'


# Sentinel option_id for "Dismiss" so the menu can present it uniformly.
DISMISS_OPTION = CockpitOption(
    action_id='',
    label='Dismiss — not actionable',
    summary='Mark this proposal dismissed without running a remediation.',
    effect='Flips status to dismissed. The reviewer note (if any) is recorded.',
    reversible=False,
    verb='dismiss',
)


# Per-rule canned options. MVP fallback for when the server has not yet
# emitted ``evidence.proposed_followups``. Recommended option is marked.
_DEFAULT_OPTIONS_BY_RULE: dict[str, list[CockpitOption]] = {
    'orphan_mental_model': [
        CockpitOption(
            action_id='archive_mental_model',
            label='Archive the mental model',
            summary='Set archived_at on this mental model.',
            effect='Hidden from retrieval, survey, and reflection. Row content kept.',
            reversible=True,
            recommended=True,
        ),
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — keep active',
            summary='Record review; leave the model in place.',
            effect='No mutation; the orphan signal persists until conditions change.',
            reversible=True,
        ),
        DISMISS_OPTION,
    ],
    'cold_low_mw_unit': [
        CockpitOption(
            action_id='deprioritize_unit',
            label='Deprioritize the unit',
            summary='Suppress this unit from retrieval; refresh citing observations.',
            effect=(
                'Sets is_deprioritized=true; queue refresh on every observation citing the unit.'
            ),
            reversible=True,
            recommended=True,
        ),
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — keep visible',
            summary='Record review; leave the unit retrievable.',
            effect='No mutation; the cold-low-MW signal persists.',
            reversible=True,
        ),
        DISMISS_OPTION,
    ],
    'composite_deprioritize_candidate': [
        CockpitOption(
            action_id='deprioritize_unit',
            label='Deprioritize the unit',
            summary='Suppress the candidate from retrieval; refresh citing observations.',
            effect='Sets is_deprioritized=true; observation refresh queued.',
            reversible=True,
            recommended=True,
        ),
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — composite score is fine',
            summary='Record review; do not deprioritize.',
            effect='No mutation.',
            reversible=True,
        ),
        DISMISS_OPTION,
    ],
    'llm_semantic_contradiction': [
        CockpitOption(
            action_id='deprioritize_unit',
            label='Deprioritize the contradicting unit',
            summary='Suppress this unit; the related units in evidence remain.',
            effect=(
                'Sets is_deprioritized=true on the target; observation refresh '
                'queued. Review evidence.related_unit_ids before choosing.'
            ),
            reversible=True,
            recommended=True,
        ),
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — not a real contradiction',
            summary='Record review; both sides stay active.',
            effect='No mutation; the contradiction signal persists in audit.',
            reversible=True,
        ),
        DISMISS_OPTION,
    ],
    'llm_schema_drift': [
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — intentional schema variation',
            summary='Record review; the unit stays as-is.',
            effect='No mutation; the schema-drift signal persists in audit.',
            reversible=True,
            recommended=True,
        ),
        CockpitOption(
            action_id='deprioritize_unit',
            label='Deprioritize the off-format unit',
            summary='Suppress until re-ingested in the corpus-norm format.',
            effect='Sets is_deprioritized=true; observation refresh queued.',
            reversible=True,
        ),
        DISMISS_OPTION,
    ],
    'claim_too_aggressive': [
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — keep the unit',
            summary='Record review; treat as a tuning signal.',
            effect='No mutation.',
            reversible=True,
            recommended=True,
        ),
        CockpitOption(
            action_id='deprioritize_unit',
            label='Deprioritize the over-matching unit',
            summary='Suppress the aggressive claim.',
            effect='Sets is_deprioritized=true; observation refresh queued.',
            reversible=True,
        ),
        DISMISS_OPTION,
    ],
    'sensitive_unreviewed_unit': [
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — review done',
            summary='Record human review; counter resets at the next sweep.',
            effect='No mutation; the sensitive flag stays.',
            reversible=True,
            recommended=True,
        ),
        CockpitOption(
            action_id='deprioritize_unit',
            label='Deprioritize the sensitive unit',
            summary='Suppress retrieval while you escalate further.',
            effect='Sets is_deprioritized=true; observation refresh queued.',
            reversible=True,
        ),
        DISMISS_OPTION,
    ],
    'dangling_entity_ref_in_unit': [
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — auto-reaper will clean up',
            summary='Recorded so the human-review counter advances.',
            effect='No mutation; the dangling-ref reaper still runs.',
            reversible=True,
            recommended=True,
        ),
        DISMISS_OPTION,
    ],
    'orphan_contradicts_links_post_stale': [
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — orphan-link reaper will clean up',
            summary='Recorded so the human-review counter advances.',
            effect='No mutation; the orphan-link reaper still runs.',
            reversible=True,
            recommended=True,
        ),
        DISMISS_OPTION,
    ],
    # entity_collapse_cluster and propose_contradiction_winner still flow
    # through their dedicated endpoints (`memex lint resolve --winner …` and
    # `memex lint apply <id>`) — the cockpit refuses to canned-resolve them
    # to avoid silently bypassing the cluster collapse or the winner action.
    # We surface that explicitly via a sentinel option that the cockpit will
    # render as advisory text only (action_id stays empty, label tells the
    # human to drop to the CLI).
    'entity_collapse_cluster': [
        CockpitOption(
            action_id='',
            label='Cluster collapse — drop to `memex lint resolve --winner`',
            summary='Cluster collapses require a winner UUID; the cockpit cannot pick one.',
            effect='No mutation from the cockpit.',
            reversible=False,
            verb='dismiss',  # treat any pick as a dismiss with note
        ),
    ],
    'propose_contradiction_winner': [
        CockpitOption(
            action_id='',
            label='Winner proposal — drop to `memex lint apply <id>`',
            summary='Winner proposals carry a recorded action only `lint apply` can execute.',
            effect='No mutation from the cockpit.',
            reversible=False,
            verb='dismiss',
        ),
    ],
}


# Client-side action catalogue: action_id → (label, description, applicable_target_types, reversible).
# Inlined here on purpose — importing memex_core.services to read the live
# registry transitively loads onnxruntime via SearchService / reranker, which
# adds ~1.5s to every cockpit launch for information the cockpit already knows.
# Adding a new server-side action means adding a row here too; the integration
# tests in packages/core/tests/integration cover the round-trip end to end.
_ACTION_CATALOGUE: dict[str, tuple[str, str, tuple[str, ...], bool]] = {
    'no_op': (
        'No-op (record only)',
        'Record the verdict and any reviewer note without touching the target.',
        ('memory_unit', 'mental_model', 'note', 'unit_entity'),
        True,
    ),
    'deprioritize_unit': (
        'Deprioritize unit',
        'Suppress the unit from retrieval; refresh citing observations.',
        ('memory_unit',),
        True,
    ),
    'restore_unit': (
        'Restore unit',
        'Clear is_deprioritized; the unit becomes retrievable again.',
        ('memory_unit',),
        True,
    ),
    'archive_mental_model': (
        'Archive mental model',
        'Hide the mental model from retrieval / briefing / reflection.',
        ('mental_model',),
        True,
    ),
}


def options_for_rule(rule_name: str, target_type: str) -> list[CockpitOption]:
    """Return the cockpit options for a given rule.

    Falls back to ``[no_op, dismiss]`` for unknown rules so the cockpit
    always offers a verdict path. Options are filtered to those whose
    ``action_id`` is empty (dismiss) or compatible with the target_type
    per the client-side action catalogue above.
    """
    options = _DEFAULT_OPTIONS_BY_RULE.get(rule_name)
    if options is None:
        options = [
            CockpitOption(
                action_id='no_op',
                label='Acknowledge — record review',
                summary='Mark resolved without remediation.',
                effect='No mutation.',
                reversible=True,
                recommended=True,
            ),
            DISMISS_OPTION,
        ]
    filtered: list[CockpitOption] = []
    for option in options:
        if not option.action_id:  # dismiss sentinel always allowed
            filtered.append(option)
            continue
        descriptor = _ACTION_CATALOGUE.get(option.action_id)
        if descriptor is None:
            continue  # unknown action — drop defensively
        _, _, applicable_types, _ = descriptor
        if target_type in applicable_types:
            filtered.append(option)
    return filtered


def custom_action_options(target_type: str) -> list[CockpitOption]:
    """Return the full action catalogue filtered to this target_type — used by [O]ther.

    The user picks one of these to map a free-form intent onto an executable
    canned action.
    """
    options: list[CockpitOption] = []
    for action_id, (label, description, applicable_types, reversible) in _ACTION_CATALOGUE.items():
        if target_type not in applicable_types:
            continue
        options.append(
            CockpitOption(
                action_id=action_id,
                label=label,
                summary=description,
                effect='',
                reversible=reversible,
            )
        )
    return options


@dataclass
class CockpitProposal:
    """View-model for a maintenance proposal in the cockpit.

    Built from the raw `lint_findings` response dict. The TUI never reaches
    back into the raw shape; everything it needs is on this dataclass.
    """

    finding_id: str
    rule_name: str
    lint_type: str
    target_type: str
    target_id: str
    target_text: str | None
    vault_id: str | None
    created_at: str | None
    source: str  # 'rule' or 'llm'
    explanation: str | None
    surprise_score: float | None
    related_unit_ids: list[str]
    polarity_contradiction_prob: float | None
    suggested_action: str | None
    raw_evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_finding(cls, finding: dict[str, Any]) -> CockpitProposal:
        evidence = finding.get('evidence') or {}
        if not isinstance(evidence, dict):
            evidence = {}
        rel = evidence.get('related_unit_ids') or []
        if not isinstance(rel, list):
            rel = []
        polarity = evidence.get('polarity_contradiction_prob')
        if polarity is not None:
            try:
                polarity = float(polarity)
            except (TypeError, ValueError):
                polarity = None
        return cls(
            finding_id=str(finding.get('id') or ''),
            rule_name=str(finding.get('rule_name') or ''),
            lint_type=str(finding.get('lint_type') or ''),
            target_type=str(finding.get('target_type') or ''),
            target_id=str(finding.get('target_id') or ''),
            target_text=finding.get('target_text'),
            vault_id=finding.get('vault_id'),
            created_at=finding.get('created_at'),
            source=str(finding.get('source') or 'rule'),
            explanation=evidence.get('explanation'),
            surprise_score=evidence.get('surprise_score'),
            related_unit_ids=[str(r) for r in rel],
            polarity_contradiction_prob=polarity,
            suggested_action=finding.get('suggested_action'),
            raw_evidence=evidence,
        )

    @property
    def is_llm_source(self) -> bool:
        return self.source == 'llm'


class CockpitClient(Protocol):
    """Subset of `MemexClient` the cockpit depends on.

    Declared as a Protocol so unit tests can pass a fake without dragging in
    HTTP infrastructure. The real client (`memex_common.client.MemexClient`)
    satisfies this surface naturally.
    """

    async def lint_findings(
        self,
        *,
        vault_id: str | None = None,
        lint_type: str | None = None,
        status: str = 'pending',
        limit: int = 50,
    ) -> dict[str, Any]: ...

    async def lint_resolve(
        self,
        finding_id: str,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]: ...

    async def lint_dismiss(
        self,
        finding_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]: ...

    async def lint_reverse(self, finding_id: str) -> dict[str, Any]: ...


class CockpitController:
    """Async wrapper around the HTTP client used by the TUI."""

    def __init__(self, client: CockpitClient, *, vault_id: str | None = None) -> None:
        self._client = client
        self._vault_id = vault_id

    async def fetch_pending(self, *, limit: int = 50) -> list[CockpitProposal]:
        """Fetch pending proposals, LLM-first then rule, newest first within tier."""
        payload = await self._client.lint_findings(
            vault_id=self._vault_id,
            status='pending',
            limit=limit,
        )
        findings = payload.get('findings') or []
        proposals = [CockpitProposal.from_finding(f) for f in findings]
        proposals.sort(key=_sort_key)
        return proposals

    async def resolve(
        self,
        proposal: CockpitProposal,
        option: CockpitOption,
        *,
        note: str | None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute the chosen option against this proposal."""
        if option.verb == 'dismiss':
            return await self._client.lint_dismiss(proposal.finding_id, note=note)
        action_id = option.action_id or 'no_op'
        return await self._client.lint_resolve(
            proposal.finding_id,
            action=action_id,
            params=params,
            note=note,
        )

    async def reverse(self, finding_id: str) -> dict[str, Any]:
        return await self._client.lint_reverse(finding_id)


def _sort_key(p: CockpitProposal) -> tuple[int, float]:
    """Sort LLM-source proposals first, then rule; then newest-first within tier.

    The second axis is the proposal's `created_at` epoch negated so a
    plain ascending sort yields newest-first. Parsing failures fall back
    to 0.0 (sorts as "oldest possible") to keep malformed rows at the
    bottom rather than at the top.
    """
    from datetime import datetime

    tier = 0 if p.is_llm_source else 1
    created = p.created_at or ''
    if not created:
        return (tier, 0.0)
    # Normalise the trailing 'Z' shape so fromisoformat accepts it on 3.10+.
    iso = created.replace('Z', '+00:00')
    try:
        ts = datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        ts = 0.0
    return (tier, -ts)
