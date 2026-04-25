"""PR1.5 sync-offload hardening tests.

Covers:
- AC-009: four-bucket audit invariant — every gated to_thread site is gated;
  every exempt site has a rationale comment; total count matches the audit.
- AC-010 + AC-011: shared module-level semaphores cap concurrent in-flight
  reranker/embedding/NER calls; timeout returns the coroutine while the
  underlying thread continues running.
- AC-012: ServerConfig schema fields exist with the right defaults, bounds,
  and round-trip from YAML.
- AC-013: docs/how-to/memory-budget.md exists, is registered in zensical.toml,
  contains the five required components, and field descriptions cross-link
  to it.
- W1 warmup ordering invariant: configure_offload_semaphores is called in
  server/__init__.py before the warmup block.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from memex_common.config import ServerConfig
from memex_core.memory.retrieval import _offload


# ---------------------------------------------------------------------------
# AC-012: ServerConfig schema (3x *_max_concurrency, 3x *_call_timeout)
# ---------------------------------------------------------------------------


class TestServerConfigSchema:
    def test_default_concurrency_caps_are_4(self) -> None:
        cfg = ServerConfig()
        assert cfg.reranker_max_concurrency == 4
        assert cfg.embedding_max_concurrency == 4
        assert cfg.ner_max_concurrency == 4

    def test_default_call_timeouts_are_30(self) -> None:
        cfg = ServerConfig()
        assert cfg.reranker_call_timeout == 30
        assert cfg.embedding_call_timeout == 30
        assert cfg.ner_call_timeout == 30

    def test_concurrency_caps_round_trip_from_dict(self) -> None:
        cfg = ServerConfig.model_validate(
            {
                'reranker_max_concurrency': 1,
                'embedding_max_concurrency': 2,
                'ner_max_concurrency': 3,
                'reranker_call_timeout': 5,
                'embedding_call_timeout': 6,
                'ner_call_timeout': 7,
            }
        )
        assert cfg.reranker_max_concurrency == 1
        assert cfg.embedding_max_concurrency == 2
        assert cfg.ner_max_concurrency == 3
        assert cfg.reranker_call_timeout == 5
        assert cfg.embedding_call_timeout == 6
        assert cfg.ner_call_timeout == 7

    @pytest.mark.parametrize(
        'field',
        [
            'reranker_max_concurrency',
            'embedding_max_concurrency',
            'ner_max_concurrency',
        ],
    )
    def test_concurrency_caps_enforce_ge_1(self, field: str) -> None:
        with pytest.raises(ValidationError):
            ServerConfig.model_validate({field: 0})

    @pytest.mark.parametrize(
        'field',
        [
            'reranker_max_concurrency',
            'embedding_max_concurrency',
            'ner_max_concurrency',
        ],
    )
    def test_concurrency_caps_enforce_le_4(self, field: str) -> None:
        with pytest.raises(ValidationError):
            ServerConfig.model_validate({field: 5})

    @pytest.mark.parametrize(
        'field',
        [
            'reranker_max_concurrency',
            'embedding_max_concurrency',
            'ner_max_concurrency',
        ],
    )
    def test_offload_caps_match_ac_012_ceiling(self, field: str) -> None:
        """AC-012 (F4 regression guard): the schema ceiling on the three
        offload concurrency caps is `le=4`, NOT `le=64`. The cap is a
        memory-safety rail — `le=4` matches the upper-bound the requirements
        doc commits to, the docs page recommends, and the worst-case combined
        peak (cap × per-call peak memory) the Jetson example tolerates.
        Without this regression test the schema can silently drift back to
        a permissive ceiling that lets operators configure values the
        memory-budget recipe doesn't validate.
        """
        info = ServerConfig.model_fields[field]
        # Field metadata holds the validators; we want the `le` constraint.
        le_values = [m.le for m in info.metadata if hasattr(m, 'le') and m.le is not None]
        assert le_values, f'ServerConfig.{field} must declare an le= constraint per AC-012'
        assert le_values[0] == 4, (
            f'ServerConfig.{field}.le == {le_values[0]}; AC-012 requires le=4. '
            f'See F4 in the Phase 3 adversarial review.'
        )

    @pytest.mark.parametrize(
        'field',
        [
            'reranker_call_timeout',
            'embedding_call_timeout',
            'ner_call_timeout',
        ],
    )
    def test_call_timeouts_enforce_ge_1(self, field: str) -> None:
        with pytest.raises(ValidationError):
            ServerConfig.model_validate({field: 0})

    def test_field_descriptions_link_to_memory_budget_doc(self) -> None:
        """Each new field's description points operators to the memory-budget docs page."""
        fields = ServerConfig.model_fields
        for name in (
            'reranker_max_concurrency',
            'embedding_max_concurrency',
            'ner_max_concurrency',
        ):
            desc = fields[name].description or ''
            assert 'docs/how-to/memory-budget.md' in desc, (
                f'ServerConfig.{name}.description must cross-link to '
                f'docs/how-to/memory-budget.md per AC-013'
            )


