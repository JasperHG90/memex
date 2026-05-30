"""Unit tests: evidence model shapes + static checks on migration 054 (no DB)."""

from __future__ import annotations

import importlib.util
import json
import pathlib as plb

from memex_core.services.inbox_router.evidence import (
    CandidateEvidence,
    NoFitEvidence,
    RouteEvidence,
)

_MIGRATION = (
    plb.Path(__file__).resolve().parents[4] / 'src/memex_core/alembic/versions/055_inbox_router.py'
)


def test_route_evidence_round_trips():
    ev = RouteEvidence(
        routing_state='warmed_up_auto_eligible',
        margin=0.6,
        source_vault_id='11111111-1111-1111-1111-111111111111',
        top_candidates=[
            CandidateEvidence(
                vault_id='22222222-2222-2222-2222-222222222222',
                vault_name='memex',
                p_match=0.8,
                p_match_raw=0.9,
                ci_half_width=0.02,
            )
        ],
    )
    payload = json.loads(ev.model_dump_json())
    assert payload['kind'] == 'inbox_vault_route'
    assert payload['top_candidates'][0]['vault_name'] == 'memex'
    # Re-parse to confirm the shape is stable.
    assert RouteEvidence.model_validate(payload).margin == 0.6


def test_no_fit_evidence_defaults():
    ev = NoFitEvidence(routing_state='cold_start_no_auto', best_p_match_raw=0.03)
    payload = json.loads(ev.model_dump_json())
    assert payload['kind'] == 'inbox_vault_no_fit'
    assert payload['retry_n'] == 0
    assert payload['next_retry_at'] is None


def _load_migration():
    spec = importlib.util.spec_from_file_location('mig054', _MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_revision_chain():
    mod = _load_migration()
    assert mod.revision == '055_inbox_router'
    assert mod.down_revision == '054_nodes_vault_active'


def test_migration_extends_lint_type_check_with_routing():
    text = _MIGRATION.read_text()
    assert "'routing'" in text
    # Upgrade adds routing; downgrade removes it.
    assert 'ADD CONSTRAINT ck_maintenance_proposals_lint_type' in text
    assert 'inbox_router_nb_stats' in text
    assert 'inbox_router_vault_anchors' in text
    assert 'inbox_router_note_cache' in text
    assert 'CREATE VIEW inbox_router_nb_params' in text


def test_migration_seeds_prior():
    text = _MIGRATION.read_text()
    # All five features seeded for both labels (10 rows) + class counts.
    for feat in (
        'sem_summary_sim',
        'sem_centroid_sim',
        'mm_centroid_sim',
        'entity_jaccard',
        'keyword_ts_rank',
    ):
        assert feat in text
    assert 'inbox_router_nb_class_counts' in text
