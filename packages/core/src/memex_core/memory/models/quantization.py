"""F42 — int8 dynamic quantization for the cross-encoder reranker.

Produces an int8 ONNX variant of the reranker on first use and caches it
to disk alongside the fp32 source so subsequent loads reuse it. Designed
to be invoked lazily from ``get_reranking_model`` only when
``OnnxBackend.quantization == 'int8'``.

Why dynamic quantization
~~~~~~~~~~~~~~~~~~~~~~~~
``onnxruntime.quantization.quantize_dynamic`` quantises Linear / MatMul /
Gather weights to int8 without calibration data. Activations stay fp32 at
runtime. This is the standard CPU-side win for transformer rerankers —
typical 2× speedup, sub-1% scoring drift on cosine similarity rerankers
(the spec calls out ``low recall risk``).

Why we patch ``save_and_reload_model_with_shape_infer``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The bundled reranker (``ms-marco-minilm-l12-hindsight-reranker``) ships
with a tensor whose declared shape disagrees with shape inference's
prediction. The default quantization path runs shape inference first,
then errors out. We bypass that single step — the rest of the
quantization pipeline is unaffected. If the upstream model export is
ever fixed, the patch becomes a no-op.

External-data files
~~~~~~~~~~~~~~~~~~~
The reranker stores weights in an external ``model.onnx.data`` sidecar
(>2GB protobuf limit workaround). We pass ``use_external_data_format=True``
so the int8 output mirrors that layout — quantizing in place would
otherwise inline gigabytes of weights into a single .onnx file.
"""

from __future__ import annotations

import logging
import pathlib as plb
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger('memex.core.memory.models.quantization')


@contextmanager
def _bypass_shape_inference() -> Iterator[None]:
    """Patch out ``save_and_reload_model_with_shape_infer`` for the duration
    of the ``with`` block.

    This is the single line in onnxruntime.quantization that re-runs ONNX
    shape inference. Some exported models trip it; bypassing returns the
    in-memory model unchanged so quantization can proceed. Restored on
    exit so other call sites are unaffected.
    """
    from onnxruntime.quantization import onnx_quantizer, quant_utils

    original_qutils = quant_utils.save_and_reload_model_with_shape_infer
    original_onnx = onnx_quantizer.save_and_reload_model_with_shape_infer

    def _passthrough(model):  # type: ignore[no-untyped-def]
        return model

    quant_utils.save_and_reload_model_with_shape_infer = _passthrough
    onnx_quantizer.save_and_reload_model_with_shape_infer = _passthrough
    try:
        yield
    finally:
        quant_utils.save_and_reload_model_with_shape_infer = original_qutils
        onnx_quantizer.save_and_reload_model_with_shape_infer = original_onnx


def quantized_model_filename(source_filename: str) -> str:
    """Return ``model.int8.onnx`` for ``model.onnx``.

    Sidesteps colliding with anything else in the model dir — every file
    that rides along with the int8 variant uses the ``.int8.`` infix.
    """
    p = plb.Path(source_filename)
    return f'{p.stem}.int8{p.suffix}'


def ensure_quantized_model(model_dir: plb.Path, source_model_name: str) -> str:
    """Lazily produce an int8 ONNX file in *model_dir*; return its filename.

    Caches on disk: the first call quantises and writes; subsequent calls
    detect the existing file and short-circuit. The output filename is
    derived from the source via ``quantized_model_filename``.

    Args:
        model_dir: Directory holding the source ONNX file (and any
            external-data sidecar).
        source_model_name: Filename of the fp32 ONNX file inside
            ``model_dir`` (typically ``model.onnx``).

    Returns:
        Filename of the int8 ONNX file (relative to ``model_dir``). Pass
        this to ``BaseOnnxModel(model_dir=..., model_name=<this>)`` to
        load it.

    Raises:
        ImportError: if ``onnx`` is not installed (it is an optional
            dependency only required for the int8 path).
        FileNotFoundError: if the source model file is missing.
    """
    source_path = model_dir / source_model_name
    if not source_path.exists():
        raise FileNotFoundError(f'Source ONNX model not found: {source_path}')

    target_name = quantized_model_filename(source_model_name)
    target_path = model_dir / target_name

    if target_path.exists():
        logger.debug('F42: int8 reranker variant already cached at %s', target_path)
        return target_name

    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
    except ImportError as e:  # pragma: no cover - import failure surfaced to user
        raise ImportError(
            'onnxruntime.quantization requires the ``onnx`` package. '
            'Install it via ``uv add onnx`` (already declared as a memex_core '
            'dependency for the int8 reranker path).'
        ) from e

    logger.info('F42: building int8 reranker variant at %s (one-time, cached)', target_path)
    with _bypass_shape_inference():
        quantize_dynamic(
            model_input=str(source_path),
            model_output=str(target_path),
            weight_type=QuantType.QInt8,
            use_external_data_format=True,
        )

    if not target_path.exists():
        raise RuntimeError(f'F42: int8 quantization completed without producing {target_path}')

    return target_name