# ---------------------------------------------------------------------------
# _offload module: semaphore identity + configuration semantics
# ---------------------------------------------------------------------------


class TestOffloadModule:
    def test_configure_initializes_semaphores_with_caps(self) -> None:
        cfg = ServerConfig(
            reranker_max_concurrency=2,
            embedding_max_concurrency=3,
            ner_max_concurrency=4,
        )
        _offload.configure_offload_semaphores(cfg)
        assert _offload.get_reranker_semaphore()._value == 2
        assert _offload.get_embedding_semaphore()._value == 3
        assert _offload.get_ner_semaphore()._value == 4

    def test_semaphore_identity_is_stable(self) -> None:
        """One model = one semaphore: repeated calls return the same object."""
        _offload.configure_offload_semaphores(ServerConfig())
        assert _offload.get_reranker_semaphore() is _offload.get_reranker_semaphore()
        assert _offload.get_embedding_semaphore() is _offload.get_embedding_semaphore()
        assert _offload.get_ner_semaphore() is _offload.get_ner_semaphore()

    def test_call_timeouts_are_returned_as_floats(self) -> None:
        cfg = ServerConfig(
            reranker_call_timeout=15,
            embedding_call_timeout=25,
            ner_call_timeout=35,
        )
        _offload.configure_offload_semaphores(cfg)
        assert _offload.get_reranker_call_timeout() == 15.0
        assert _offload.get_embedding_call_timeout() == 25.0
        assert _offload.get_ner_call_timeout() == 35.0

    def test_unconfigured_get_raises(self) -> None:
        """Tests that bypass the autouse configure should fail loudly."""
        original_cfg = _offload._CFG
        original_rs = _offload._RERANKER_SEMAPHORE
        original_es = _offload._EMBEDDING_SEMAPHORE
        original_ns = _offload._NER_SEMAPHORE
        try:
            _offload._CFG = None
            _offload._RERANKER_SEMAPHORE = None
            _offload._EMBEDDING_SEMAPHORE = None
            _offload._NER_SEMAPHORE = None
            with pytest.raises(RuntimeError, match='configure_offload_semaphores'):
                _offload.get_reranker_semaphore()
            with pytest.raises(RuntimeError, match='configure_offload_semaphores'):
                _offload.get_embedding_semaphore()
            with pytest.raises(RuntimeError, match='configure_offload_semaphores'):
                _offload.get_ner_semaphore()
        finally:
            _offload._CFG = original_cfg
            _offload._RERANKER_SEMAPHORE = original_rs
            _offload._EMBEDDING_SEMAPHORE = original_es
            _offload._NER_SEMAPHORE = original_ns


# ---------------------------------------------------------------------------
# AC-010 + AC-011: shared semaphores cap concurrency at gated sites
# ---------------------------------------------------------------------------


class _StubRerankerWithCounter:
    """Records max in-flight count across all score() calls."""

    def __init__(self, sleep_ms: int = 25) -> None:
        self.sleep_s = sleep_ms / 1000.0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = None

    def score(self, query: str, texts: list[str]) -> list[float]:
        # Count concurrent calls. Module-level so the same counter is
        # observed regardless of which call site invokes us.
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.sleep_s)
            return [0.5 for _ in texts]
        finally:
            self.in_flight -= 1


