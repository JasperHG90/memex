"""Retrieval-ranking stability suite.

Captures and verifies the top-k retrieval ranking for every query in
the three retrieval corpora (``acme_corp``, ``ai_research_lab``,
``project_nexus``), in BOTH ``search_type='memory'`` (which exercises
the log-additive bounded-boost composition inside
``retrieval.engine._rerank_units``) AND ``search_type='note'`` (which
exercises the ``NoteSearchEngine._rerank_results`` path — independent
of the memory-rerank composition but a real regression surface).

Workflow:
  1. **Capture** (first run on a fresh clone, or after intentional
     retrieval changes):
     ``MEMEX_EVAL_CAPTURE_BASELINES=1 memex-eval suite run retrieval_stability``.
     The outcome writes ``baselines/<scenario_id>.json`` and reports
     pass=1.0 for every scenario (capture is not a regression check).
  2. **Verify** (default):
     ``memex-eval suite run retrieval_stability``.
     The outcome reads each baseline and compares the current
     ranking via Rank-Biased Overlap (Webber/Moffat/Zobel 2010,
     p=0.9); the scenario passes when RBO ≥ 0.996.

The baseline pins note_keys (filename stems), NOT unit IDs — see
``_outcomes.py`` for the design rationale.

IMPORTANT (import order): the imports of ``_outcomes`` and
``_setup_actions`` MUST appear before ``suite.register(...)`` calls.
The ``@register_outcome`` / ``@register_setup_action`` decorators fire
at import time; scenarios reference ``RankingBaselineRbo(...)`` and
``SetupAction(kind='seed_paragraphs_from_sources', ...)`` by name and
those registrations must already exist.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path

from memex_eval.suite import SuiteMetadata, SuiteSources
from memex_eval.suite.decorator import Suite

logger = logging.getLogger('memex_eval.suites.retrieval_stability')

# Side-effect imports — DO NOT MOVE. These decorator registrations
# populate the framework's outcome and setup-action registries before
# any scenario reference resolves.
from . import _outcomes  # noqa: F401 — decorator side effect
from . import _setup_actions  # noqa: F401 — decorator side effect
from ._outcomes import RankingBaselineRbo

_ROOT = Path(__file__).parent
_SOURCES_DIR = _ROOT / 'sources'
_BASELINES_DIR = _ROOT / 'baselines'
_SOURCE_SUITES_ROOT = _ROOT.parent  # packages/eval/src/memex_eval/suites/


def _slugify(text: str, max_len: int = 60) -> str:
    """Lowercase, collapse non-alphanumeric runs to underscores, truncate."""
    slug = re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')
    return slug[:max_len].rstrip('_')


def _scrape_queries_from_suite(corpus: str) -> list[str]:
    """AST-scrape unique ``query=`` literals from a source suite.

    Why scrape instead of import: importing the source suite triggers
    its own scenario registration against the framework's global suite
    registry. We want only the literal query strings without spinning
    up the source suite's runtime state.
    """
    src_path = _SOURCE_SUITES_ROOT / corpus / '__init__.py'
    if not src_path.is_file():
        logger.warning(
            'retrieval_stability: source suite %r not found at %s — '
            'zero scenarios will be registered for this corpus',
            corpus,
            src_path,
        )
        return []
    tree = ast.parse(src_path.read_text(encoding='utf-8'))
    seen: set[str] = set()
    ordered: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            call_name = func.attr
        elif isinstance(func, ast.Name):
            call_name = func.id
        else:
            continue
        if call_name not in {'register', 'scenario'}:
            continue
        for kw in node.keywords:
            if kw.arg != 'query':
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                v = kw.value.value
                if v not in seen:
                    seen.add(v)
                    ordered.append(v)
    return ordered


def _load_baseline(scenario_id: str) -> tuple[list[str], dict[str, object]]:
    """Read the captured ranking + meta block for a scenario.

    Returns ``([], {})`` when the baseline file is absent. The
    outcome's ``score()`` distinguishes empty-baseline (capture
    pending → status='error' with a clear hint) from a 0.0 RBO
    (regression), and compares the persisted ``meta`` block against
    the current scenario state to detect stale baselines.

    A truncated / corrupt JSON is wrapped in a ``RuntimeError`` with
    a recapture hint — otherwise a bare ``json.JSONDecodeError``
    crashes suite-import (``memex-eval suite list`` etc.) before
    operators see a useful message.
    """
    path = _BASELINES_DIR / f'{scenario_id}.json'
    if not path.is_file():
        return [], {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f'baseline file {path} is corrupt: {exc}. '
            f'Recapture with `MEMEX_EVAL_CAPTURE_BASELINES=1 '
            f'memex-eval suite run retrieval_stability`.'
        ) from exc
    return list(payload.get('ranking', [])), dict(payload.get('meta', {}))


METADATA = SuiteMetadata(
    name='retrieval_stability',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Captures and verifies top-k retrieval rankings (memory-search '
        "AND note-search) over the three retrieval corpora's queries. "
        'Gates against drift in: embedder, reranker, RRF/MMR/pre-filter, '
        'log-additive bounded-boost composition (memory path), and the '
        'note-rerank composition (CE+sigmoid+RRF+cosine, note path).'
    ),
    tags=['retrieval', 'reranking', 'regression-gate', 'stability'],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'retrieval.cross_encoder_rerank',
        'retrieval.composition.log_clip',
        'retrieval.note_rerank',
        'retrieval.rrf',
        'retrieval.mmr',
        'retrieval.pre_filter',
        'memory.models.embedder',
    ],
    knobs=[
        'server.memory.retrieval.composite_boost_log_clip',
        'server.memory.retrieval.reranking_mw_alpha',
        'server.memory.retrieval.reranking_recency_alpha',
        'server.memory.retrieval.reranking_temporal_alpha',
    ],
    requires_llm_judge=False,
)


suite = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_SOURCES_DIR),
    readme_path=_ROOT / 'README.md',
)


def _register_query(corpus: str, query: str) -> None:
    """Register a memory + note pair for one (corpus, query)."""
    query_slug = _slugify(query)
    for search_type, group in (
        ('memory', 'retrieval_stability_memory'),
        ('note', 'retrieval_stability_notes'),
    ):
        scenario_id = f'{corpus}_{query_slug}_{search_type}'
        baseline_path = _BASELINES_DIR / f'{scenario_id}.json'
        ranking, meta = _load_baseline(scenario_id)
        suite.register(
            id=scenario_id,
            description=(f'{search_type}-search reranker order — {corpus} / {query!r}'),
            query=query,
            group=group,
            top_k=10,
            search_type=search_type,  # type: ignore[arg-type]
            expected=RankingBaselineRbo(
                type='ranking_baseline_rbo',
                baseline_path=str(baseline_path),
                baseline_ranking=ranking,
                baseline_meta=meta,
                expected_top_k=10,
                expected_search_type=search_type,  # type: ignore[arg-type]
                p=0.9,
                rbo_floor=0.996,
            ),
        )


for _corpus in ('acme_corp', 'ai_research_lab', 'project_nexus'):
    for _query in _scrape_queries_from_suite(_corpus):
        _register_query(_corpus, _query)


SUITE = suite.build()
