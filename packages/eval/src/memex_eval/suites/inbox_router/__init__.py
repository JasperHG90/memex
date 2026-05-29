"""Inbox-router suite — auto-routing accuracy gate.

Seeds two topically-distinct vaults plus a set of labelled notes in the inbox
vault, triggers a dry-run triage, and checks the router's top-1 routing accuracy
against the labels. Exercises the full path: per-vault anchor refresh →
per-note feature cache → pairwise GaussianNB scoring → decision policy — over
the live HTTP server (``POST /api/v1/inbox/triage``).

The corpus and labels live in ``_setup_actions.py`` (the action ingests them
into multiple vaults itself; the runner's single-vault source ingestion doesn't
fit a routing scenario). Import order matters: ``_outcomes`` / ``_setup_actions``
import FIRST so their decorators register before ``suite.register`` references
them.
"""

from pathlib import Path

from memex_eval.suite import SuiteMetadata
from memex_eval.suite.base import SetupAction
from memex_eval.suite.decorator import Suite

# Side-effect imports: register the outcome + setup action before use.
from memex_eval.suites.inbox_router._outcomes import InboxRouteAccuracy
from memex_eval.suites.inbox_router import _setup_actions  # noqa: F401

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='inbox_router',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Inbox-router auto-routing accuracy: labelled notes in the inbox vault '
        'must be routed (top-1) to the topically-correct vault by the pairwise '
        'GaussianNB router evaluated in Postgres.'
    ),
    tags=['inbox-router', 'routing', 'classification'],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'services.inbox_router',
        'services.inbox_router.decisions',
    ],
    knobs=[
        'server.memory.inbox_router.t_low',
        'server.memory.inbox_router.top_k_entities',
    ],
    requires_llm_judge=False,
)

suite = Suite(metadata=METADATA, readme_path=_ROOT / 'README.md')

suite.register(
    id='inbox_router_top1_accuracy',
    description='Labelled inbox notes route to the topically-correct vault (top-1).',
    # The setup action does all the work and publishes predictions vs labels into
    # context; the query is a no-op placeholder (the outcome ignores the answer).
    query='inbox router routing accuracy',
    setup_actions=[SetupAction(kind='seed_inbox_router_corpus')],
    expected=InboxRouteAccuracy(type='inbox_route_accuracy', min_accuracy=0.75),
)

SUITE = suite.build()
