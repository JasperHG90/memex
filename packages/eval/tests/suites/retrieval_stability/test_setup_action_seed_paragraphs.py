"""Integration tests for ``seed_paragraphs_from_sources``.

The handler exists as a framework-extension example (the
``retrieval_stability`` suite itself uses normal ingest). With no
production consumer, the handler would bit-rot — these tests boot a real
Postgres testcontainer, plant a vault row, and exercise the full write
path end-to-end.

The ONNX embedder is monkey-patched to a deterministic stub so tests
stay fast and offline. Everything else — SQLModel registration, pgvector
column type, FK to ``vaults.id`` — is real.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator, Generator
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg
import numpy as np
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope='module'),
]


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope='module')
def postgres_dsn() -> Generator[str, None, None]:
    container = PostgresContainer('pgvector/pgvector:pg18-trixie')
    container.start()
    try:
        url = container.get_connection_url().replace('+psycopg2', '')
        if '+' in url.split('://', 1)[0]:
            url = 'postgresql://' + url.split('://', 1)[1]
        yield url
    finally:
        container.stop()


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def initialized_db(postgres_dsn: str) -> AsyncGenerator[str, None]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel

    import memex_core.memory.sql_models  # noqa: F401 — register tables

    engine = create_async_engine(
        postgres_dsn.replace('postgresql://', 'postgresql+asyncpg://'),
        future=True,
        echo=False,
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
            await conn.run_sync(SQLModel.metadata.create_all)
    finally:
        await engine.dispose()

    yield postgres_dsn


@pytest_asyncio.fixture
async def clean_db(initialized_db: str) -> AsyncGenerator[str, None]:
    conn = await asyncpg.connect(initialized_db)
    try:
        await conn.execute('TRUNCATE TABLE vaults, notes, memory_units RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield initialized_db


@pytest.fixture
def env_dsn(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv('MEMEX_EVAL_DATABASE_URL', clean_db)
    return clean_db


@pytest_asyncio.fixture
async def seeded_vault(env_dsn: str) -> AsyncGenerator[UUID, None]:
    """Insert a vault row the handler's MemoryUnit FK requires."""
    vault_id = uuid4()
    conn = await asyncpg.connect(env_dsn)
    try:
        await conn.execute(
            'INSERT INTO vaults (id, name) VALUES ($1, $2)',
            vault_id,
            f'test-vault-{vault_id.hex[:8]}',
        )
    finally:
        await conn.close()
    yield vault_id


class _StubEmbedder:
    """Stand-in for the ONNX FastEmbedder.

    Returns a fixed-length float32 vector per input string, seeded from
    the SHA-256 of the text so the same text → same vector across
    processes (Python's builtin ``hash`` is salted by PYTHONHASHSEED, so
    don't use it here). Cross-process stability is not strictly required
    by any assertion in this file, but the property makes failures
    reproducible if a future test starts asserting vector equality.
    """

    def encode(self, texts: list[str]) -> np.ndarray:
        import hashlib

        out = np.zeros((len(texts), 384), dtype=np.float32)
        for i, t in enumerate(texts):
            digest = hashlib.sha256(t.encode('utf-8')).digest()[:4]
            seed = int.from_bytes(digest, 'big')
            rng = np.random.default_rng(seed=seed)
            out[i] = rng.standard_normal(384).astype(np.float32)
        return out


@pytest_asyncio.fixture
async def stub_embedder() -> AsyncGenerator[_StubEmbedder, None]:
    """Patch ``get_embedding_model`` for the handler's import site."""
    stub = _StubEmbedder()

    async def _fake_loader(*_args: object, **_kwargs: object) -> _StubEmbedder:
        return stub

    with patch('memex_core.memory.models.get_embedding_model', new=_fake_loader):
        yield stub


# --- Helpers ----------------------------------------------------------------


def _write_source(sources_dir: Path, note_key: str, content: str) -> Path:
    p = sources_dir / f'{note_key}.md'
    p.write_text(content, encoding='utf-8')
    return p


