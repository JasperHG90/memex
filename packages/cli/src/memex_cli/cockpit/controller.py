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

from dataclasses import dataclass, field, replace
from typing import Any, Protocol


@dataclass(frozen=True)
class UnitMeta:
    """Lightweight metadata for a memory unit, ready for TUI display."""

    unit_id: str
    date: str | None = None  # ISO date string from mentioned_at
    note_name: str | None = None  # source note title
    status: str | None = None


@dataclass(frozen=True)
class UnitDetail:
    """Rich detail view for a single memory unit in the DETAIL drill-down."""

    unit_id: str
    text: str
    status: str | None = None
    created_at: str | None = None
    note_id: str | None = None
    note_key: str | None = None
    note_created_at: str | None = None
    chunk_index: str | None = None  # e.g. "page 3 of 7"
    entities: list[str] = field(default_factory=list)
    fact_type: str | None = None
    confidence: float | None = None
    is_deprioritized: bool = False


@dataclass(frozen=True)
class UnitLineage:
    """Upstream and downstream lineage summary for a memory unit."""

    upstream: list[tuple[str, str]] = field(default_factory=list)  # (unit_id, label)
    downstream: list[tuple[str, str]] = field(default_factory=list)  # (unit_id, label)


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
    # Extra params forwarded to the server (e.g. override_target_id for
    # deprioritizing the RELATED unit instead of the TARGET).
    params: dict[str, Any] | None = None


# Sentinel option for "Flag for later" — orthogonal to resolution.
FLAG_OPTION = CockpitOption(
    action_id='__flag__',
    label='Flag for later',
    summary=(
        'Bookmark this finding so you can easily find it later. '
        'Flagging does NOT resolve or dismiss — it is a personal bookmark '
        'orthogonal to the finding lifecycle.'
    ),
    effect='No mutation. Toggles the flagged_at timestamp.',
    reversible=True,
    verb='flag',
)

# Sentinel option_id for "Dismiss" so the menu can present it uniformly.
DISMISS_OPTION = CockpitOption(
    action_id='',
    label='Dismiss — this finding is wrong / noise',
    summary=(
        'You think the linter was wrong to flag this. Status flips to dismissed; '
        'nothing mutates. The audit trail records your verdict so the distinction '
        'between "wrong finding" and "valid finding, no action" is preserved.'
    ),
    effect='No mutation. Status flips to dismissed.',
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
        FLAG_OPTION,
        DISMISS_OPTION,
    ],
    'inbox_vault_no_fit': [
        CockpitOption(
            action_id='no_op',
            label='Leave in inbox',
            summary='No vault fits this note; keep it in the inbox for now.',
            effect='No mutation; the router backs off and re-evaluates later.',
            reversible=True,
            recommended=True,
        ),
        FLAG_OPTION,
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
        FLAG_OPTION,
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
        FLAG_OPTION,
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
        FLAG_OPTION,
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
        FLAG_OPTION,
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
        FLAG_OPTION,
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
        FLAG_OPTION,
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
        FLAG_OPTION,
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
        FLAG_OPTION,
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
# This static dict is the OFFLINE FALLBACK; ``refresh_action_catalogue`` swaps
# in the server's live registry (GET /lint/actions) at cockpit startup so new
# server-side actions appear without a CLI release. The integration tests in
# packages/core/tests/integration cover the round-trip end to end.
_ACTION_CATALOGUE: dict[str, tuple[str, str, tuple[str, ...], bool]] = {
    'no_op': (
        'No-op (record only)',
        'Record the verdict and any reviewer note without touching the target.',
        ('memory_unit', 'mental_model', 'note', 'unit_entity', 'entity', 'kv'),
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
    'route_note_to_vault': (
        'Route note to vault',
        'Migrate the note to params.target_vault_id; undo migrates it back.',
        ('note',),
        True,
    ),
    'set_note_status': (
        'Set note status',
        "Lifecycle transition: 'superseded' stales units, 'archived' deprioritizes, "
        "'active' reactivates.",
        ('note',),
        True,
    ),
    'update_note_title': (
        'Update note title',
        'Replace the note title; embedded title facts re-extract.',
        ('note',),
        True,
    ),
    'update_note_date': (
        'Update note date',
        'Replace the publish date; child unit timestamps cascade.',
        ('note',),
        True,
    ),
    'merge_entities': (
        'Merge entities into winner',
        'Fold member entities onto the chosen winner; losers hard-deleted. NOT reversible.',
        ('entity',),
        False,
    ),
    'collapse_into_new_entity': (
        'Collapse into a new entity',
        'Create a new entity and fold ALL members onto it; originals hard-deleted. NOT reversible.',
        ('entity',),
        False,
    ),
    'kv_delete': (
        'Delete KV entry',
        'Hard-delete the KV entry including procedure history. NOT reversible.',
        ('kv',),
        False,
    ),
    'record_outcome': (
        'Record outcome on unit',
        'Append helpful / not_helpful / not_used to the Memory-Worth ledger. NOT reversible.',
        ('memory_unit',),
        False,
    ),
    'delete_note': (
        'Delete note (permanent)',
        'Hard-delete the note + units + chunks + assets. NOT reversible.',
        ('note',),
        False,
    ),
    'delete_entity': (
        'Delete entity (permanent)',
        'Hard-delete the entity + mental models + aliases + links. NOT reversible.',
        ('entity',),
        False,
    ),
    'delete_mental_model': (
        'Delete mental model (permanent)',
        "Hard-delete this vault's mental model; the entity is untouched. NOT reversible.",
        ('entity',),
        False,
    ),
}

# action_id → params JSON schema (or None), populated alongside the catalogue
# by ``refresh_action_catalogue``. Static fallback carries no schemas — the
# review flows degrade to free-form params when offline.
_ACTION_PARAMS_SCHEMA: dict[str, dict[str, Any] | None] = {}


def refresh_action_catalogue(descriptors: list[dict[str, Any]]) -> None:
    """Replace the client-side catalogue with the server's live registry.

    ``descriptors`` is the ``actions`` list from ``GET /lint/actions``.
    Malformed entries are skipped defensively; an empty list is a no-op so
    a degenerate server response can never blank the menus.
    """
    parsed: dict[str, tuple[str, str, tuple[str, ...], bool]] = {}
    schemas: dict[str, dict[str, Any] | None] = {}
    for entry in descriptors:
        if not isinstance(entry, dict):
            continue
        action_id = str(entry.get('id') or '')
        if not action_id:
            continue
        parsed[action_id] = (
            str(entry.get('name') or action_id),
            str(entry.get('description') or ''),
            tuple(str(t) for t in (entry.get('applicable_target_types') or ())),
            bool(entry.get('reversible')),
        )
        schema = entry.get('params_schema')
        schemas[action_id] = schema if isinstance(schema, dict) else None
    if not parsed:
        return
    _ACTION_CATALOGUE.clear()
    _ACTION_CATALOGUE.update(parsed)
    _ACTION_PARAMS_SCHEMA.clear()
    _ACTION_PARAMS_SCHEMA.update(schemas)


def action_params_schema(action_id: str) -> dict[str, Any] | None:
    """JSON schema for an action's params, when the live catalogue carried one."""
    return _ACTION_PARAMS_SCHEMA.get(action_id)


def action_is_reversible(action_id: str) -> bool | None:
    """Reversibility per the catalogue; None when the action is unknown."""
    descriptor = _ACTION_CATALOGUE.get(action_id)
    return descriptor[3] if descriptor else None


def options_for_contradiction(
    proposal: CockpitProposal,
) -> list[CockpitOption]:
    """Generate per-unit deprioritize options for a semantic contradiction.

    The user sees which exact unit (by short ID) will be deprioritized,
    and can pick either side.
    """
    target_short = proposal.target_id[:8]
    options: list[CockpitOption] = [
        CockpitOption(
            action_id='deprioritize_unit',
            label=f'Deprioritize TARGET ({target_short})',
            summary=f'Suppress TARGET {target_short}; related units stay active.',
            effect='Sets is_deprioritized=true on the target unit.',
            reversible=True,
            recommended=True,
        ),
    ]
    if proposal.related_unit_ids:
        related_short = proposal.related_unit_ids[0][:8]
        options.append(
            CockpitOption(
                action_id='deprioritize_unit',
                label=f'Deprioritize RELATED ({related_short})',
                summary=f'Suppress RELATED {related_short}; target stays active.',
                effect='Sets is_deprioritized=true on the related unit.',
                reversible=True,
                params={'override_target_id': proposal.related_unit_ids[0]},
            ),
        )
    options.append(
        CockpitOption(
            action_id='no_op',
            label='Acknowledge — not a real contradiction',
            summary='Record review; both sides stay active.',
            effect='No mutation; the contradiction signal persists in audit.',
            reversible=True,
        ),
    )
    options.append(FLAG_OPTION)
    options.append(DISMISS_OPTION)
    return options


def options_for_inbox_route(proposal: CockpitProposal) -> list[CockpitOption]:
    """Build one route option per candidate vault from ``evidence.top_candidates``.

    The router proposes the top-K vaults; the user picks which (or dismisses).
    Each option carries ``params.target_vault_id`` so ``route_note_to_vault``
    knows where to migrate the note.
    """
    candidates = proposal.raw_evidence.get('top_candidates') or []
    valid = [c for c in candidates if isinstance(c, dict) and c.get('vault_id')]
    all_vault_ids = [str(c['vault_id']) for c in valid]
    options: list[CockpitOption] = []
    for i, cand in enumerate(valid):
        vault_id = str(cand['vault_id'])
        vault_name = cand.get('vault_name') or vault_id
        p_match = cand.get('p_match')
        try:
            p_str = f'{float(p_match):.2f}' if p_match is not None else '?'
        except (TypeError, ValueError):
            p_str = '?'
        # Pass the other candidates so the router can record them as negatives —
        # a human confirmation is the same learning signal as an auto-route.
        other_vault_ids = [v for v in all_vault_ids if v != vault_id]
        options.append(
            CockpitOption(
                action_id='route_note_to_vault',
                label=f'Route to {vault_name} (p={p_str})',
                summary=f'Migrate this note from the inbox to {vault_name}.',
                effect='Moves the note + its units, chunks, and links to the target vault.',
                reversible=True,
                recommended=(i == 0),
                params={'target_vault_id': vault_id, 'other_vault_ids': other_vault_ids},
            )
        )
    options.append(
        CockpitOption(
            action_id='no_op',
            label='Leave in inbox',
            summary='Record review; the note stays in the inbox vault.',
            effect='No mutation; the router may re-propose at the next tick.',
            reversible=True,
        )
    )
    options.append(FLAG_OPTION)
    options.append(DISMISS_OPTION)
    return options


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
                label='Mark reviewed (no mutation)',
                summary=(
                    'You looked at this proposal and decided no state change is '
                    'needed. Status flips to resolved; the linter may re-emit if '
                    'the condition still holds at the next sweep.'
                ),
                effect='No mutation. Status flips to resolved.',
                reversible=True,
                recommended=True,
            ),
            FLAG_OPTION,
            DISMISS_OPTION,
        ]
    filtered: list[CockpitOption] = []
    for option in options:
        if not option.action_id or option.action_id == '__flag__':
            # dismiss and flag sentinels are always allowed
            filtered.append(option)
            continue
        descriptor = _ACTION_CATALOGUE.get(option.action_id)
        if descriptor is None:
            continue  # unknown action — drop defensively
        _, _, applicable_types, _ = descriptor
        if target_type in applicable_types:
            filtered.append(option)
    return filtered


