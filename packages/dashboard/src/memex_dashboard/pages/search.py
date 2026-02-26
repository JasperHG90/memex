import asyncio
import logging
import re
from typing import Any, Optional

import markdown as md_lib
import reflex as rx
from pydantic import BaseModel

from .. import style
from ..api import api_client
from ..vault_state import VaultState

logger = logging.getLogger('memex.dashboard.search')


class SearchResult(BaseModel):
    id: str
    text: str
    fact_type: str = 'unknown'
    score: float = 0.0
    metadata: dict[str, Any] = {}
    source_note_ids: list[str] = []


_ALL_STRATEGIES: list[str] = ['semantic', 'keyword', 'graph', 'temporal', 'mental_model']


class SearchState(rx.State):
    query: str = ''
    results: list[SearchResult] = []
    is_loading: bool = False

    # Pagination
    limit: int = 10
    has_more: bool = False

    # Modal State
    selected_result: Optional[SearchResult] = None
    metadata_list: list[dict[str, str]] = []
    is_modal_open: bool = False

    # Summary State
    summary_text: str = ''
    summary_html: str = ''
    is_summary_loading: bool = False
    show_summary: bool = False
    _summary_generation: int = 0

    # View Mode (Explicitly managed)
    mode: str = 'idle'

    # Strategy filter state (5 TEMPR strategies)
    active_strategies: list[str] = ['semantic', 'keyword', 'graph', 'temporal', 'mental_model']
    is_filter_panel_open: bool = False

    def handle_citation_click(self, value: str):
        """Handle citation click from JS bridge (receives index as string)."""
        try:
            index = int(value)
            if 0 <= index < len(self.results):
                self.open_details(self.results[index])
        except (ValueError, IndexError) as e:
            logger.warning(f'Failed to handle citation click {value}: {e}')

    def set_query(self, value: str):
        self.query = value

    def open_details(self, result: SearchResult):
        self.selected_result = result
        # Convert metadata to list of pairs for stable rendering
        self.metadata_list = [{'key': str(k), 'value': str(v)} for k, v in result.metadata.items()]
        self.is_modal_open = True

    def close_details(self, value: bool = False):
        self.is_modal_open = value

    def toggle_summary(self, value: bool):
        self.show_summary = value
        if value and self.results and not self.summary_text:
            return SearchState.generate_summary

    # CSS to restore list styling stripped by Radix theme reset
    _SUMMARY_CSS = (
        '<style>'
        '.summary-html ul{list-style-type:disc;padding-left:1.5em;margin:0.5em 0}'
        '.summary-html ol{list-style-type:decimal;padding-left:1.5em;margin:0.5em 0}'
        '.summary-html li{margin-bottom:0.25em}'
        '.summary-html li>p{margin:0}'
        '.summary-html p{margin:0.5em 0}'
        '</style>'
    )

    @staticmethod
    def _expand_citations(match: re.Match[str]) -> str:
        """Expand [N] or [N, M, ...] into individual markdown links."""
        numbers = re.findall(r'\d+', match.group(0))
        return ', '.join(f'[[{n}]](#cite-{n})' for n in numbers)

    @staticmethod
    def _cite_onclick(idx: str) -> str:
        """Build self-contained inline JS for a citation click."""
        return (
            'var i=document.getElementById("__cite-bridge");'
            'if(i){Object.getOwnPropertyDescriptor('
            f'window.HTMLInputElement.prototype,"value").set.call(i,"{idx}");'
            'i.dispatchEvent(new Event("input",{bubbles:true}))}'
            'return false;'
        )

    @classmethod
    def _build_summary_html(cls, raw_summary: str) -> str:
        """Convert markdown summary with [N] citations to HTML with clickable links."""
        text = raw_summary

        # Ensure blank lines before list markers for proper markdown parsing
        text = re.sub(r'(\S)\n(\s*[\*\-\+]\s)', r'\1\n\n\2', text)
        text = re.sub(r'(\S)\n(\s*\d+\.\s)', r'\1\n\n\2', text)

        # Expand single [N] and multi [N, M] citations to markdown links
        text = re.sub(r'\[[\d,\s]+\]', cls._expand_citations, text)

        # Convert markdown to HTML
        html = md_lib.markdown(text)

        # Style citation <a> tags with self-contained onclick handlers
        def _style_cite(m: re.Match[str]) -> str:
            idx = m.group(1)
            onclick = cls._cite_onclick(idx)
            return (
                f'<a href="javascript:void(0)" onclick=\'{onclick}\' '
                f'style="color:#3b82f6;font-weight:bold;text-decoration:underline;'
                f'cursor:pointer;">[{idx}]</a>'
            )

        html = re.sub(r'<a href="#cite-(\d+)">\[(\d+)\]</a>', _style_cite, html)

        return cls._SUMMARY_CSS + html

    async def generate_summary(self):
        """Generate AI summary from current search results."""
        if not self.results or self.is_summary_loading:
            return

        self._summary_generation += 1
        current_gen = self._summary_generation

        self.is_summary_loading = True
        self.summary_text = ''
        self.summary_html = ''
        yield
        await asyncio.sleep(0.1)

        try:
            # Api endpoint allows max 20 results
            texts = [r.text for r in self.results[:50]]
            response = await api_client.api.summarize(query=self.query, texts=texts)

            # Race condition guard: only update if this is still the latest generation
            if current_gen == self._summary_generation:
                logger.info(f'Raw summary from LLM: {response.summary[:300]}...')
                self.summary_html = self._build_summary_html(response.summary)
                self.summary_text = response.summary
        except Exception as e:
            logger.warning(f'Summary generation failed: {e}')
            if current_gen == self._summary_generation:
                self.summary_text = 'Summary generation failed. Please try again.'
                self.summary_html = '<p>Summary generation failed. Please try again.</p>'
        finally:
            if current_gen == self._summary_generation:
                self.is_summary_loading = False

    async def perform_search(self):
        if not self.query:
            return

        self.is_loading = True
        self.mode = 'loading'
        self.results = []
        self.summary_text = ''
        self.summary_html = ''
        yield
        await asyncio.sleep(0.1)

        try:
            # Scope search to selected vaults
            vault_state = await self.get_state(VaultState)
            vault_ids = vault_state.all_selected_vault_ids or None

            # Only pass strategies when not all are selected (let backend use defaults)
            strategies = self.active_strategies if len(self.active_strategies) < 5 else None

            # NB: skip opinion formation here to avoid flooding the API with new opinions to generate
            memory_units = await api_client.api.search(
                query=self.query,
                limit=self.limit,
                skip_opinion_formation=True,
                vault_ids=vault_ids,
                strategies=strategies,
            )

            # Convert DTOs to models for Reflex state
            new_results = []
            for unit in memory_units:
                # Clean fact type
                raw_type = str(getattr(unit, 'fact_type', 'unknown'))
                if '.' in raw_type:
                    clean_type = raw_type.split('.')[-1].lower()
                else:
                    clean_type = raw_type.lower()

                new_results.append(
                    SearchResult(
                        id=str(getattr(unit, 'id', '')),
                        text=getattr(unit, 'text', ''),
                        fact_type=clean_type,
                        score=float(getattr(unit, 'score') or 0.0),
                        metadata=getattr(unit, 'metadata', {}) or {},
                        source_note_ids=[str(uid) for uid in getattr(unit, 'source_note_ids', [])],
                    )
                )

            self.results = new_results
            self.mode = 'results' if new_results else 'idle'
            self.has_more = len(new_results) >= self.limit

        except Exception as e:
            print(f'Search failed: {e}')
            self.mode = 'idle'
            rx.window_alert(f'Search failed: {e}')
        finally:
            self.is_loading = False

        # Auto-trigger summary if toggle is on
        if self.show_summary and self.results:
            yield SearchState.generate_summary

    async def load_more(self):
        if not self.query or self.is_loading:
            return

        self.is_loading = True
        self.mode = 'loading'
        yield
        await asyncio.sleep(0.1)
        try:
            current_count = len(self.results)

            # Scope search to selected vaults
            vault_state = await self.get_state(VaultState)
            vault_ids = vault_state.all_selected_vault_ids or None

            # Only pass strategies when not all are selected
            strategies = self.active_strategies if len(self.active_strategies) < 5 else None

            # Offset search
            memory_units = await api_client.api.search(
                query=self.query,
                limit=self.limit,
                offset=current_count,
                skip_opinion_formation=True,
                vault_ids=vault_ids,
                strategies=strategies,
            )

            new_results = []
            for unit in memory_units:
                # Clean fact type
                raw_type = str(getattr(unit, 'fact_type', 'unknown'))
                if '.' in raw_type:
                    clean_type = raw_type.split('.')[-1].lower()
                else:
                    clean_type = raw_type.lower()

                new_results.append(
                    SearchResult(
                        id=str(getattr(unit, 'id', '')),
                        text=getattr(unit, 'text', ''),
                        fact_type=clean_type,
                        score=float(getattr(unit, 'score') or 0.0),
                        metadata=getattr(unit, 'metadata', {}) or {},
                        source_note_ids=[str(uid) for uid in getattr(unit, 'source_note_ids', [])],
                    )
                )

            self.results.extend(new_results)
            self.mode = 'results'
            self.has_more = len(new_results) >= self.limit
        except Exception as e:
            print(f'Load more failed: {e}')
            self.mode = 'results' if self.results else 'idle'
        finally:
            self.is_loading = False

    def handle_key_down(self, key: str):
        if key == 'Enter':
            return SearchState.perform_search

    def on_submit(self):
        return SearchState.perform_search

    def toggle_filter_panel(self):
        self.is_filter_panel_open = not self.is_filter_panel_open

    def toggle_strategy(self, strategy: str):
        if strategy in self.active_strategies:
            # Keep at least one strategy active
            if len(self.active_strategies) > 1:
                self.active_strategies = [s for s in self.active_strategies if s != strategy]
        else:
            self.active_strategies = [*self.active_strategies, strategy]

    def reset_strategies(self):
        self.active_strategies = ['semantic', 'keyword', 'graph', 'temporal', 'mental_model']