async def _count_units(dsn: str, vault_id: UUID) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(
            'SELECT count(*) FROM memory_units WHERE vault_id = $1', vault_id
        )
    finally:
        await conn.close()


async def _fetch_texts(dsn: str, vault_id: UUID) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            'SELECT text FROM memory_units WHERE vault_id = $1 ORDER BY text',
            vault_id,
        )
        return [r['text'] for r in rows]
    finally:
        await conn.close()


# --- Tests ------------------------------------------------------------------


async def test_seeds_paragraphs_into_vault(
    env_dsn: str,
    seeded_vault: UUID,
    stub_embedder: _StubEmbedder,
    tmp_path: Path,
) -> None:
    """Happy path: 2 notes × 2 paragraphs each → 4 units in DB."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    sources = tmp_path / 'sources'
    sources.mkdir()
    _write_source(
        sources,
        'note_a',
        'First paragraph of note A with enough content to clear the min-char threshold.\n\n'
        'Second paragraph of note A with similarly substantive content for the splitter.',
    )
    _write_source(
        sources,
        'note_b',
        'First paragraph of note B with enough content to clear the min-char threshold.\n\n'
        'Second paragraph of note B with similarly substantive content for the splitter.',
    )

    handler = _SeedParagraphsFromSources()
    result = await handler.run(
        api=None,  # handler doesn't touch the API
        vault_id=seeded_vault,
        params={'corpus_name': 'test_corpus', 'sources_dir': str(sources)},
    )

    assert set(result['note_key_to_unit_ids'].keys()) == {'note_a', 'note_b'}
    assert len(result['note_key_to_unit_ids']['note_a']) == 2
    assert len(result['note_key_to_unit_ids']['note_b']) == 2
    assert await _count_units(env_dsn, seeded_vault) == 4


async def test_unit_ids_are_deterministic_uuidv5(
    env_dsn: str,
    seeded_vault: UUID,
    stub_embedder: _StubEmbedder,
    tmp_path: Path,
) -> None:
    """Same (corpus, note_key, paragraph_index, text) → same unit_id."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
        _stable_unit_id,
    )

    sources = tmp_path / 'sources'
    sources.mkdir()
    body = (
        'Paragraph one is long enough to clear the minimum paragraph threshold.\n\n'
        'Paragraph two also clears the minimum and is distinct from paragraph one.'
    )
    _write_source(sources, 'note_x', body)

    handler = _SeedParagraphsFromSources()
    result = await handler.run(
        api=None,
        vault_id=seeded_vault,
        params={'corpus_name': 'test_corpus', 'sources_dir': str(sources)},
    )

    expected_ids = [
        str(_stable_unit_id('test_corpus', 'note_x', 0, body.split('\n\n')[0])),
        str(_stable_unit_id('test_corpus', 'note_x', 1, body.split('\n\n')[1])),
    ]
    assert result['note_key_to_unit_ids']['note_x'] == expected_ids


async def test_different_corpus_produces_different_unit_ids() -> None:
    """Same text under a different corpus name → different unit_id."""
    from memex_eval.suites.retrieval_stability._setup_actions import _stable_unit_id

    text = 'Identical paragraph text used under two different corpus names.'
    id_a = _stable_unit_id('corpus_a', 'note', 0, text)
    id_b = _stable_unit_id('corpus_b', 'note', 0, text)
    assert id_a != id_b


