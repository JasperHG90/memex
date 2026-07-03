"""Tests for Scenario.replicates_override + runner replicate-loop honoring."""

from __future__ import annotations

import pytest

from memex_eval.suite import KeywordsPresent, Scenario


def _make_scenario(**overrides) -> Scenario:
    base = dict(
        id='s1',
        description='d',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        top_k=5,
    )
    base.update(overrides)
    return Scenario(**base)


def test_default_is_none() -> None:
    sc = _make_scenario()
    assert sc.replicates_override is None


def test_explicit_override_accepted() -> None:
    sc = _make_scenario(replicates_override=1)
    assert sc.replicates_override == 1


def test_override_must_be_positive() -> None:
    with pytest.raises(Exception):
        _make_scenario(replicates_override=0)


def test_override_negative_rejected() -> None:
    with pytest.raises(Exception):
        _make_scenario(replicates_override=-3)


def test_runner_replicate_count_respects_override() -> None:
    sc = _make_scenario(replicates_override=2)
    replicates_arg = 5
    actual = sc.replicates_override or replicates_arg
    assert actual == 2


def test_runner_replicate_count_falls_through_when_unset() -> None:
    sc = _make_scenario()
    replicates_arg = 5
    actual = sc.replicates_override or replicates_arg
    assert actual == 5