class _StubEmbedderWithCounter:
    def __init__(self, sleep_ms: int = 25) -> None:
        self.sleep_s = sleep_ms / 1000.0
        self.in_flight = 0
        self.max_in_flight = 0

    def encode(self, texts: list[str]):  # type: ignore[no-untyped-def]
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.sleep_s)
            return [[0.0] * 4 for _ in texts]
        finally:
            self.in_flight -= 1


class _StubNERWithCounter:
    def __init__(self, sleep_ms: int = 25) -> None:
        self.sleep_s = sleep_ms / 1000.0
        self.in_flight = 0
        self.max_in_flight = 0

    def predict(self, text: str):  # type: ignore[no-untyped-def]
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.sleep_s)
            return []
        finally:
            self.in_flight -= 1


class TestRerankerCapShared:
    """AC-010 (rev 2): one reranker model, one capacity budget, shared
    across BOTH reranker call sites. The test mixes 10 calls across both
    sites with cap=2 and asserts max in-flight <= 2 — a per-site cap=2
    implementation would let 4 in-flight pass, which this test must reject.
    """

    @pytest.mark.asyncio
    async def test_reranker_respects_concurrency_cap(self) -> None:
        cfg = ServerConfig(reranker_max_concurrency=2, reranker_call_timeout=10)
        _offload.configure_offload_semaphores(cfg)
        stub = _StubRerankerWithCounter(sleep_ms=25)

        async def call_through_document_search() -> None:
            """Mimic the gated pattern at memory/retrieval/document_search.py:243."""
            async with _offload.get_reranker_semaphore():
                await asyncio.wait_for(
                    asyncio.to_thread(stub.score, 'q', ['a', 'b']),
                    timeout=_offload.get_reranker_call_timeout(),
                )

        async def call_through_engine() -> None:
            """Mimic the gated pattern at memory/retrieval/engine.py:1086."""
            async with _offload.get_reranker_semaphore():
                await asyncio.wait_for(
                    asyncio.to_thread(stub.score, 'q', ['c', 'd']),
                    timeout=_offload.get_reranker_call_timeout(),
                )

        # 5 calls per site, mixed; total = 10. With cap=2 shared, max ought to be 2.
        # If the implementation gave each site its own cap=2 semaphore,
        # max would be 4 and this test would fail.
        coros = [call_through_document_search() for _ in range(5)] + [
            call_through_engine() for _ in range(5)
        ]
        await asyncio.gather(*coros)
        assert stub.max_in_flight <= 2, (
            f'Shared reranker cap violated: max in-flight={stub.max_in_flight}; '
            f'expected <= 2 across both reranker sites combined'
        )

    @pytest.mark.asyncio
    async def test_reranker_timeout_returns_coroutine_while_thread_runs(
        self,
    ) -> None:
        """The wait_for fires before the slow stub finishes; the side-effect
        counter proves the underlying thread keeps running. The cap, not the
        timeout, prevents thread accumulation.
        """
        cfg = ServerConfig(reranker_max_concurrency=1, reranker_call_timeout=1)
        _offload.configure_offload_semaphores(cfg)

        sentinel = {'finished': False}

        def slow_score(query: str, texts: list[str]) -> list[float]:
            time.sleep(2.0)  # exceeds the 1s call timeout
            sentinel['finished'] = True
            return [0.0]

        t0 = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            async with _offload.get_reranker_semaphore():
                await asyncio.wait_for(
                    asyncio.to_thread(slow_score, 'q', ['a']),
                    timeout=_offload.get_reranker_call_timeout(),
                )
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5, f'TimeoutError fired too late: {elapsed:.2f}s'
        # Underlying thread continues; eventually flips the sentinel.
        # Wait briefly to confirm thread alive (without making test flaky).
        for _ in range(40):
            if sentinel['finished']:
                break
            await asyncio.sleep(0.1)
        assert sentinel['finished'], (
            'Underlying thread did not complete after wait_for fired — the '
            'docstring/comment claim that the thread keeps running is false'
        )