async def test_bom_does_not_break_paragraph_splitter(
    env_dsn: str,
    seeded_vault: UUID,
    stub_embedder: _StubEmbedder,
    tmp_path: Path,
) -> None:
    """A UTF-8 BOM on the first paragraph must not push it below
    _MIN_PARAGRAPH_CHARS via stray bytes, and the BOM must not appear in
    the stored text."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    sources = tmp_path / 'sources'
    sources.mkdir()
    body = '﻿Paragraph with a leading BOM that should be stripped during splitting.'
    _write_source(sources, 'note_bom', body)

    handler = _SeedParagraphsFromSources()
    result = await handler.run(
        api=None,
        vault_id=seeded_vault,
        params={'corpus_name': 'bom_corpus', 'sources_dir': str(sources)},
    )

    assert len(result['note_key_to_unit_ids']['note_bom']) == 1
    texts = await _fetch_texts(env_dsn, seeded_vault)
    assert texts == ['Paragraph with a leading BOM that should be stripped during splitting.']


async def test_split_strips_embedded_nul_bytes() -> None:
    """``_stable_unit_id`` derives the UUIDv5 from a NUL-separated key.
    The collision-resistance claim only holds if no field contains a
    NUL, so the splitter must strip NULs from source content."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _split_into_paragraphs,
    )

    body = 'A paragraph that contains\x00an embedded NUL byte in its middle text.'
    paragraphs = _split_into_paragraphs(body)
    assert len(paragraphs) == 1
    assert '\x00' not in paragraphs[0]


async def test_split_strips_frontmatter_without_trailing_newline() -> None:
    """A closing ``---`` on the last line of frontmatter (no trailing
    newline) must still be recognised; otherwise the fence survives
    as content."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _split_into_paragraphs,
    )

    body = (
        '---\n'
        'title: No Trailing Newline\n'
        '---\n\nBody paragraph that is long enough to clear the threshold.'
    )
    paragraphs = _split_into_paragraphs(body)
    assert all('---' not in p for p in paragraphs)
    assert all('title:' not in p for p in paragraphs)


async def test_split_strips_yaml_frontmatter() -> None:
    """The source files carry YAML frontmatter; the splitter must drop
    it so ``title:``/``tags:`` don't seed as a paragraph."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _split_into_paragraphs,
    )

    body = (
        '---\n'
        'title: Some Doc\n'
        'tags: [foo, bar]\n'
        '---\n'
        '\n'
        'Body paragraph that should survive the frontmatter strip.\n'
        '\n'
        'Second body paragraph below the first.'
    )
    paragraphs = _split_into_paragraphs(body)
    assert len(paragraphs) == 2
    assert all('title:' not in p for p in paragraphs)


async def test_crlf_line_endings_normalized(
    env_dsn: str,
    seeded_vault: UUID,
    stub_embedder: _StubEmbedder,
    tmp_path: Path,
) -> None:
    """CRLF and CR boundaries split paragraphs equivalently to LF."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    sources = tmp_path / 'sources'
    sources.mkdir()
    body = (
        'First paragraph terminated by a CRLF pair followed by another CRLF blank line.\r\n'
        '\r\n'
        'Second paragraph after the blank line, also long enough to clear the threshold.'
    )
    _write_source(sources, 'note_crlf', body)

    handler = _SeedParagraphsFromSources()
    result = await handler.run(
        api=None,
        vault_id=seeded_vault,
        params={'corpus_name': 'crlf_corpus', 'sources_dir': str(sources)},
    )

    assert len(result['note_key_to_unit_ids']['note_crlf']) == 2


async def test_short_paragraphs_dropped(
    env_dsn: str,
    seeded_vault: UUID,
    stub_embedder: _StubEmbedder,
    tmp_path: Path,
) -> None:
    """Paragraphs below ``_MIN_PARAGRAPH_CHARS`` (30) are filtered."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    sources = tmp_path / 'sources'
    sources.mkdir()
    body = (
        '# Short heading\n\n'
        'Substantive paragraph long enough to survive the minimum-length filter.\n\n'
        'tiny\n\n'
        'Another substantive paragraph that also clears the length threshold easily.'
    )
    _write_source(sources, 'note_mixed', body)

    handler = _SeedParagraphsFromSources()
    result = await handler.run(
        api=None,
        vault_id=seeded_vault,
        params={'corpus_name': 'filter_corpus', 'sources_dir': str(sources)},
    )

    assert len(result['note_key_to_unit_ids']['note_mixed']) == 2


