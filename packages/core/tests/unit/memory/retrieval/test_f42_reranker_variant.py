"""F42 — reranker variant config (fp32 / int8 quantization) unit tests.

Pure-Python pinning tests that exercise the config field, the
``model_version`` plumbing, the F42 latency histogram label, and the
quantized-model filename helper. No model loading / ONNX runtime work
happens here — that belongs in the slow integration test
(``tests/integration/memory/retrieval/test_int_f42_quantization.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY

from memex_common.config import OnnxBackend, RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.models.quantization import quantized_model_filename
from memex_core.memory.models.reranking import FastReranker
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryUnit
from memex_core.metrics import RERANKER_LATENCY_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_unit(unit_id: UUID | None = None, text: str = 'fact') -> MemoryUnit:
    return MemoryUnit(
        id=unit_id or uuid4(),
        text=text,
        fact_type=FactTypes.WORLD,
        event_date=datetime.now(timezone.utc),
        vault_id=uuid4(),
        note_id=uuid4(),
        embedding=[],
        success_co_count=0,
        failure_co_count=0,
    )


def _read_histogram_count(metric, **labels) -> float:
    """Sum the ``_count`` sample for a labelled histogram."""
    name = metric._name + '_count'
    total = 0.0
    for sample in REGISTRY.collect():
        for s in sample.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                total += s.value
    return total


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestOnnxBackendQuantizationField:
    def test_default_is_fp32(self) -> None:
        b = OnnxBackend()
        assert b.quantization == 'fp32'

    def test_int8_accepted(self) -> None:
        b = OnnxBackend(quantization='int8')
        assert b.quantization == 'int8'

    def test_invalid_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            OnnxBackend(quantization='int4')  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FastReranker.variant property
# ---------------------------------------------------------------------------


class TestFastRerankerVariant:
    def test_fp32_parsed(self) -> None:
        with patch.object(FastReranker, '__init__', lambda self, **kw: None):
            r = FastReranker()
            r._model_version = 'onnx:repo:rev:fp32'
            assert r.variant == 'fp32'
            assert r.model_version == 'onnx:repo:rev:fp32'

    def test_int8_parsed(self) -> None:
        with patch.object(FastReranker, '__init__', lambda self, **kw: None):
            r = FastReranker()
            r._model_version = 'onnx:repo:rev:int8'
            assert r.variant == 'int8'

    def test_legacy_no_variant_returns_unknown(self) -> None:
        with patch.object(FastReranker, '__init__', lambda self, **kw: None):
            r = FastReranker()
            # Pre-F42 model_version had three colons (no trailing variant).
            r._model_version = 'onnx:repo:rev'
            assert r.variant == 'unknown'

    def test_int8_model_version_differs_from_fp32(self) -> None:
        """F41 invariant: cache key must differ between variants so flipping
        the config naturally invalidates stored scores."""
        with patch.object(FastReranker, '__init__', lambda self, **kw: None):
            r_fp32 = FastReranker()
            r_fp32._model_version = 'onnx:repo:rev:fp32'
            r_int8 = FastReranker()
            r_int8._model_version = 'onnx:repo:rev:int8'
            assert r_fp32.model_version != r_int8.model_version


# ---------------------------------------------------------------------------
# get_reranking_model — variant routing
# ---------------------------------------------------------------------------


class TestGetRerankingModelVariantRouting:
    @pytest.mark.asyncio
    async def test_fp32_uses_default_filename_and_tag(self) -> None:
        """Default OnnxBackend produces ``model.onnx`` + ``:fp32`` tag —
        no quantization helper invoked."""
        from memex_core.memory.models import reranking

        # Reset module-level cache so the test sees a fresh load.
        reranking._onnx_reranker_cache = None

        captured: dict[str, object] = {}

        def fake_init(self, model_dir, model_name, batch_size, model_version):
            captured['model_name'] = model_name
            captured['model_version'] = model_version
            self.batch_size = batch_size
            self._model_version = model_version

        with (
            patch.object(FastReranker, '__init__', fake_init),
            patch('pathlib.Path.exists', return_value=True),
            patch('memex_core.memory.models.quantization.ensure_quantized_model') as ensure_mock,
        ):
            from memex_core.memory.models.reranking import get_reranking_model

            await get_reranking_model(OnnxBackend())

        assert captured['model_name'] == 'model.onnx'
        assert str(captured['model_version']).endswith(':fp32')
        ensure_mock.assert_not_called()

        reranking._onnx_reranker_cache = None

    @pytest.mark.asyncio
    async def test_int8_invokes_helper_and_uses_quantized_filename(self) -> None:
        """``quantization='int8'`` calls ``ensure_quantized_model`` and
        threads the returned filename + ``:int8`` suffix through."""
        from memex_core.memory.models import reranking

        reranking._onnx_reranker_cache = None

        captured: dict[str, object] = {}

        def fake_init(self, model_dir, model_name, batch_size, model_version):
            captured['model_name'] = model_name
            captured['model_version'] = model_version
            self.batch_size = batch_size
            self._model_version = model_version

        with (
            patch.object(FastReranker, '__init__', fake_init),
            patch('pathlib.Path.exists', return_value=True),
            patch(
                'memex_core.memory.models.quantization.ensure_quantized_model',
                return_value='model.int8.onnx',
            ) as ensure_mock,
        ):
            from memex_core.memory.models.reranking import get_reranking_model

            await get_reranking_model(OnnxBackend(quantization='int8'))

        ensure_mock.assert_called_once()
        assert captured['model_name'] == 'model.int8.onnx'
        assert str(captured['model_version']).endswith(':int8')

        reranking._onnx_reranker_cache = None

    @pytest.mark.asyncio
    async def test_fp32_and_int8_model_versions_differ(self) -> None:
        """End-to-end check that F41's cache key (model_version) differs
        between variants — flipping the config invalidates stale scores."""
        from memex_core.memory.models import reranking
        from memex_core.memory.models.reranking import get_reranking_model

        captured: list[str] = []

        def fake_init(self, model_dir, model_name, batch_size, model_version):
            captured.append(model_version)
            self.batch_size = batch_size
            self._model_version = model_version

        with (
            patch.object(FastReranker, '__init__', fake_init),
            patch('pathlib.Path.exists', return_value=True),
            patch(
                'memex_core.memory.models.quantization.ensure_quantized_model',
                return_value='model.int8.onnx',
            ),
        ):
            reranking._onnx_reranker_cache = None
            await get_reranking_model(OnnxBackend())

            reranking._onnx_reranker_cache = None
            await get_reranking_model(OnnxBackend(quantization='int8'))

        assert len(captured) == 2
        assert captured[0] != captured[1]
        assert ':fp32' in captured[0]
        assert ':int8' in captured[1]

        reranking._onnx_reranker_cache = None


# ---------------------------------------------------------------------------
# Quantization helper — filename derivation
# ---------------------------------------------------------------------------


class TestQuantizedModelFilename:
    def test_default_model_name(self) -> None:
        assert quantized_model_filename('model.onnx') == 'model.int8.onnx'

    def test_alt_basename(self) -> None:
        assert quantized_model_filename('reranker.onnx') == 'reranker.int8.onnx'


# ---------------------------------------------------------------------------
# Latency histogram — labelled by variant
# ---------------------------------------------------------------------------


class TestRerankerLatencyHistogram:
    def test_histogram_registered_with_variant_label(self) -> None:
        """``variant`` is the only label and supports fp32/int8/unknown."""
        # Touch each label; the histogram registers them lazily.
        RERANKER_LATENCY_SECONDS.labels(variant='fp32').observe(0.0)
        RERANKER_LATENCY_SECONDS.labels(variant='int8').observe(0.0)
        RERANKER_LATENCY_SECONDS.labels(variant='unknown').observe(0.0)

        # Confirm the metric is in the default registry under its name.
        names = {sample.name for collector in REGISTRY.collect() for sample in collector.samples}
        assert 'memex_reranker_latency_seconds_count' in names
        assert 'memex_reranker_latency_seconds_bucket' in names

    @pytest.mark.asyncio
    async def test_engine_emits_with_variant_label_on_score(self) -> None:
        """``_reranker_score_uncached`` records under the reranker's
        ``variant`` attribute. Confirms the engine path actually reads it."""
        from memex_core.memory.retrieval._offload import configure_offload_semaphores
        from memex_common.config import ServerConfig

        configure_offload_semaphores(ServerConfig())

        reranker = MagicMock()
        reranker.score.return_value = [0.5, 0.3]
        reranker.model_version = 'onnx:test:v1:int8'
        reranker.variant = 'int8'

        engine = RetrievalEngine(
            embedder=MagicMock(),
            reranker=reranker,
            retrieval_config=RetrievalConfig(cross_encoder_cache_enabled=False),
        )

        before = _read_histogram_count(RERANKER_LATENCY_SECONDS, variant='int8')
        await engine._reranker_score_uncached('q', ['a', 'b'])
        after = _read_histogram_count(RERANKER_LATENCY_SECONDS, variant='int8')
        assert after - before == pytest.approx(1.0)