def render_kv_row(k: str, v: str) -> rx.Component:
    return rx.table.row(
        rx.table.cell(
            rx.text(k, font_weight='bold', color='gray', font_size='10px'), padding_y='1'
        ),
        rx.table.cell(rx.text(v, font_size='10px', color='white'), padding_y='1'),
        border_bottom=f'1px solid {style.BORDER_COLOR}',
    )


def type_badge(type_str: str) -> rx.Component:
    return rx.badge(
        type_str.replace('_', ' '),
        variant='solid',
        bg=rx.match(
            type_str,
            ('asset', '#8b5cf6'),
            ('document', '#10b981'),
            ('memory_unit', '#3b82f6'),
            ('observation', '#f59e0b'),
            ('mental_model', '#ef4444'),
            '#888',
        ),
        color='white',
        border_radius='6px',
        padding_x='8px',
        font_size='10px',
        text_transform='capitalize',
    )


def detail_modal() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title('Memory Details'),
            rx.cond(
                SearchState.selected_result,
                rx.vstack(
                    type_badge(SearchState.selected_result.fact_type),  # type: ignore
                    rx.text.strong('Content:'),
                    rx.text(SearchState.selected_result.text, size='2'),  # type: ignore
                    rx.divider(),
                    rx.text.strong('Metadata:'),
                    rx.scroll_area(
                        rx.table.root(
                            rx.table.body(
                                rx.foreach(
                                    SearchState.metadata_list,
                                    lambda item: render_kv_row(item['key'], item['value']),
                                )
                            ),
                            width='100%',
                        ),
                        height='300px',
                    ),
                    spacing='4',
                    align='stretch',
                ),
                rx.spinner(),
            ),
            rx.dialog.close(
                rx.button('Close', variant='soft', color_scheme='gray', margin_top='16px'),
            ),
        ),
        open=SearchState.is_modal_open,
        on_open_change=SearchState.close_details,
    )


def result_card(result: SearchResult) -> rx.Component:
    return rx.box(
        rx.hstack(
            type_badge('memory_unit'),
            rx.badge(
                result.fact_type, variant='soft', color_scheme='gray', text_transform='capitalize'
            ),
            width='100%',
            margin_bottom='4px',
        ),
        rx.text(result.text, font_size='14px', margin_bottom='8px', no_of_lines=3),
        rx.divider(margin_y='2'),
        rx.hstack(
            rx.button(
                'Details',
                size='1',
                variant='soft',
                on_click=lambda: SearchState.open_details(result),  # type: ignore
            ),
            rx.button(
                'View Lineage',
                size='1',
                variant='outline',
                on_click=rx.redirect(f'/lineage?id={result.id}&type=memory_unit'),
            ),
            # Entity Graph Link (Placeholder for now until Entity page is ready)
            rx.button(
                'Entity Graph',
                size='1',
                variant='ghost',
                disabled=True,  # Todo: Enable when entity page supports ID param
            ),
            spacing='2',
        ),
        width='100%',
        padding='16px',
        bg=style.SIDEBAR_BG,
        border_radius='8px',
        border=f'1px solid {style.BORDER_COLOR}',
        margin_bottom='12px',
        _hover={'border_color': style.ACCENT_COLOR},
        transition='border-color 0.2s',
    )


def render_summary_html() -> rx.Component:
    """Render markdown-formatted summary with clickable inline citations."""
    return rx.box(
        rx.html(
            SearchState.summary_html,
            class_name='summary-html',
        ),
        # Hidden input that bridges JS onclick -> Reflex on_change handler.
        # The onclick in citations is self-contained JS (no global function needed).
        rx.el.input(
            id='__cite-bridge',
            type='text',
            on_change=SearchState.handle_citation_click,
            style={
                'position': 'absolute',
                'width': '0',
                'height': '0',
                'opacity': '0',
                'overflow': 'hidden',
                'pointer-events': 'none',
            },
        ),
        font_size='13px',
        color=style.TEXT_COLOR,
        line_height='1.6',
        position='relative',
    )


def summary_card() -> rx.Component:
    """AI summary card displayed between the search bar and results."""
    return rx.cond(
        SearchState.show_summary,
        rx.box(
            rx.hstack(
                rx.icon('sparkles', size=16, color=style.ACCENT_COLOR),
                rx.text('AI Summary', font_weight='bold', font_size='14px'),
                rx.spacer(),
                rx.cond(
                    SearchState.is_summary_loading,
                    rx.spinner(size='1'),
                    rx.fragment(),
                ),
                width='100%',
                margin_bottom='8px',
            ),
            rx.cond(
                SearchState.is_summary_loading,
                rx.center(
                    rx.vstack(
                        rx.spinner(size='2', color=style.ACCENT_COLOR),
                        rx.text(
                            'Generating summary...', color=style.SECONDARY_TEXT, font_size='13px'
                        ),
                        align='center',
                        spacing='2',
                    ),
                    width='100%',
                    padding='20px',
                ),
                rx.cond(
                    SearchState.summary_html,
                    render_summary_html(),
                    rx.box(width='100%'),
                ),
            ),
            width='100%',
            padding='16px',
            bg='rgba(59, 130, 246, 0.05)',
            border_radius='8px',
            border=f'1px solid {style.ACCENT_COLOR}',
            margin_bottom='12px',
        ),
    )


def _strategy_switch(strategy: str) -> rx.Component:
    """Single strategy switch row for the filter panel."""
    return rx.hstack(
        rx.switch(
            checked=SearchState.active_strategies.contains(strategy),  # type: ignore[attr-defined]
            on_change=lambda v: SearchState.toggle_strategy(strategy),  # type: ignore[call-arg, arg-type]
            size='1',
        ),
        rx.text(strategy.replace('_', ' ').title(), size='2', color=style.TEXT_COLOR),
        spacing='2',
        align='center',
    )


def search_filter_panel() -> rx.Component:
    """Collapsible panel for selecting TEMPR retrieval strategies."""
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.text('Search Filters', font_weight='bold', size='2', color='white'),
                rx.spacer(),
                rx.icon(
                    rx.cond(SearchState.is_filter_panel_open, 'chevron-up', 'chevron-down'),
                    size=16,
                    cursor='pointer',
                ),
                width='100%',
                align='center',
                on_click=SearchState.toggle_filter_panel,
                cursor='pointer',
            ),
            rx.cond(
                SearchState.is_filter_panel_open,
                rx.vstack(
                    rx.divider(margin_y='2'),
                    rx.text('Retrieval Strategies', size='1', color='gray'),
                    _strategy_switch('semantic'),
                    _strategy_switch('keyword'),
                    _strategy_switch('graph'),
                    _strategy_switch('temporal'),
                    _strategy_switch('mental_model'),
                    rx.button(
                        'Reset',
                        size='1',
                        variant='soft',
                        color_scheme='gray',
                        on_click=SearchState.reset_strategies,
                        margin_top='4px',
                    ),
                    spacing='2',
                    width='100%',
                    padding_top='2',
                ),
            ),
            bg=style.SIDEBAR_BG,
            border=f'1px solid {style.BORDER_COLOR}',
            border_radius='8px',
            padding='12px',
            width='100%',
        ),
        width='100%',
    )


def search_page() -> rx.Component:
    return rx.vstack(
        rx.heading('Memory Search', size='8'),
        rx.hstack(
            rx.input(
                placeholder='Search memories, entities, and documents...',
                value=SearchState.query,
                on_change=SearchState.set_query,
                on_key_down=SearchState.handle_key_down,
                width='100%',
                size='3',
            ),
            rx.button(
                rx.cond(
                    SearchState.is_loading,
                    rx.spinner(size='1'),
                    rx.icon('search', size=18),
                ),
                on_click=SearchState.perform_search,
                size='3',
                bg=style.ACCENT_COLOR,
                disabled=SearchState.is_loading,
            ),
            width='100%',
            spacing='2',
        ),
        # Strategy filter panel
        search_filter_panel(),
        # Summary toggle
        rx.hstack(
            rx.text('AI Summary', font_size='13px', color=style.SECONDARY_TEXT),
            rx.switch(
                checked=SearchState.show_summary,
                on_change=SearchState.toggle_summary,
                size='1',
            ),
            spacing='2',
            align='center',
        ),
        # Summary card (conditionally rendered)
        summary_card(),
        rx.match(
            SearchState.mode,
            (
                'loading',
                rx.center(
                    rx.vstack(
                        rx.spinner(size='3', color=style.ACCENT_COLOR),
                        rx.heading('Searching memories...', size='5'),
                        align='center',
                        spacing='4',
                    ),
                    width='100%',
                    height='300px',
                ),
            ),
            (
                'results',
                rx.vstack(
                    rx.foreach(SearchState.results, result_card),
                    width='100%',
                    spacing='2',
                ),
            ),
            rx.box(
                rx.text('Enter a query to start searching.', color=style.SECONDARY_TEXT),
                width='100%',
                padding='40px',
                bg=style.SIDEBAR_BG,
                border_radius='12px',
                border=f'1px solid {style.BORDER_COLOR}',
                text_align='center',
            ),
        ),
        detail_modal(),
        spacing='6',
        width='100%',
    )