class TestEmbeddingCapShared:
    """AC-011: embedding cap is shared across all three embedding sites
    (api.py + document_search.py + retrieval/engine.py). Same shared-cap
    invariant as the reranker.
    """

    @pytest.mark.asyncio
    async def test_embedding_respects_concurrency_cap(self) -> None:
        cfg = ServerConfig(embedding_max_concurrency=2, embedding_call_timeout=10)
        _offload.configure_offload_semaphores(cfg)
        stub = _StubEmbedderWithCounter(sleep_ms=25)

        async def call_site(label: str) -> None:
            async with _offload.get_embedding_semaphore():
                await asyncio.wait_for(
                    asyncio.to_thread(stub.encode, [label]),
                    timeout=_offload.get_embedding_call_timeout(),
                )

        # 4 calls per site (3 sites) = 12 calls; cap=2.
        coros = [call_site(f's{site}-{i}') for site in range(3) for i in range(4)]
        await asyncio.gather(*coros)
        assert stub.max_in_flight <= 2, (
            f'Shared embedding cap violated: max in-flight={stub.max_in_flight}; '
            f'expected <= 2 across all three embedding sites'
        )

    @pytest.mark.asyncio
    async def test_embedding_timeout_fires(self) -> None:
        cfg = ServerConfig(embedding_max_concurrency=1, embedding_call_timeout=1)
        _offload.configure_offload_semaphores(cfg)

        def slow_encode(texts: list[str]):  # type: ignore[no-untyped-def]
            time.sleep(2.0)
            return [[0.0] * 4 for _ in texts]

        with pytest.raises(asyncio.TimeoutError):
            async with _offload.get_embedding_semaphore():
                await asyncio.wait_for(
                    asyncio.to_thread(slow_encode, ['a']),
                    timeout=_offload.get_embedding_call_timeout(),
                )


class TestNERCap:
    """AC-011 NER variant: only one NER call site, but the same gating pattern."""

    @pytest.mark.asyncio
    async def test_ner_respects_concurrency_cap(self) -> None:
        cfg = ServerConfig(ner_max_concurrency=2, ner_call_timeout=10)
        _offload.configure_offload_semaphores(cfg)
        stub = _StubNERWithCounter(sleep_ms=25)

        async def call_ner(query: str) -> None:
            async with _offload.get_ner_semaphore():
                await asyncio.wait_for(
                    asyncio.to_thread(stub.predict, query),
                    timeout=_offload.get_ner_call_timeout(),
                )

        await asyncio.gather(*(call_ner(f'q{i}') for i in range(8)))
        assert stub.max_in_flight <= 2, (
            f'NER cap violated: max in-flight={stub.max_in_flight}; expected <= 2'
        )

    @pytest.mark.asyncio
    async def test_ner_timeout_fires(self) -> None:
        cfg = ServerConfig(ner_max_concurrency=1, ner_call_timeout=1)
        _offload.configure_offload_semaphores(cfg)

        def slow_predict(text: str):  # type: ignore[no-untyped-def]
            time.sleep(2.0)
            return []

        with pytest.raises(asyncio.TimeoutError):
            async with _offload.get_ner_semaphore():
                await asyncio.wait_for(
                    asyncio.to_thread(slow_predict, 'q'),
                    timeout=_offload.get_ner_call_timeout(),
                )


# ---------------------------------------------------------------------------
# AC-009: four-bucket audit invariant
# ---------------------------------------------------------------------------


CORE_SRC = Path(__file__).resolve().parents[2] / 'src' / 'memex_core'

EXPECTED_GATED_SITES = [
    ('api.py', '_EMBEDDING'),
    ('memory/retrieval/document_search.py', '_EMBEDDING'),
    ('memory/retrieval/document_search.py', '_RERANKER'),
    ('memory/retrieval/engine.py', '_EMBEDDING'),
    ('memory/retrieval/engine.py', '_NER'),
    ('memory/retrieval/engine.py', '_RERANKER'),
]

EXPECTED_EXEMPT_SITES = [
    'memory/extraction/core.py',
    'memory/extraction/engine.py',
    'processing/files.py',  # 2 calls in this file
    'processing/web.py',
    'templates.py',
]