async def test_underscore_prefixed_files_skipped(
    env_dsn: str,
    seeded_vault: UUID,
    stub_embedder: _StubEmbedder,
    tmp_path: Path,
) -> None:
    """Files whose stem starts with ``_`` are framework-private and skipped."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    sources = tmp_path / 'sources'
    sources.mkdir()
    _write_source(
        sources,
        '_private',
        'Substantive paragraph that should be skipped because of underscore prefix.',
    )
    _write_source(
        sources,
        'public_note',
        'Substantive paragraph that should be seeded under the public note key.',
    )

    handler = _SeedParagraphsFromSources()
    result = await handler.run(
        api=None,
        vault_id=seeded_vault,
        params={'corpus_name': 'private_corpus', 'sources_dir': str(sources)},
    )

    assert set(result['note_key_to_unit_ids'].keys()) == {'public_note'}


async def test_empty_sources_dir_returns_empty(
    env_dsn: str,
    seeded_vault: UUID,
    stub_embedder: _StubEmbedder,
    tmp_path: Path,
) -> None:
    """Empty source directory returns an empty mapping; nothing inserted."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    sources = tmp_path / 'sources'
    sources.mkdir()

    handler = _SeedParagraphsFromSources()
    result = await handler.run(
        api=None,
        vault_id=seeded_vault,
        params={'corpus_name': 'empty_corpus', 'sources_dir': str(sources)},
    )

    assert result == {'note_key_to_unit_ids': {}}
    assert await _count_units(env_dsn, seeded_vault) == 0


async def test_missing_sources_dir_raises_filenotfound(tmp_path: Path) -> None:
    """Pointing at a nonexistent directory raises a clear error."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    handler = _SeedParagraphsFromSources()
    with pytest.raises(FileNotFoundError, match='not found'):
        await handler.run(
            api=None,
            vault_id=uuid4(),
            params={'corpus_name': 'x', 'sources_dir': str(tmp_path / 'does_not_exist')},
        )


async def test_missing_params_raises_valueerror() -> None:
    """Missing required params surface as ValueError with a hint."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    handler = _SeedParagraphsFromSources()
    vault_id = uuid4()
    with pytest.raises(ValueError, match='requires'):
        await handler.run(api=None, vault_id=vault_id, params={})
    with pytest.raises(ValueError, match='requires'):
        await handler.run(api=None, vault_id=vault_id, params={'corpus_name': 'x'})


async def test_rerun_with_same_content_is_idempotent(
    env_dsn: str,
    seeded_vault: UUID,
    stub_embedder: _StubEmbedder,
    tmp_path: Path,
) -> None:
    """The ``reusable_under_reuse_vault=True`` contract: re-running with
    identical sources is a no-op against an already-seeded vault."""
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    sources = tmp_path / 'sources'
    sources.mkdir()
    _write_source(
        sources,
        'note_idem',
        'First substantive paragraph for idempotency check, long enough to clear filter.\n\n'
        'Second substantive paragraph for idempotency check, also long enough to clear.',
    )

    handler = _SeedParagraphsFromSources()
    params = {'corpus_name': 'idem_corpus', 'sources_dir': str(sources)}
    first = await handler.run(api=None, vault_id=seeded_vault, params=params)
    second = await handler.run(api=None, vault_id=seeded_vault, params=params)

    assert first == second
    assert await _count_units(env_dsn, seeded_vault) == 2


async def test_action_is_registered_with_framework() -> None:
    """Importing the suite package registers the handler under the
    documented name, and the registry resolves to the right class."""
    import memex_eval.suites.retrieval_stability  # noqa: F401 — decorator side effect
    from memex_eval.suite.setup_actions import get_setup_action
    from memex_eval.suites.retrieval_stability._setup_actions import (
        _SeedParagraphsFromSources,
    )

    handler = get_setup_action('seed_paragraphs_from_sources')
    assert isinstance(handler, _SeedParagraphsFromSources)
