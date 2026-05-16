"""Suite-private setup action: direct paragraph seeding into MemoryUnit rows.

Registers ``seed_paragraphs_from_sources`` via
:func:`memex_eval.suite.setup_actions.register_setup_action`.

This handler exists as a **framework extension example** for suites
that need paragraph-precision seeding without going through LLM-driven
extraction. The ``retrieval_stability`` suite itself does NOT use this
handler — it uses the framework's normal ingest pipeline (which the
gate is meant to monitor). The handler is shipped here so the
extension pattern is documented, tested, and discoverable from a real
suite package. See :doc:`.claude/rules/eval-suites.md` for the
``direct-db-setup-action`` rule.

Determinism contract:
  * Unit IDs are deterministic UUIDv5 derived from
    ``(corpus_name, note_key, paragraph_index, paragraph_text)`` with a
    NUL byte separator. NUL is forbidden in every field (markdown text
    is checked via paragraph splitter; ``note_key`` matches
    ``[a-z][a-z0-9_-]*``; ``corpus_name`` is operator-set), so distinct
    field tuples cannot collide via injected delimiter.
  * Every seeded unit shares a fixed ``event_date`` (2020-01-01); the
    reranker's recency boost saturates to a constant, removing
    wall-clock variance from rankings.

Usage in a suite's `__init__.py`::

    suite.register(
        id='example',
        ...,
        setup_actions=[
            SetupAction(
                kind='seed_paragraphs_from_sources',
                params={
                    'corpus_name': 'my_corpus',
                    'sources_dir': str(_ROOT / 'sources'),
                },
            ),
        ],
    )
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from memex_eval.suite.setup_actions import SetupActionHandler, register_setup_action

if TYPE_CHECKING:
    from memex_common.client import RemoteMemexAPI

logger = logging.getLogger('memex_eval.suites.retrieval_stability._setup_actions')

# Stable UUIDv5 namespace. Pinned random UUIDv4 picked once at the
# creation of this handler — DO NOT change it. Changing the namespace
# invalidates every previously-seeded unit_id and forces every
# downstream baseline to re-capture. Pinning it here means the same
# paragraph always maps to the same unit_id across runs, machines, and
# Python versions.
_RANKING_BASELINE_NAMESPACE = UUID('a8d2b9c4-1e7f-4d3a-9b6e-8c5d4a2f1e3b')

# Minimum paragraph length to consider a paragraph "substantive" — short
# strings are mostly headings / list-item fragments that pollute ranking.
_MIN_PARAGRAPH_CHARS = 30

# Fixed event_date. Every seeded unit shares this so the recency boost
# is a constant for all of them and rankings derive from cross-encoder
# score alone. (Production recency formula clamps days_ago > 365 to a
# floor, so any past date past one year is equivalent.)
_RANKING_BASELINE_EVENT_DATE = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _split_into_paragraphs(content: str) -> list[str]:
    """Split a markdown body into paragraph strings.

    Strips an optional leading UTF-8 BOM (``removeprefix('\\ufeff')``),
    drops a leading YAML frontmatter block so ``title:``, ``tags:``,
    etc. don't pollute the seeded corpus as a paragraph, normalises
    CRLF / CR → LF, collapses runs of >2 newlines to a single blank
    line, and splits on blank-line boundaries. Paragraphs shorter than
    :data:`_MIN_PARAGRAPH_CHARS` are dropped (eliminates headings).
    """
    normalized = content.removeprefix('\ufeff')
    normalized = re.sub(r'\r\n?', '\n', normalized)
    # Strip embedded NUL bytes \u2014 ``_stable_unit_id`` relies on NUL being
    # absent from every field for its collision-free UUIDv5 derivation,
    # and the docstring of ``_split_into_paragraphs`` upstreams that
    # invariant to source-file content.
    normalized = normalized.replace('\x00', '')
    # Drop a leading YAML frontmatter block. ``\A`` + ``count=1``
    # anchor at the start of the file, so a body-level ``---``
    # horizontal-rule separator inside the markdown is preserved. The
    # trailing ``\n?`` matches files where the closing ``---`` is the
    # last line (no terminating newline) — without it the closing
    # fence would survive as content. Convention: source files MUST
    # NOT open with ``---\n`` unless they are YAML frontmatter; a
    # markdown file that opens with a horizontal rule would have its
    # opening rule + the next ``---`` block stripped.
    normalized = re.sub(r'\A---\n.*?\n---\n?', '', normalized, count=1, flags=re.DOTALL)
    normalized = re.sub(r'\n{3,}', '\n\n', normalized.strip())
    blocks = [b.strip() for b in normalized.split('\n\n') if b.strip()]
    return [b for b in blocks if len(b) >= _MIN_PARAGRAPH_CHARS]


def _stable_unit_id(corpus_name: str, note_key: str, paragraph_index: int, text: str) -> UUID:
    """Derive a deterministic UUIDv5 for a (corpus, note, paragraph) tuple.

    NUL byte separator: NUL is forbidden in every input field, so
    distinct field tuples cannot collide via an injected delimiter.
    The invariant is enforced here on every field — ``text`` is also
    NUL-stripped by ``_split_into_paragraphs`` upstream, but defensive
    validation makes the collision-resistance guarantee explicit at
    the derivation site.
    """
    if '\x00' in corpus_name:
        raise ValueError(f'corpus_name must not contain NUL: {corpus_name!r}')
    if '\x00' in note_key:
        raise ValueError(f'note_key must not contain NUL: {note_key!r}')
    if '\x00' in text:
        raise ValueError('paragraph text must not contain NUL (use _split_into_paragraphs)')
    name = f'{corpus_name}\x00{note_key}\x00{paragraph_index}\x00{text}'
    return uuid.uuid5(_RANKING_BASELINE_NAMESPACE, name)


@register_setup_action('seed_paragraphs_from_sources')
class _SeedParagraphsFromSources(SetupActionHandler):
    """Insert paragraph-precision MemoryUnit rows directly into the vault.

    Bypasses ingest+extraction. Reads ``.md`` files from
    ``params['sources_dir']``, splits each on blank lines, embeds with
    the local ONNX model, and inserts one ``MemoryUnit`` per paragraph
    via a SQLModel session against the configured Postgres.

    Returns ``{'note_key_to_unit_ids': {<note_key>: [<unit_id>, ...]}}``.
    The runner auto-prefixes the publish so the entry lands in
    ``scenario_context`` as
    ``'seed_paragraphs_from_sources.note_key_to_unit_ids'``. Outcomes
    that need note-key → unit-id resolution read from there.
    """

    required: ClassVar[bool] = True
    # Re-runnable for UNCHANGED content: deterministic UUIDv5 ids +
    # INSERT ... ON CONFLICT DO NOTHING make re-invocation a no-op
    # when the source markdown is byte-identical to the previous run.
    # If a source file is edited, the new paragraph hashes to a new
    # UUID and is inserted alongside the old row (which the gate
    # never reads — the gate consumes the current-run return value).
    # Operators who edit sources MUST treat the vault as needing a
    # fresh reseed (drop the vault, re-run).
    reusable_under_reuse_vault: ClassVar[bool] = True

    async def run(
        self,
        api: 'RemoteMemexAPI',
        vault_id: UUID,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        corpus_name = params.get('corpus_name')
        sources_dir_str = params.get('sources_dir')
        if not corpus_name or not sources_dir_str:
            raise ValueError(
                'seed_paragraphs_from_sources requires '
                "params={'corpus_name': str, 'sources_dir': str}; got "
                f'{params!r}'
            )
        sources_dir = Path(sources_dir_str)
        if not sources_dir.is_dir():
            raise FileNotFoundError(f'sources_dir {sources_dir_str!r} not found')

        # Walk markdown notes (non-recursive, *.md only).
        plan: list[tuple[str, int, str, UUID]] = []  # (note_key, idx, text, unit_id)
        for md_path in sorted(sources_dir.glob('*.md')):
            note_key = md_path.stem
            if note_key.startswith('_'):
                # Underscore-prefixed files are framework-convention
                # private; skip.
                continue
            content = md_path.read_text(encoding='utf-8')
            paragraphs = _split_into_paragraphs(content)
            for idx, text in enumerate(paragraphs):
                plan.append(
                    (note_key, idx, text, _stable_unit_id(corpus_name, note_key, idx, text))
                )

        if not plan:
            logger.warning(
                'seed_paragraphs_from_sources: no paragraphs to seed from %s',
                sources_dir,
            )
            return {'note_key_to_unit_ids': {}}

        # All heavy imports are deferred to method-body so that
        # ``import memex_eval.suites.retrieval_stability`` (which fires
        # at suite discovery and CLI startup) does not pull in
        # ``memex_core`` and the ONNX embedder stack. Grouping them here
        # keeps the deferred-import boundary explicit.
        from urllib.parse import urlparse

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from memex_common.config import (
            PostgresInstanceConfig,
            PostgresMetaStoreConfig,
            SecretStr,
        )
        from memex_core.memory.models import get_embedding_model
        from memex_core.memory.sql_models import ContentStatus, FactTypes, MemoryUnit
        from memex_core.storage.metastore import AsyncPostgresMetaStoreEngine
        from memex_eval.suite.db_teardown import resolve_database_dsn

        # Batch-embed in one call — real ONNX models amortise their
        # session-creation cost across the whole batch. The encode call
        # is synchronous CPU-bound work, so offload to a worker thread
        # to keep the runner's event loop responsive.
        embedder = await get_embedding_model()
        texts = [text for _, _, text, _ in plan]
        embeddings = await asyncio.to_thread(embedder.encode, texts)

        dsn = resolve_database_dsn()
        parsed = urlparse(dsn)
        meta_config = PostgresMetaStoreConfig(
            instance=PostgresInstanceConfig(
                host=parsed.hostname or 'localhost',
                port=parsed.port or 5432,
                database=(parsed.path or '/postgres').lstrip('/'),
                user=parsed.username or 'postgres',
                password=SecretStr(parsed.password or 'postgres'),
            ),
        )
        metastore = AsyncPostgresMetaStoreEngine(config=meta_config)
        await metastore.connect()

        note_key_to_unit_ids: dict[str, list[str]] = {}
        # INSERT ... ON CONFLICT (id) DO NOTHING honours the
        # reusable_under_reuse_vault=True contract: deterministic UUIDv5
        # ids + on-conflict-no-op make re-running against an already-
        # seeded vault a true no-op. Use the raw AsyncEngine via
        # ``engine.begin()`` rather than ``metastore.session()`` — the
        # SQLModel-wrapped session emits a deprecation warning for
        # non-SELECT statements, which is a false positive for INSERT.
        try:
            rows = [
                {
                    'id': unit_id,
                    'vault_id': vault_id,
                    'text': text,
                    'embedding': list(emb),
                    'status': ContentStatus.ACTIVE.value,
                    'fact_type': FactTypes.WORLD.value,
                    'event_date': _RANKING_BASELINE_EVENT_DATE,
                    'mentioned_at': _RANKING_BASELINE_EVENT_DATE,
                    'is_deprioritized': False,
                }
                for (_, _, text, unit_id), emb in zip(plan, embeddings, strict=True)
            ]
            if rows:
                stmt = (
                    pg_insert(MemoryUnit).values(rows).on_conflict_do_nothing(index_elements=['id'])
                )
                async with metastore.engine.begin() as conn:
                    await conn.execute(stmt)
            for note_key, _, _, unit_id in plan:
                note_key_to_unit_ids.setdefault(note_key, []).append(str(unit_id))
        finally:
            await metastore.close()

        return {'note_key_to_unit_ids': note_key_to_unit_ids}