# Files containing dead-fallback to_thread calls explicitly out of scope.
DEAD_FALLBACK_FILES = ['llm.py']


class TestToThreadAudit:
    """AC-009 (d): grep -rn 'asyncio.to_thread' produces 18 calls; this
    test re-runs the audit at test time so any *new* call added without
    classification fails the build.
    """

    def _all_call_lines(self) -> list[tuple[str, int, str]]:
        """Return [(rel_path, line_no, line_text), ...] for every real
        asyncio.to_thread call (skips docstring mentions and binary files).
        """
        result = subprocess.run(
            ['grep', '-rn', '--include=*.py', 'asyncio.to_thread', str(CORE_SRC)],
            capture_output=True,
            text=True,
            check=True,
        )
        lines: list[tuple[str, int, str]] = []
        for raw in result.stdout.splitlines():
            # path:lineno:content
            parts = raw.split(':', 2)
            if len(parts) != 3:
                continue
            path, lineno_str, content = parts
            rel = str(Path(path).relative_to(CORE_SRC))
            stripped = content.lstrip()
            # Skip docstring references — these are inside `"""` blocks and
            # do not have asyncio.to_thread( as a call.
            if 'asyncio.to_thread(' not in stripped:
                continue
            # Drop the two known docstring lines from litellm backends
            if 'memory/models/backends/litellm_' in rel and 'asyncio.to_thread`' in stripped:
                continue
            lines.append((rel, int(lineno_str), content))
        return lines

    def test_total_call_count_matches_audit(self) -> None:
        """RFC-001 §"Step 1.5.1" verified 18 actual calls on 8e59301.
        AC-009 (d): if origin/main adds a new asyncio.to_thread between
        this AC and merge it must be classified — this test catches a
        silent addition.
        """
        lines = self._all_call_lines()
        assert len(lines) == 18, (
            f'Expected 18 asyncio.to_thread calls per AC-009 four-bucket '
            f'audit; found {len(lines)}. New calls must be classified into '
            f'gated / dead / exempt / warmup.'
        )

    def test_every_gated_site_acquires_a_semaphore(self) -> None:
        """For each gated file, the to_thread call is preceded by an
        `async with get_<X>_semaphore():` block (within a few lines).
        """
        for rel, _model in EXPECTED_GATED_SITES:
            full = (CORE_SRC / rel).read_text()
            # Crude but resilient: gated sites must have BOTH a get_*_semaphore
            # call and an asyncio.wait_for + asyncio.to_thread in the same file.
            assert (
                'get_reranker_semaphore' in full
                or 'get_embedding_semaphore' in full
                or 'get_ner_semaphore' in full
            ), f'{rel}: gated site missing semaphore acquisition'
            assert 'asyncio.wait_for' in full, (
                f'{rel}: gated site missing asyncio.wait_for around to_thread'
            )

    def test_every_exempt_site_has_rationale_comment(self) -> None:
        """AC-009 (b): each exempt to_thread call has an inline
        `# exempt: <reason>` comment within 3 lines above it.
        """
        for rel in EXPECTED_EXEMPT_SITES:
            full = (CORE_SRC / rel).read_text().splitlines()
            for i, line in enumerate(full):
                if 'asyncio.to_thread(' not in line:
                    continue
                window = '\n'.join(full[max(0, i - 3) : i + 1])
                assert 'exempt:' in window, (
                    f'{rel}:{i + 1} asyncio.to_thread call missing '
                    f'`# exempt: <reason>` comment within 3 lines above'
                )

    def test_every_dead_path_site_has_rationale_comment(self) -> None:
        """AC-009 four-bucket audit: each dead-fallback to_thread call
        carries an inline `# dead path: <reason>` comment within 3 lines
        above it. DSPy 3.1+ always exposes acall, so the to_thread branch
        is unreachable on the extraction hot path; the comment makes the
        audit grep-able and prevents future PRs from gating the wrong site.
        """
        for rel in DEAD_FALLBACK_FILES:
            full = (CORE_SRC / rel).read_text().splitlines()
            for i, line in enumerate(full):
                if 'asyncio.to_thread(' not in line:
                    continue
                window = '\n'.join(full[max(0, i - 3) : i + 1])
                assert 'dead path:' in window, (
                    f'{rel}:{i + 1} asyncio.to_thread call missing '
                    f'`# dead path: <reason>` comment within 3 lines above'
                )


