"""Tests for suite loader + referential-integrity validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from memex_eval.suite import (
    GoldUnitIds,
    KeywordsPresent,
    Scenario,
    Suite,
    SuiteMetadata,
    SuiteSources,
    load_suite,
    discover_suite_names,
    SuiteNotFound,
)


def _write_suite(tmp_path: Path, name: str, init_py: str, sources: dict[str, str]) -> Path:
    suite_dir = tmp_path / name
    sources_dir = suite_dir / 'sources'
    sources_dir.mkdir(parents=True)
    (suite_dir / '__init__.py').write_text(init_py)
    (suite_dir / 'README.md').write_text('# test')
    for filename, content in sources.items():
        (sources_dir / filename).write_text(content)
    return suite_dir


def test_loads_built_in_basic_extraction() -> None:
    suite = load_suite('basic_extraction')
    assert suite.name == 'basic_extraction'
    assert len(suite.scenarios) == 5


def test_discover_lists_built_ins() -> None:
    names = discover_suite_names()
    assert 'basic_extraction' in names
    assert 'contradiction' in names


def test_unknown_suite_raises() -> None:
    with pytest.raises(SuiteNotFound):
        load_suite('does_not_exist_42')


def test_load_by_path(tmp_path: Path) -> None:
    init_py = textwrap.dedent("""\
        from pathlib import Path
        from memex_eval.suite import (
            Suite, SuiteMetadata, SuiteSources, Scenario, KeywordsPresent
        )
        _ROOT = Path(__file__).parent
        SUITE = Suite(
            metadata=SuiteMetadata(
                name='custom_test', schema_version='1', suite_version='1.0.0',
                description='custom',
            ),
            sources=SuiteSources.from_directory(_ROOT / 'sources'),
            scenarios=[
                Scenario(
                    id='s1',
                    description='d',
                    query='q',
                    expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                ),
            ],
            readme_path=_ROOT / 'README.md',
        )
    """)
    sources = {'note.md': '---\ntags: [t]\n---\n# Note\n'}
    suite_dir = _write_suite(tmp_path, 'custom_test', init_py, sources)
    suite = load_suite(suite_dir)
    assert suite.name == 'custom_test'


def test_referential_integrity_rejects_unresolved_note_keys() -> None:
    # Constructing a Suite with a GoldUnitIds referencing a non-existent
    # note_key must fail at validation time.
    sources = SuiteSources(notes=[])
    with pytest.raises(ValueError, match='note_keys'):
        Suite(
            metadata=SuiteMetadata(
                name='bad',
                schema_version='1',
                suite_version='1.0.0',
                description='x',
            ),
            sources=sources,
            scenarios=[
                Scenario(
                    id='s1',
                    description='d',
                    query='q',
                    expected=GoldUnitIds(
                        type='gold_unit_ids',
                        note_keys=['ghost-note'],
                    ),
                ),
            ],
        )


def test_referential_integrity_rejects_duplicate_scenario_ids() -> None:
    with pytest.raises(ValueError, match='Duplicate scenario_id'):
        Suite(
            metadata=SuiteMetadata(
                name='dup',
                schema_version='1',
                suite_version='1.0.0',
                description='x',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[
                Scenario(
                    id='same',
                    description='d',
                    query='q',
                    expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                ),
                Scenario(
                    id='same',
                    description='d2',
                    query='q2',
                    expected=KeywordsPresent(type='keywords_present', keywords=['y']),
                ),
            ],
        )


def test_scenario_id_must_be_snake_case() -> None:
    with pytest.raises(ValueError, match='must match'):
        Scenario(
            id='BadID',
            description='d',
            query='q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )


def test_sources_content_hash_is_stable() -> None:
    suite = load_suite('basic_extraction')
    h1 = suite.sources.content_hash()
    h2 = suite.sources.content_hash()
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex
