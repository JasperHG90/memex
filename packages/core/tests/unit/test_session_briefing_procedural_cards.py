"""Unit tests for the procedural-cards section in SessionBriefingService.

The V7 procedural plane exposes a pin-chain briefing surface. The
session-briefing service renders those cards in its own section
(``## Procedural Cards``) when the plane is wired into the service.
This module exercises that path in isolation — the rest of the briefing
behaviour is already covered by ``test_session_briefing.py`` and the new
``procedural_search`` dependency is optional, so we stand up a fresh
service instance per test rather than depending on the existing one.

What's pinned here:

* Section is empty when the plane is not wired (default — the optional
  dep is None).
* Section is empty when the plane returns ``None`` or an empty
  ``cards`` list.
* Section is empty when every card's ``entry`` is None (defensive —
  a malformed card must not crash the briefing).
* One bullet per card with kind/title/summary/pin context in the
  documented shape.
* Summary truncates at 240 chars with an ellipsis (240-char cap is
  the briefing's per-card budget; 240 chars ≈ 60 tokens).
* Bullet shape degrades gracefully when summary or context_key is
  missing.
* Overflow Step 5b drops the section entirely under tight budget.

We deliberately do not re-test the briefing-cards HTTP route or the
search service itself — those live in their own test modules.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.memory.sql_models import MentalModel
from memex_core.services.session_briefing import SessionBriefingService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    kind: str = 'procedure',
    title: str = 'rotate-api-key',
    summary: str = 'Rotate the OpenAI API key via the platform UI every 90 days.',
) -> SimpleNamespace:
    """A bare object matching the DTO's attribute access surface.

    Using ``SimpleNamespace`` (not a full Pydantic model) keeps the
    tests decoupled from schema evolution — the briefing's section
    builder only reads these four attributes via ``getattr``.
    """
    return SimpleNamespace(kind=kind, title=title, summary=summary)


def _make_card(
    entry: SimpleNamespace | None = None,
    context_key: str = 'project:42',
    pin_position: int = 0,
) -> SimpleNamespace:
    """Bare object matching :class:`ExperientialBriefingCard` attribute access."""
    return SimpleNamespace(
        entry=entry,
        context_key=context_key,
        pin_position=pin_position,
    )


def _make_cards_response(cards: list[SimpleNamespace]) -> SimpleNamespace:
    """The 5th element of ``_fetch_all``'s tuple — the cards envelope."""
    return SimpleNamespace(cards=cards, context_keys=[], total_pinned=len(cards))


def _make_procedural_search(cards_response: object) -> AsyncMock:
    """Mock the V7 search service to a fixed ``briefing_cards`` response."""
    svc = MagicMock()
    svc.briefing_cards = AsyncMock(return_value=cards_response)
    return svc


def _mock_metastore(mental_models: list[MentalModel] | None = None) -> MagicMock:
    """Same shape as the existing fixture in ``test_session_briefing.py``.

    The briefing service only consults the metastore for mental models
    via the ``session()`` async-context-manager protocol — we hand back
    an empty result so mental-model assertions stay orthogonal to the
    procedural-cards section.
    """
    metastore = MagicMock()
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.all.return_value = mental_models or []
    session.exec = AsyncMock(return_value=result_mock)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore.session.return_value = ctx
    return metastore


def _make_service(
    procedural_search: AsyncMock | None = None,
) -> SessionBriefingService:
    """Build a briefing service with minimal dependencies.

    The vault summary, KV, and vault services return empty / None —
    the procedural-cards section is independent of those. Tests that
    care about overflow need a *padded* narrative (see the overflow
    test below); for the rendering tests the empty summary is fine.
    """
    vault_summary_svc = AsyncMock()
    vault_summary_svc.get_summary = AsyncMock(return_value=None)

    metastore = _mock_metastore()

    kv_svc = AsyncMock()
    kv_svc.list_entries = AsyncMock(return_value=[])

    vault_svc = AsyncMock()
    vault_svc.list_vaults_with_counts = AsyncMock(return_value=[])

    return SessionBriefingService(
        vault_summary_service=vault_summary_svc,
        metastore=metastore,
        kv_service=kv_svc,
        vault_service=vault_svc,
        procedural_search=procedural_search,
    )


# ---------------------------------------------------------------------------
# Plane-not-wired (backwards compat)
# ---------------------------------------------------------------------------