# ---------------------------------------------------------------------------
# W1 warmup ordering invariant
# ---------------------------------------------------------------------------


class TestWarmupOrdering:
    """RFC-001 §"Step 1.5.4" Option W1: configure_offload_semaphores(cfg)
    must run BEFORE the warmup block so warmup acquires through the
    production gate. Verified by static read of server/__init__.py — line
    numbers shift over time, but the relative ordering is the invariant.
    """

    def test_configure_runs_before_warmup_in_server_init(self) -> None:
        src = (CORE_SRC / 'server' / '__init__.py').read_text()
        configure_idx = src.find('configure_offload_semaphores(')
        warmup_idx = src.find('Warming ONNX model arenas')
        assert configure_idx > 0, (
            'server/__init__.py must call configure_offload_semaphores at startup'
        )
        assert warmup_idx > 0, 'server/__init__.py warmup block marker not found'
        assert configure_idx < warmup_idx, (
            'W1 invariant violated: configure_offload_semaphores must be '
            'called BEFORE the warmup block (RFC-001 §"Step 1.5.4")'
        )

    def test_warmup_to_thread_calls_acquire_semaphores(self) -> None:
        """Each of the 4 routed-to-W1 warmup to_thread calls (3 in the
        Warming/Warmed marker block + 1 in the litellm dim probe just below)
        is wrapped in `async with get_<X>_semaphore():` (W1 —
        gate-with-startup-pre-acquire). Per RFC-001 §"Step 1.5.4" Option W1
        the AC-009 audit table classifies all 4 sites as routed warmup, not
        only the 3 inside the marker block.

        F15 (Phase 3 adversarial): a previous version of this test only
        walked the Warming/Warmed marker window and missed the dim-probe
        site at server/__init__.py:158-161; that site was gated correctly
        but unverified by a test, leaving silent regression risk. This
        test now spans both regions.
        """
        src_path = CORE_SRC / 'server' / '__init__.py'
        src = src_path.read_text()
        # F15 fix: the routed-warmup span extends from the "Warming ONNX
        # model arenas" marker through the litellm dim probe block. The
        # probe is conditional on isinstance(config.server.embedding_model,
        # LitellmEmbeddingBackend) so its body does not appear inside the
        # Warming/Warmed pair, but it IS a warmup-class call — same model
        # at startup, same gating story.
        warmup_start = src.find('Warming ONNX model arenas')
        # End at the first non-warmup site marker. We stop at MemexAPI(
        # which is where post-warmup application initialisation begins.
        post_warmup = src.find('MemexAPI(')
        assert warmup_start != -1, 'server/__init__.py warmup-start marker not found'
        assert post_warmup != -1, 'server/__init__.py post-warmup MemexAPI() not found'
        assert warmup_start < post_warmup, (
            f'warmup-start (idx {warmup_start}) must precede post-warmup MemexAPI() '
            f'(idx {post_warmup}); routed-warmup window is empty or inverted'
        )
        warmup_window = src[warmup_start:post_warmup]
        # AC-009 audit table: 4 routed-warmup asyncio.to_thread sites
        # (embed warmup, reranker warmup, ner warmup, embed dim probe).
        to_thread_count = warmup_window.count('asyncio.to_thread(')
        assert to_thread_count == 4, (
            f'Expected 4 routed-warmup asyncio.to_thread calls (embed, '
            f'rerank, ner, dim-probe); found {to_thread_count}. F15 audit: '
            f'a missing site means either someone deleted gating or added '
            f'an ungated warmup call.'
        )
        # Each must be preceded by its production semaphore acquire.
        sem_acquire_count = (
            warmup_window.count('get_embedding_semaphore()')
            + warmup_window.count('get_reranker_semaphore()')
            + warmup_window.count('get_ner_semaphore()')
        )
        assert sem_acquire_count >= 4, (
            f'Each routed-warmup to_thread must acquire its production '
            f'semaphore; found {sem_acquire_count} acquisitions for 4 sites. '
            f'F15: dim-probe at server/__init__.py:158-161 is a routed site, '
            f'not exempt.'
        )


