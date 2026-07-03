"""Unit tests for the F10b ONNX NLI backend's load-time label-order validator."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memex_core.memory.models.backends.onnx_nli import (
    _LABEL_ORDER,
    OnnxNLIClassifier,
    _load_label_order_from_config,
)


@pytest.fixture
def mock_base_onnx_init():
    with patch(
        'memex_core.memory.models.base.BaseOnnxModel.__init__', return_value=None
    ) as mock_init:
        yield mock_init


def _write_config(tmp_path: Path, id2label: dict[str, str] | None) -> None:
    config: dict = {}
    if id2label is not None:
        config['id2label'] = id2label
    (tmp_path / 'config.json').write_text(json.dumps(config))


class TestLoadLabelOrderFromConfig:
    def test_returns_order_when_config_well_formed(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            {'0': 'contradiction', '1': 'entailment', '2': 'neutral'},
        )
        assert _load_label_order_from_config(str(tmp_path)) == (
            'contradiction',
            'entailment',
            'neutral',
        )

    def test_returns_none_when_config_missing(self, tmp_path: Path) -> None:
        assert _load_label_order_from_config(str(tmp_path)) is None

    def test_returns_none_when_id2label_missing(self, tmp_path: Path) -> None:
        _write_config(tmp_path, None)
        assert _load_label_order_from_config(str(tmp_path)) is None

    def test_returns_none_when_id2label_wrong_size(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {'0': 'a', '1': 'b'})
        assert _load_label_order_from_config(str(tmp_path)) is None

    def test_returns_none_when_keys_not_int_castable(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {'a': 'x', 'b': 'y', 'c': 'z'})
        assert _load_label_order_from_config(str(tmp_path)) is None

    def test_returns_none_when_json_malformed(self, tmp_path: Path) -> None:
        (tmp_path / 'config.json').write_text('{ malformed')
        assert _load_label_order_from_config(str(tmp_path)) is None

    def test_normalises_label_case(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            {'0': 'CONTRADICTION', '1': 'Entailment', '2': 'NEUTRAL'},
        )
        assert _load_label_order_from_config(str(tmp_path)) == (
            'contradiction',
            'entailment',
            'neutral',
        )


class TestOnnxNLIClassifierLabelValidation:
    def test_default_init_with_matching_config_succeeds(
        self, tmp_path: Path, mock_base_onnx_init: MagicMock
    ) -> None:
        _write_config(
            tmp_path,
            {'0': 'contradiction', '1': 'entailment', '2': 'neutral'},
        )
        clf = OnnxNLIClassifier(str(tmp_path))
        assert clf._label_order == _LABEL_ORDER

    def test_explicit_label_order_matching_config_succeeds(
        self, tmp_path: Path, mock_base_onnx_init: MagicMock
    ) -> None:
        _write_config(
            tmp_path,
            {'0': 'contradiction', '1': 'entailment', '2': 'neutral'},
        )
        order: tuple[str, str, str] = ('contradiction', 'entailment', 'neutral')
        clf = OnnxNLIClassifier(str(tmp_path), label_order=order)
        assert clf._label_order == order

    def test_explicit_label_order_mismatching_config_raises(
        self, tmp_path: Path, mock_base_onnx_init: MagicMock
    ) -> None:
        _write_config(
            tmp_path,
            {'0': 'contradiction', '1': 'entailment', '2': 'neutral'},
        )
        with pytest.raises(ValueError, match='does not match the model config'):
            OnnxNLIClassifier(
                str(tmp_path),
                label_order=('entailment', 'neutral', 'contradiction'),
            )

    def test_default_init_with_nonstandard_config_requires_explicit_label_order(
        self, tmp_path: Path, mock_base_onnx_init: MagicMock
    ) -> None:
        """A model whose config.json declares a non-default order must not be
        loaded with the F10b default — caller must opt in explicitly to avoid
        silent logit/label misattribution."""
        _write_config(
            tmp_path,
            {'0': 'entailment', '1': 'neutral', '2': 'contradiction'},
        )
        with pytest.raises(ValueError, match='Pass an explicit'):
            OnnxNLIClassifier(str(tmp_path))

    def test_default_init_without_config_falls_back_to_default(
        self, tmp_path: Path, mock_base_onnx_init: MagicMock
    ) -> None:
        clf = OnnxNLIClassifier(str(tmp_path))
        assert clf._label_order == _LABEL_ORDER