def proposed_action_option(proposal: CockpitProposal) -> CockpitOption | None:
    """The submitter's suggested action, when an external proposal carries one.

    External proposals stamp ``evidence.proposed_action = {action_name,
    params}`` at submission (already validated server-side against the
    registry). Surface it as the recommended option so the reviewer sees the
    submitter's intent first — but it is advisory: every other option stays
    available.
    """
    suggestion = proposal.raw_evidence.get('proposed_action')
    if not isinstance(suggestion, dict):
        return None
    action_id = str(suggestion.get('action_name') or '')
    descriptor = _ACTION_CATALOGUE.get(action_id)
    if descriptor is None:
        return None
    label, description, applicable_types, reversible = descriptor
    if proposal.target_type not in applicable_types:
        return None
    params = suggestion.get('params')
    return CockpitOption(
        action_id=action_id,
        label=f'{label} (suggested by submitter)',
        summary=description,
        effect='Executes the catalogue action with the submitter-supplied params.',
        reversible=reversible,
        recommended=True,
        params=dict(params) if isinstance(params, dict) else None,
    )


def options_for_proposal(proposal: CockpitProposal) -> list[CockpitOption]:
    """Dispatch to the correct option builder for a proposal's rule/target.

    Single source of truth for both the single-detail panel and batch
    resolution so the two never drift. The batch path historically called only
    ``options_for_rule`` and therefore never offered the dynamically-built
    inbox-route / contradiction actions (which carry per-proposal ``params``),
    so a batch could never actually route a note.

    An external proposal's ``evidence.proposed_action`` is prepended as the
    recommended option; rule defaults follow (de-duplicated, demoted to
    non-recommended so the submitter's suggestion wins batch-accept).
    """
    if proposal.rule_name == 'llm_semantic_contradiction':
        base = options_for_contradiction(proposal)
    elif proposal.rule_name == 'inbox_vault_route':
        base = options_for_inbox_route(proposal)
    else:
        base = options_for_rule(proposal.rule_name, proposal.target_type)
    suggested = proposed_action_option(proposal)
    if suggested is None:
        return base
    rest: list[CockpitOption] = []
    for option in base:
        if option.action_id == suggested.action_id and option.params == suggested.params:
            continue
        if option.recommended:
            option = replace(option, recommended=False)
        rest.append(option)
    return [suggested, *rest]