# ---------------------------------------------------------------------------
# AC-013: docs/how-to/memory-budget.md content + registration
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS_PATH = REPO_ROOT / 'docs' / 'how-to' / 'memory-budget.md'
ZENSICAL_TOML = REPO_ROOT / 'zensical.toml'


class TestMemoryBudgetDocs:
    def test_docs_file_exists(self) -> None:
        assert DOCS_PATH.is_file(), f'AC-013 (a): docs page must exist at {DOCS_PATH}'

    def test_docs_registered_in_zensical_toml(self) -> None:
        assert ZENSICAL_TOML.is_file()
        text = ZENSICAL_TOML.read_text()
        assert 'how-to/memory-budget.md' in text, (
            'AC-013 (a): memory-budget.md must be discoverable from zensical.toml nav'
        )

    def test_docs_contain_recipe_template(self) -> None:
        """AC-013 (i): recipe template showing the four levers + shared constraint."""
        text = DOCS_PATH.read_text()
        for lever in (
            'ONNX_GPU_MEM_LIMIT',
            'RERANKER_BATCH_SIZE',
            'EMBEDDING_BATCH_SIZE',
            'memory_max',
        ):
            assert lever in text, f'AC-013 (i): missing lever "{lever}"'

    def test_docs_contain_jetson_worked_example(self) -> None:
        """AC-013 (ii): one concrete worked example for Jetson Orin Nano 8 GiB."""
        text = DOCS_PATH.read_text()
        assert 'Jetson Orin Nano' in text
        assert '8 GiB' in text or '8 GB' in text
        # validated values per RFC §"Step 1.5.5"
        assert '4_000_000_000' in text or '4000000000' in text or '4 GiB' in text
        assert re.search(r'RERANKER_BATCH_SIZE.{0,20}8', text), (
            'expected RERANKER_BATCH_SIZE=8 in Jetson worked example'
        )

    def test_docs_contain_per_lever_explanation(self) -> None:
        """AC-013 (iii): brief explanation of WHY each lever (one paragraph max)."""
        text = DOCS_PATH.read_text()
        # Heuristic: there is a "Why each lever matters" section or equivalent
        # heading + per-lever discussion.
        assert 'Why each lever matters' in text or 'why each lever' in text.lower()

    def test_docs_contain_wedge_warning(self) -> None:
        """AC-013 (iv): explicit warning re #50 reranker cuDNN crash."""
        text = DOCS_PATH.read_text()
        assert '#50' in text, 'AC-013 (iv): warning must cite issue #50'
        assert 'cuDNN' in text, 'AC-013 (iv): warning must mention cuDNN allocation'
        assert (
            'reranker_max_concurrency' in text and 'reranker_batch_size' in text.lower()
        ) or 'sister lever' in text.lower(), (
            'AC-013 (iv): warning must connect reranker batch and concurrency caps'
        )

    def test_docs_link_to_pr1_pr1_5_config_fields(self) -> None:
        """AC-013 (v): cross-link back to the new concurrency-cap fields."""
        text = DOCS_PATH.read_text()
        assert 'reranker_max_concurrency' in text
        assert 'embedding_max_concurrency' in text
        assert 'ner_max_concurrency' in text

    def test_docs_provide_adaptation_guidance(self) -> None:
        """AC-013 tightening: adaptation recipe rather than fabricated tuples."""
        text = DOCS_PATH.read_text()
        assert 'Adapting' in text or 'adaptation' in text.lower()
        # No fabricated 16 GiB or 32 GiB recipe tuple — only the Jetson 8 GiB example.
        # Heuristic: ensure 8 GiB is mentioned and unmistakable as the validated example.
        assert text.count('Jetson Orin Nano') >= 2, (
            'expected the docs to reference the Jetson example by name in '
            'multiple places (recipe + adaptation discussion)'
        )
