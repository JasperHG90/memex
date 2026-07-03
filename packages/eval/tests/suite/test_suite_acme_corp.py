"""Tests for the consolidated ``acme_corp`` suite.

Covers:
- The suite loads cleanly via the public loader and exposes the expected
  source notes + scenario count.
- Vault-isolation source notes carry ``vault_name`` frontmatter so the
  runner ingests them into the right vault.
- The architecture-overview note has its PNG asset auto-attached via the
  per-note ``sources/assets/<note-key>/`` convention.
- Every scenario's expected ``ExpectedOutcome.type`` is registered and
  every scenario's setup-action ``kind`` is registered.
- The ``trigger_reflections`` setup action — added for the reflection
  scenarios in this suite — is wired into the registry.
"""

from __future__ import annotations

from memex_eval.suite import (
    list_outcomes,
    list_setup_actions,
    load_suite,
)


def test_acme_corp_loads_clean() -> None:
    suite = load_suite('acme_corp')
    assert suite.name == 'acme_corp'
    # Every source note we expect to find on disk.
    expected_keys = {
        # BASIC
        'project-alpha-kickoff',
        'project-alpha-update',
        'project-beta-overview',
        # REFLECTION
        'sarah-chen-profile',
        'sarah-chen-tech-decisions',
        # TEMPORAL
        'quarterly-review-q1',
        'quarterly-review-q2',
        # VAULT_ISOLATION
        'project-gamma',
        'project-delta',
        # ASSETS
        'architecture-overview',
        # OUTCOMES_MW
        'project-zeta-achievement',
        'project-zeta-incident',
        # DEPRIORITIZATION
        'widget-pro',
        'widget-lite-discontinued',
        # INTENT_CLASSIFICATION
        'company-core-values',
        'annual-roadmap-2025',
        'standup-jan-15',
        # SUMMARIZATION
        'data-platform-architecture',
        # SCALE — 11 departments + history
        'dept-engineering',
        'dept-marketing',
        'dept-sales',
        'dept-product',
        'dept-data-science',
        'dept-customer-success',
        'dept-finance',
        'dept-legal',
        'dept-hr',
        'dept-security',
        'dept-ai-research',
        'dept-engineering-history',
    }
    assert suite.sources.note_keys == expected_keys
    # >=30 scenarios per blueprint.
    assert len(suite.scenarios) >= 30


def test_acme_corp_scenario_ids_unique() -> None:
    suite = load_suite('acme_corp')
    ids = [sc.id for sc in suite.scenarios]
    assert len(ids) == len(set(ids))


def test_acme_corp_outcome_types_registered() -> None:
    suite = load_suite('acme_corp')
    registered = set(list_outcomes())
    for sc in suite.scenarios:
        assert sc.expected.type in registered, (
            f'Scenario {sc.id!r} uses unregistered outcome type {sc.expected.type!r}'
        )


def test_acme_corp_setup_action_kinds_registered() -> None:
    suite = load_suite('acme_corp')
    registered = set(list_setup_actions())
    for sc in suite.scenarios:
        for action in sc.setup_actions:
            assert action.kind in registered, (
                f'Scenario {sc.id!r} uses unregistered setup_action kind {action.kind!r}'
            )


def test_acme_corp_trigger_reflections_registered() -> None:
    # The reflection scenarios depend on this action existing in the
    # registry — guard against it being unregistered or renamed.
    assert 'trigger_reflections' in list_setup_actions()


def test_acme_corp_vault_isolation_frontmatter() -> None:
    suite = load_suite('acme_corp')
    gamma = suite.sources.get('project-gamma')
    delta = suite.sources.get('project-delta')
    assert gamma is not None
    assert delta is not None
    assert gamma.vault_name == 'bench-vault-a'
    assert delta.vault_name == 'bench-vault-b'


def test_acme_corp_vault_scenarios_pin_vault_name() -> None:
    suite = load_suite('acme_corp')
    by_id = {sc.id: sc for sc in suite.scenarios}
    a_ids = [
        'vault_a_contains_gamma',
        'vault_a_excludes_delta',
        'vault_a_entity_isolation',
    ]
    b_ids = [
        'vault_b_contains_delta',
        'vault_b_excludes_gamma',
        'vault_b_entity_isolation',
    ]
    for sc_id in a_ids:
        assert by_id[sc_id].vault_name == 'bench-vault-a'
    for sc_id in b_ids:
        assert by_id[sc_id].vault_name == 'bench-vault-b'


def test_acme_corp_architecture_asset_present() -> None:
    suite = load_suite('acme_corp')
    arch = suite.sources.get('architecture-overview')
    assert arch is not None
    assert 'system-diagram.png' in arch.assets
    asset_path = arch.assets['system-diagram.png']
    assert asset_path.is_file()
    # Sanity: file is non-empty and starts with the legacy PNG-magic prefix.
    data = asset_path.read_bytes()
    assert len(data) > 0
    assert data.startswith(b'\x89PNG')


def test_acme_corp_xfail_markers_for_api_only_outcomes() -> None:
    # Outcomes that consume API-shaped fields (kv_value, summary_text,
    # unit.metadata, entity_mentions) cannot be evaluated against
    # text-only agent backends. Those scenarios must declare
    # expected_failure_modes for ``claude-code`` and ``hermes`` so
    # cross-backend runs don't artificially tank the pass_rate.
    suite = load_suite('acme_corp')
    api_only_outcome_types = {
        'unit_metadata_matches',
        'kv_roundtrip',
        'summary_nonempty',
    }
    for sc in suite.scenarios:
        if sc.expected.type in api_only_outcome_types:
            assert 'claude-code' in sc.expected_failure_modes, (
                f'Scenario {sc.id!r} ({sc.expected.type}) must declare '
                "expected_failure_modes including 'claude-code'"
            )
            assert 'hermes' in sc.expected_failure_modes, (
                f'Scenario {sc.id!r} ({sc.expected.type}) must declare '
                "expected_failure_modes including 'hermes'"
            )