def recommended_resolve_option(proposal: CockpitProposal) -> CockpitOption | None:
    """The option a per-proposal batch 'accept' applies to one proposal.

    Prefers an explicitly ``recommended`` resolve option — which carries any
    proposal-specific ``params`` such as ``target_vault_id`` for an inbox route
    — and falls back to the first concrete resolve action. Returns ``None`` when
    the proposal exposes no actionable resolve (only flag/dismiss), so the
    caller can skip it rather than silently no-op.
    """
    resolves = [o for o in options_for_proposal(proposal) if o.verb == 'resolve']
    for option in resolves:
        if option.recommended:
            return option
    return resolves[0] if resolves else None


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
    target_label: str | None
    vault_id: str | None
    created_at: str | None
    source: str  # 'rule' or 'llm'
    explanation: str | None
    surprise_score: float | None
    related_unit_ids: list[str]
    polarity_contradiction_prob: float | None
    suggested_action: str | None
    flagged_at: str | None
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
            target_label=finding.get('target_label'),
            vault_id=finding.get('vault_id'),
            created_at=finding.get('created_at'),
            source=str(finding.get('source') or 'rule'),
            explanation=evidence.get('explanation'),
            surprise_score=evidence.get('surprise_score'),
            related_unit_ids=[str(r) for r in rel],
            polarity_contradiction_prob=polarity,
            suggested_action=finding.get('suggested_action'),
            flagged_at=finding.get('flagged_at'),
            raw_evidence=evidence,
        )

    @property
    def target_display(self) -> str:
        """Compact human-readable target for queue / detail rows.

        Prefers the server-resolved ``target_label`` (note title / entity /
        mental-model name), then evidence-embedded names, then a unit-text
        snippet, and finally a truncated ``target_id`` — so the reviewer never
        stares at a bare UUID.
        """
        label: str | None = self.target_label
        if not label and isinstance(self.raw_evidence, dict):
            names = self.raw_evidence.get('member_canonical_names') or self.raw_evidence.get(
                'canonical_names'
            )
            if isinstance(names, dict):
                names = list(names.values())
            if isinstance(names, list) and names:
                # Cap to a few names + "+N more" so a many-member cluster reads
                # as a deliberate summary, not a mid-name truncation.
                shown = ', '.join(str(n) for n in names[:3])
                label = shown if len(names) <= 3 else f'{shown} +{len(names) - 3} more'
            else:
                entity_name = self.raw_evidence.get('entity_name')
                label = str(entity_name) if entity_name else None
        if not label:
            label = self.target_text
        if not label:
            return f'{self.target_id[:8]}…'
        collapsed = ' '.join(label.split())
        return collapsed if len(collapsed) <= 56 else collapsed[:55] + '…'

    @property
    def is_llm_source(self) -> bool:
        return self.source == 'llm'

    @property
    def is_flagged(self) -> bool:
        return self.flagged_at is not None


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
        legacy_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def lint_dismiss(
        self,
        finding_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]: ...

    async def lint_reverse(self, finding_id: str) -> dict[str, Any]: ...

    async def lint_flag(self, finding_id: str) -> dict[str, Any]: ...

    async def list_lint_actions(self) -> dict[str, Any]: ...

    async def lint_preview_action(
        self,
        finding_id: str,
        *,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def get_memory_unit(self, unit_id: str) -> Any: ...

    async def get_note(self, note_id: Any) -> Any: ...

    async def get_note_page_index(self, note_id: Any) -> Any: ...

    async def get_lineage(
        self,
        entity_type: str,
        entity_id: Any,
        direction: Any = ...,
        depth: int = 3,
        limit: int = 10,
    ) -> Any: ...

    async def list_vaults(self) -> Any: ...


class CockpitController:
    """Async wrapper around the HTTP client used by the TUI."""

    def __init__(self, client: CockpitClient, *, vault_id: str | None = None) -> None:
        self._client = client
        self._vault_id = vault_id
        self._vault_name_cache: dict[str, str] = {}
        self._catalogue_loaded = False

    async def resolve_vault_name(self, vault_id: str) -> str:
        """Resolve a vault UUID to its human-readable name.

        Results are cached for the lifetime of the controller so that the
        vault list is fetched at most once per session.
        """
        if vault_id in self._vault_name_cache:
            return self._vault_name_cache[vault_id]
        try:
            vaults = await self._client.list_vaults()
            for v in vaults:
                vid = str(getattr(v, 'id', ''))
                vname = str(getattr(v, 'name', ''))
                self._vault_name_cache[vid] = vname
        except Exception:  # noqa: BLE001
            pass  # fall through — return truncated UUID below
        return self._vault_name_cache.get(vault_id, vault_id[:8])

    async def fetch_pending(self, *, limit: int = 50) -> list[CockpitProposal]:
        """Fetch pending proposals, LLM-first then rule, newest first within tier."""
        # First fetch also swaps the static action catalogue for the server's
        # live registry, so the menus track new server-side actions.
        await self.load_action_catalogue()
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

    async def apply_entity_collapse(
        self,
        finding_id: str,
        *,
        winner_id: str,
        member_ids: list[str],
        note: str | None = None,
    ) -> dict[str, Any]:
        """Apply an entity-cluster collapse with a chosen winner + member subset.

        Uses the server's ``entity_collapse_cluster`` carveout (no canned
        ``action``; ``winner_id`` / ``member_ids`` are read from the top-level
        body, so they go through ``legacy_params``). Only ``member_ids`` are
        merged into ``winner_id`` — deselected members stay separate.
        """
        return await self._client.lint_resolve(
            finding_id,
            action=None,
            note=note,
            legacy_params={'winner_id': winner_id, 'member_ids': member_ids},
        )

    async def reverse(self, finding_id: str) -> dict[str, Any]:
        return await self._client.lint_reverse(finding_id)

    async def flag_finding(self, finding_id: str) -> dict[str, Any]:
        """Toggle the flagged_at bookmark on a finding."""
        return await self._client.lint_flag(finding_id)

    async def load_action_catalogue(self) -> None:
        """Swap the static action catalogue for the server's live registry.

        Idempotent per controller; failures keep the static fallback so the
        cockpit stays usable against older servers.
        """
        if self._catalogue_loaded:
            return
        self._catalogue_loaded = True
        try:
            payload = await self._client.list_lint_actions()
        except Exception:  # noqa: BLE001 - offline fallback is the contract
            return
        actions = payload.get('actions') if isinstance(payload, dict) else None
        if isinstance(actions, list):
            refresh_action_catalogue(actions)

    async def preview_action(
        self,
        finding_id: str,
        *,
        action_id: str,
        params: dict[str, Any] | None = None,
    ) -> str | None:
        """Server-side blast-radius preview; None when unavailable."""
        try:
            payload = await self._client.lint_preview_action(
                finding_id, action=action_id, params=params
            )
        except Exception:  # noqa: BLE001 - preview is advisory, never blocking
            return None
        preview = payload.get('preview') if isinstance(payload, dict) else None
        return str(preview) if preview else None

    async def fetch_unit_texts(self, unit_ids: list[str]) -> dict[str, str]:
        """Fetch the text body of one or more memory units by ID.

        Returns a mapping of ``{unit_id: text}``.  Units that fail to
        load (network error, 404, missing method on the client) are
        silently dropped so the caller always gets a partial result
        rather than an exception.
        """
        result: dict[str, str] = {}
        for uid in unit_ids:
            try:
                unit = await self._client.get_memory_unit(uid)
                text = getattr(unit, 'text', None)
                if isinstance(text, str):
                    result[uid] = text
            except Exception:  # noqa: BLE001
                continue
        return result

    async def fetch_unit_metadata(self, unit_ids: list[str]) -> dict[str, UnitMeta]:
        """Fetch lightweight metadata for one or more memory units.

        Returns ``{unit_id: UnitMeta}``.  Resolves the source note title
        via ``get_note`` when a ``note_id`` is present on the unit.
        Units that fail to load are silently omitted.
        """
        result: dict[str, UnitMeta] = {}
        for uid in unit_ids:
            try:
                unit = await self._client.get_memory_unit(uid)
            except Exception:  # noqa: BLE001
                continue

            # Extract date from mentioned_at (datetime or ISO string).
            date_str: str | None = None
            mentioned = getattr(unit, 'mentioned_at', None)
            if mentioned is not None:
                try:
                    date_str = mentioned.strftime('%Y-%m-%d')
                except AttributeError:
                    # Fallback for string values.
                    date_str = str(mentioned)[:10] if mentioned else None

            status = getattr(unit, 'status', None)

            # Resolve source note title.
            note_name: str | None = None
            note_id = getattr(unit, 'note_id', None)
            if note_id is not None:
                try:
                    note = await self._client.get_note(note_id)
                    note_name = getattr(note, 'name', None) or getattr(note, 'title', None)
                except Exception:  # noqa: BLE001
                    pass  # note lookup is best-effort

            result[uid] = UnitMeta(
                unit_id=uid,
                date=date_str,
                note_name=note_name,
                status=status,
            )
        return result

    async def get_unit_detail(self, unit_id: str) -> UnitDetail | None:
        """Fetch enriched detail for a single memory unit.

        Combines unit metadata, source note metadata, and chunk position
        into a single ``UnitDetail`` view-model for the DETAIL drill-down.
        Returns ``None`` if the unit cannot be loaded.
        """
        try:
            unit = await self._client.get_memory_unit(unit_id)
        except Exception:  # noqa: BLE001
            return None

        text = getattr(unit, 'text', '') or ''
        status = getattr(unit, 'status', None)
        fact_type = getattr(unit, 'fact_type', None)
        confidence = getattr(unit, 'confidence', None)
        is_deprioritized = getattr(unit, 'is_deprioritized', False)

        # Extract created date from mentioned_at.
        created_at: str | None = None
        mentioned = getattr(unit, 'mentioned_at', None)
        if mentioned is not None:
            try:
                created_at = mentioned.isoformat()
            except AttributeError:
                created_at = str(mentioned) if mentioned else None

        # Resolve source note metadata.
        note_id_raw = getattr(unit, 'note_id', None)
        note_id_str: str | None = str(note_id_raw) if note_id_raw is not None else None
        note_key: str | None = None
        note_created: str | None = None
        if note_id_raw is not None:
            try:
                note = await self._client.get_note(note_id_raw)
                note_key = getattr(note, 'name', None) or getattr(note, 'title', None)
                note_created_dt = getattr(note, 'created_at', None)
                if note_created_dt is not None:
                    try:
                        note_created = note_created_dt.strftime('%Y-%m-%d')
                    except AttributeError:
                        note_created = str(note_created_dt)[:10]
            except Exception:  # noqa: BLE001
                pass  # note lookup is best-effort

        # Determine chunk position within the source note's page index.
        chunk_index: str | None = None
        chunk_id = getattr(unit, 'chunk_id', None)
        if note_id_raw is not None and chunk_id is not None:
            try:
                page_index = await self._client.get_note_page_index(note_id_raw)
                if page_index is not None:
                    toc = page_index if isinstance(page_index, dict) else {}
                    toc_nodes = toc.get('toc', [])
                    total_pages = _count_toc_nodes(toc_nodes)
                    position = _find_chunk_position(toc_nodes, str(chunk_id))
                    if total_pages > 0 and position is not None:
                        chunk_index = f'page {position} of {total_pages}'
                    elif total_pages > 0:
                        chunk_index = f'{total_pages} pages in source note'
            except Exception:  # noqa: BLE001
                pass

        # Extract entity names from metadata (if populated by the server).
        entities: list[str] = []
        meta = getattr(unit, 'metadata', None) or {}
        if isinstance(meta, dict):
            ent_list = meta.get('entities') or meta.get('entity_names') or []
            if isinstance(ent_list, list):
                entities = [str(e) for e in ent_list]

        return UnitDetail(
            unit_id=unit_id,
            text=text,
            status=status,
            created_at=created_at,
            note_id=note_id_str,
            note_key=note_key,
            note_created_at=note_created,
            chunk_index=chunk_index,
            entities=entities,
            fact_type=fact_type,
            confidence=confidence,
            is_deprioritized=is_deprioritized,
        )

    async def get_unit_lineage(self, unit_id: str) -> UnitLineage:
        """Fetch upstream and downstream lineage for a memory unit.

        Returns a ``UnitLineage`` with lists of (unit_id, label) tuples.
        Falls back to empty lists on any error.
        """
        upstream: list[tuple[str, str]] = []
        downstream: list[tuple[str, str]] = []

        try:
            from memex_common.schemas import LineageDirection

            up_resp = await self._client.get_lineage(
                'memory_unit',
                unit_id,
                direction=LineageDirection.UPSTREAM,
                depth=2,
                limit=5,
            )
            for node in getattr(up_resp, 'derived_from', []):
                entity = getattr(node, 'entity', {})
                nid = str(entity.get('id', ''))[:8]
                ntxt = str(entity.get('text', ''))[:80]
                upstream.append((nid, ntxt))
        except Exception:  # noqa: BLE001
            pass  # lineage is best-effort

        try:
            from memex_common.schemas import LineageDirection

            down_resp = await self._client.get_lineage(
                'memory_unit',
                unit_id,
                direction=LineageDirection.DOWNSTREAM,
                depth=2,
                limit=5,
            )
            for node in getattr(down_resp, 'derived_from', []):
                entity = getattr(node, 'entity', {})
                nid = str(entity.get('id', ''))[:8]
                ntxt = str(entity.get('text', ''))[:80]
                downstream.append((nid, ntxt))
        except Exception:  # noqa: BLE001
            pass

        return UnitLineage(upstream=upstream, downstream=downstream)

    async def fetch_note_text(self, note_id: str) -> str | None:
        """Fetch the original text of a note by ID.

        Returns ``None`` on any error.
        """
        try:
            note = await self._client.get_note(note_id)
            return getattr(note, 'original_text', None)
        except Exception:  # noqa: BLE001
            return None

    async def fetch_note_detail(self, note_id: str) -> tuple[str | None, str | None] | None:
        """Fetch ``(title, original_text)`` for a note by ID.

        Used by DETAIL mode when a finding targets a note directly (e.g. inbox
        routing proposals), where ``target_id`` is a note id, not a unit id.
        Returns ``None`` on any error so the caller can render a load failure.
        """
        try:
            note = await self._client.get_note(note_id)
        except Exception:  # noqa: BLE001
            return None
        # Handle both a model object (attrs) and a raw dict, so a client that
        # returns either still surfaces the note's title/body.
        if isinstance(note, dict):
            return note.get('title'), note.get('original_text')
        return getattr(note, 'title', None), getattr(note, 'original_text', None)


def _count_toc_nodes(toc: list[dict[str, Any]]) -> int:
    """Count total leaf+branch nodes in a TOC tree."""
    count = 0
    for node in toc:
        count += 1
        children = node.get('children', [])
        if children:
            count += _count_toc_nodes(children)
    return count


def _find_chunk_position(
    toc: list[dict[str, Any]], chunk_id: str, *, counter: list[int] | None = None
) -> int | None:
    """Find 1-based position of a chunk_id in the TOC tree (DFS order)."""
    if counter is None:
        counter = [0]
    for node in toc:
        counter[0] += 1
        if node.get('id') == chunk_id:
            return counter[0]
        children = node.get('children', [])
        if children:
            result = _find_chunk_position(children, chunk_id, counter=counter)
            if result is not None:
                return result
    return None


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