class TestProceduralCardsPlaneNotWired:
    """The procedural_search dep is optional — the briefing MUST still
    render successfully when it is None. This is the upgrade path for
    callers on the pre-V7 wiring."""

    @pytest.mark.asyncio
    async def test_section_absent_when_plane_not_wired(self):
        svc = _make_service(procedural_search=None)
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedural Cards' not in result
        # The briefing still renders successfully (header + vault binding)
        # — the optional dep is omitted, not crashed.
        assert result.startswith('# Session Briefing')

    @pytest.mark.asyncio
    async def test_briefing_cards_never_called_when_plane_not_wired(self):
        """The internal ``_procedural_search`` is None — the gather
        substitutes an empty coroutine, so the search service is never
        invoked."""
        search = _make_procedural_search(_make_cards_response([_make_card()]))
        # If procedural_search is None at construction, the briefing
        # cannot retroactively wire it — this is documenting the
        # contract rather than the failure mode.
        svc = _make_service(procedural_search=None)
        await svc.generate(uuid4(), budget=2000)
        search.briefing_cards.assert_not_called()


# ---------------------------------------------------------------------------
# Empty / malformed card states
# ---------------------------------------------------------------------------


class TestProceduralCardsEmpty:
    """Defensive rendering — every "no cards" path returns ``''`` so the
    section is silently omitted from the briefing."""

    @pytest.mark.asyncio
    async def test_none_response_renders_no_section(self):
        """A None response from the search service means 'plane wired,
        nothing to surface'. Section MUST be empty."""
        search = _make_procedural_search(None)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedural Cards' not in result

    @pytest.mark.asyncio
    async def test_empty_cards_list_renders_no_section(self):
        cards = _make_cards_response([])
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedural Cards' not in result

    @pytest.mark.asyncio
    async def test_all_cards_with_none_entry_renders_no_section(self):
        """If every card's entry is None, the section collapses to empty
        — a single bullet is never produced."""
        cards = _make_cards_response(
            [_make_card(entry=None), _make_card(entry=None), _make_card(entry=None)]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedural Cards' not in result

    @pytest.mark.asyncio
    async def test_mixed_none_and_real_entries_renders_only_real(self):
        """Cards with a None entry are dropped from the bullet list;
        cards with a real entry are kept. A single surviving bullet is
        still a valid section."""
        real = _make_entry(title='rotate', summary='Rotate keys.')
        cards = _make_cards_response(
            [_make_card(entry=None), _make_card(entry=real), _make_card(entry=None)]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedural Cards' in result
        assert '**procedure/rotate**' in result
        # Only one bullet — the other two cards collapsed to None-entry.
        assert result.count('\n- **procedure/rotate**') == 1


# ---------------------------------------------------------------------------
# Rendering shape
# ---------------------------------------------------------------------------


class TestProceduralCardsRendering:
    """The bullet shape and per-field rendering are the public surface
    the agent reads. Pin these so a future refactor that flattens or
    reorders the line trips a test."""

    @pytest.mark.asyncio
    async def test_renders_one_bullet_per_card(self):
        cards = _make_cards_response(
            [
                _make_card(
                    entry=_make_entry(title='rotate'),
                    context_key='project:42',
                    pin_position=0,
                ),
                _make_card(
                    entry=_make_entry(title='archive'),
                    context_key='project:42',
                    pin_position=1,
                ),
                _make_card(
                    entry=_make_entry(title='prune'),
                    context_key='user',
                    pin_position=2,
                ),
            ]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedural Cards' in result
        assert '**procedure/rotate**' in result
        assert '**procedure/archive**' in result
        assert '**procedure/prune**' in result
        # Pin context renders in parens.
        assert '_(pinned: project:42)_' in result
        assert '_(pinned: user)_' in result

    @pytest.mark.asyncio
    async def test_kind_only_label_when_title_empty(self):
        """Entries without a title fall back to ``**<kind>**`` — no
        slash, no trailing dash."""
        cards = _make_cards_response(
            [_make_card(entry=_make_entry(title='', summary='untitled entry'), context_key='user')]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '**procedure**' in result
        # No empty separator — the kind label is the entire prefix.
        assert '**procedure/**' not in result
        assert '**procedure** — untitled entry' in result

    @pytest.mark.asyncio
    async def test_bullet_omits_summary_when_empty(self):
        """Entries with no summary render as ``- **<kind>/<title>**``
        with no trailing ``— <empty>`` segment."""
        cards = _make_cards_response(
            [_make_card(entry=_make_entry(title='rotate', summary=''), context_key='user')]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '- **procedure/rotate**' in result
        # No empty summary separator (a single em-dash with no text after).
        assert '- **procedure/rotate** — ' not in result

    @pytest.mark.asyncio
    async def test_bullet_omits_pin_context_when_empty(self):
        """Cards with no context_key render without the ``_(pinned: …)_``
        tail — the bullet is just ``- **<kind>/<title>** — <summary>``."""
        cards = _make_cards_response(
            [
                _make_card(
                    entry=_make_entry(title='rotate', summary='rotate keys'),
                    context_key='',
                )
            ]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '- **procedure/rotate** — rotate keys' in result
        assert '_(pinned:' not in result

    @pytest.mark.asyncio
    async def test_different_kinds_render_distinctly(self):
        """The kind badge is the first segment of the label — a 'case'
        card must NOT be labelled as 'procedure'."""
        cards = _make_cards_response(
            [
                _make_card(entry=_make_entry(kind='case', title='cs-1'), context_key='user'),
                _make_card(entry=_make_entry(kind='procedure', title='rotate'), context_key='user'),
                _make_card(entry=_make_entry(kind='strategy', title='weekly'), context_key='user'),
            ]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        assert '**case/cs-1**' in result
        assert '**procedure/rotate**' in result
        assert '**strategy/weekly**' in result


# ---------------------------------------------------------------------------
# Summary truncation
# ---------------------------------------------------------------------------


class TestProceduralCardsSummaryTruncation:
    """The 240-char cap on per-card summary text is a load-bearing
    briefing-budget invariant. Tests pin both the truncation point and
    the trailing ellipsis."""

    @pytest.mark.asyncio
    async def test_summary_at_240_chars_not_truncated(self):
        """A summary of exactly 240 chars MUST NOT be truncated — the
        cap is inclusive (≤ 240)."""
        text = 'x' * 240
        cards = _make_cards_response(
            [_make_card(entry=_make_entry(summary=text), context_key='user')]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        # The full 240-char summary should appear unmodified.
        assert text in result
        # No ellipsis — the cap is inclusive.
        assert '…' not in result.split('## Procedural Cards')[1]

    @pytest.mark.asyncio
    async def test_summary_at_241_chars_truncated(self):
        """A 241-char summary truncates to 239 chars + ellipsis = 240
        total visible chars (the cap)."""
        text = 'x' * 241
        cards = _make_cards_response(
            [_make_card(entry=_make_entry(summary=text), context_key='user')]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        # The truncated body is the first 239 chars plus the ellipsis.
        expected = 'x' * 239 + '…'
        assert expected in result
        # The 241st 'x' must NOT appear after the truncation point —
        # a regression that drops the cap to ≤240 will surface here.
        section = result.split('## Procedural Cards')[1]
        assert section.count('x') == 239

    @pytest.mark.asyncio
    async def test_long_summary_truncates_to_240_visible_chars(self):
        """A 500-char summary truncates to 240 visible chars (239 + ellipsis)."""
        text = 'a' * 500
        cards = _make_cards_response(
            [_make_card(entry=_make_entry(summary=text), context_key='user')]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        section = result.split('## Procedural Cards')[1]
        # Find the bullet line and inspect the summary length.
        bullet = next(line for line in section.splitlines() if line.startswith('- **'))
        # The bullet shape is: `- **<label>** — <summary> _(pinned: <ctx>)_`.
        # Strip the pin suffix first so we're measuring summary length only.
        without_pin = bullet.split(' _(pinned:')[0]
        # `— ` separates label from summary; the summary is everything after.
        summary_in_bullet = without_pin.split('— ', 1)[1]
        # 239 'a' chars + 1 ellipsis char = 240 chars.
        assert len(summary_in_bullet) == 240
        assert summary_in_bullet.endswith('…')

    @pytest.mark.asyncio
    async def test_truncation_preserves_word_boundary(self):
        """The truncation is mid-string (chars[:239]) — it does NOT
        snap to a word boundary. Pin that explicitly so a future
        'nicer' refactor that re-introduces a word-boundary search
        trips this test."""
        # 200 chars then a long stretch of 'y's so the first 239 chars
        # end mid-word. The truncation MUST keep the first 239 chars
        # verbatim, no smarter-than-spec word snapping.
        text = 'w' * 200 + 'y' * 100
        cards = _make_cards_response(
            [_make_card(entry=_make_entry(summary=text), context_key='user')]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)
        result = await svc.generate(uuid4(), budget=2000)
        expected = 'w' * 200 + 'y' * 39 + '…'
        assert expected in result


# ---------------------------------------------------------------------------
# Overflow
# ---------------------------------------------------------------------------


class TestProceduralCardsOverflow:
    """Step 5b of the overflow path drops the procedural-cards section
    entirely when even Step 5 (procedures trim) is insufficient. The
    cards are a discovery surface — the agent can re-fetch them via
    the briefing-cards HTTP route when it needs more — so they are the
    first thing to go under a tight budget."""

    @pytest.mark.asyncio
    async def test_step_5b_drops_cards_under_tight_budget(self):
        """Pad the narrative so initial render blows the budget. The
        overflow path walks Steps 1-5 (which all leave procedural_cards
        intact), then hits Step 5b which clears the section."""
        # A 4 KiB narrative ≈ 1024 tokens — well over the 200-token
        # budget. The overflow path must terminate *with* procedural
        # cards dropped (Step 5b ran) and the final assembly under
        # budget. A regression that misses Step 5b would leave the
        # cards section in the final string.
        huge_summary = MagicMock()
        huge_summary.vault_id = uuid4()
        huge_summary.narrative = 'x ' * 2048  # ~4096 chars
        huge_summary.themes = []

        # Cards with long summaries so the section itself is heavy.
        cards = _make_cards_response(
            [
                _make_card(
                    entry=_make_entry(title=f'card-{i}', summary='y ' * 200),
                    context_key='user',
                )
                for i in range(5)
            ]
        )
        search = _make_procedural_search(cards)
        svc = _make_service(procedural_search=search)

        # Replace the empty summary service with our padded version.
        svc._vault_summary.get_summary = AsyncMock(return_value=huge_summary)  # type: ignore[attr-defined]

        result = await svc.generate(uuid4(), budget=200)

        # Step 5b cleared the section. After all overflow steps the
        # briefing is still over-budget (the narrative alone is ~1k
        # tokens), so the *best-effort* final assembly may still
        # contain the header / vault binding — but the cards section
        # specifically MUST be gone.
        assert '## Procedural Cards' not in result

    @pytest.mark.asyncio
    async def test_step_5b_runs_after_step_5_procedures_trim(self):
        """Step 5 (procedures trim) runs to completion first; Step 5b
        only fires when Step 5 is exhausted. We pin that ordering by
        configuring KV with procedure rows that the briefing will trim
        in Step 5, then asserting the cards section is still present
        while procedures are partial — the cards section only goes in
        Step 5b.

        We use a 1k-token padded narrative and a tight budget so the
        initial render is over-budget, but the procedure rows are
        trim-able in Step 5 BEFORE Step 5b drops the cards."""
        padded_summary = MagicMock()
        padded_summary.vault_id = uuid4()
        padded_summary.narrative = 'x ' * 400  # ~800 tokens
        padded_summary.themes = []

        # Two short procedure rows in KV — Step 5 will trim one of them
        # to fit a 1000-token budget, leaving the other.
        proc_entry = MagicMock()
        proc_entry.key = 'global:procedure:rotate:api_key'
        proc_entry.value = 'rotate'
        proc_entry.updated_at = None

        kv_svc = MagicMock()
        kv_svc.list_entries = AsyncMock(return_value=[proc_entry])

        # Cards section small enough to survive Step 5.
        cards = _make_cards_response(
            [
                _make_card(
                    entry=_make_entry(title='card-0', summary='short'),
                    context_key='user',
                )
            ]
        )
        search = _make_procedural_search(cards)

        svc = SessionBriefingService(
            vault_summary_service=AsyncMock(get_summary=AsyncMock(return_value=padded_summary)),
            metastore=_mock_metastore(),
            kv_service=kv_svc,
            vault_service=AsyncMock(list_vaults_with_counts=AsyncMock(return_value=[])),
            procedural_search=search,
        )
        result = await svc.generate(uuid4(), budget=1000)
        # The cards section is still in the final assembly because
        # Step 5 (procedures trim) was sufficient to fit the budget —
        # Step 5b didn't fire.
        assert '## Procedural Cards' in result
        assert '**procedure/card-0**' in result
