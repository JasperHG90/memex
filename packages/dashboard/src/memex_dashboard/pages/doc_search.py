import asyncio
import logging
import re
from typing import Any, Optional
from uuid import UUID

import markdown as md_lib
import reflex as rx
from pydantic import BaseModel

from .. import style
from ..api import api_client
from ..vault_state import VaultState
from .search import render_kv_row

logger = logging.getLogger('memex.dashboard.doc_search')


class DocSnippet(BaseModel):
    text: str
    score: float = 0.0
    node_title: Optional[str] = None
    node_level: Optional[int] = None


class DocResult(BaseModel):
    note_id: str
    title: str
    score: float = 0.0
    snippets: list[DocSnippet] = []
    metadata: dict[str, Any] = {}


class PageIndexNode(BaseModel):
    title: str
    level: int
    depth: int
    prefix: str = ''  # Box-drawing tree connector, e.g. "│   ├── "
    summary: str = ''  # Formatted 5W summary for display


_ALL_DOC_STRATEGIES: list[str] = ['semantic', 'keyword', 'graph', 'temporal']

# CSS to restore list styling stripped by Radix theme reset
_DOC_SUMMARY_CSS = (
    '<style>'
    '.doc-summary-html ul{list-style-type:disc;padding-left:1.5em;margin:0.5em 0}'
    '.doc-summary-html ol{list-style-type:decimal;padding-left:1.5em;margin:0.5em 0}'
    '.doc-summary-html li{margin-bottom:0.25em}'
    '.doc-summary-html li>p{margin:0}'
    '.doc-summary-html p{margin:0.5em 0}'
    '</style>'
)


def _extract_title(metadata: dict[str, Any]) -> str:
    """Extract note title from metadata dict."""
    return str(metadata.get('title') or metadata.get('name') or 'Untitled')


def _flatten_page_index(
    nodes: Any,
    depth: int = 0,
    _ancestor_is_last: list[bool] | None = None,
) -> list[PageIndexNode]:
    """Recursively flatten a page index tree into a flat list with depth and tree connectors.

    Each node gets a ``prefix`` built from box-drawing characters (├──, └──, │) so
    the flat list can be rendered as a proper indented tree with rx.foreach.
    """
    if _ancestor_is_last is None:
        _ancestor_is_last = []

    result: list[PageIndexNode] = []

    if isinstance(nodes, list):
        valid = [n for n in nodes if isinstance(n, dict) and n.get('title')]
        for i, item in enumerate(valid):
            result.extend(
                _flatten_page_index(item, depth, [*_ancestor_is_last, i == len(valid) - 1])
            )
    elif isinstance(nodes, dict):
        title = str(nodes.get('title', nodes.get('name', '')))
        level = int(nodes.get('level', depth))

        # Format 5W summary fields into a single readable string
        summary = ''
        raw_summary = nodes.get('summary')
        if isinstance(raw_summary, dict):
            parts = [
                str(v) for k in ('who', 'what', 'how', 'when', 'where') if (v := raw_summary.get(k))
            ]
            summary = ' | '.join(parts)

        # Build box-drawing tree connector prefix
        # _ancestor_is_last[-1] = is this node the last among its siblings
        # _ancestor_is_last[1:-1] = is_last flags for depth-1..depth-d ancestors
        if depth == 0:
            prefix = ''
        else:
            parts_list: list[str] = []
            for is_last_anc in _ancestor_is_last[1:-1]:
                parts_list.append('    ' if is_last_anc else '│   ')
            is_last = _ancestor_is_last[-1] if _ancestor_is_last else False
            parts_list.append('└── ' if is_last else '├── ')
            prefix = ''.join(parts_list)

        if title:
            result.append(
                PageIndexNode(title=title, level=level, depth=depth, prefix=prefix, summary=summary)
            )

        children = nodes.get('children') or []
        if isinstance(children, list):
            valid_ch = [c for c in children if isinstance(c, dict) and c.get('title')]
            for i, child in enumerate(valid_ch):
                result.extend(
                    _flatten_page_index(
                        child, depth + 1, [*_ancestor_is_last, i == len(valid_ch) - 1]
                    )
                )

    return result


class DocSearchState(rx.State):
    query: str = ''
    results: list[DocResult] = []
    is_loading: bool = False
    mode: str = 'idle'  # 'idle' | 'loading' | 'results'
    limit: int = 10
    has_more: bool = False

    # Strategy filter state
    active_strategies: list[str] = ['semantic', 'keyword', 'graph', 'temporal']
    is_filter_panel_open: bool = False

    # Detail modal
    selected_result: Optional[DocResult] = None
    selected_doc_content: str = ''
    selected_doc_metadata_list: list[dict[str, str]] = []
    page_index_nodes: list[PageIndexNode] = []
    is_modal_open: bool = False
    is_content_loading: bool = False
    is_page_index_loading: bool = False

    # Summary state
    summary_text: str = ''
    summary_html: str = ''
    is_summary_loading: bool = False
    show_summary: bool = False
    _summary_generation: int = 0

    _SUMMARY_CSS: str = _DOC_SUMMARY_CSS

    def set_query(self, value: str):
        self.query = value

    def toggle_filter_panel(self):
        self.is_filter_panel_open = not self.is_filter_panel_open

    def toggle_strategy(self, strategy: str):
        if strategy in self.active_strategies:
            if len(self.active_strategies) > 1:
                self.active_strategies = [s for s in self.active_strategies if s != strategy]
        else:
            self.active_strategies = [*self.active_strategies, strategy]

    def reset_strategies(self):
        self.active_strategies = ['semantic', 'keyword', 'graph', 'temporal']

    def close_details(self, value: bool = False):
        self.is_modal_open = value

    def toggle_summary(self, value: bool):
        self.show_summary = value
        if value and self.results and not self.summary_text:
            return DocSearchState.generate_summary

    def handle_citation_click(self, value: str):
        """Handle citation click from JS bridge (receives index as string)."""
        try:
            index = int(value)
            if 0 <= index < len(self.results):
                self.open_details(self.results[index])
        except (ValueError, IndexError) as e:
            logger.warning(f'Failed to handle citation click {value}: {e}')

    def handle_key_down(self, key: str):
        if key == 'Enter':
            return DocSearchState.perform_search

    def on_submit(self):
        return DocSearchState.perform_search

    @staticmethod
    def _expand_citations(match: re.Match[str]) -> str:
        """Expand [N] or [N, M, ...] into individual markdown links."""
        numbers = re.findall(r'\d+', match.group(0))
        return ', '.join(f'[[{n}]](#cite-{n})' for n in numbers)

    @staticmethod
    def _cite_onclick(idx: str) -> str:
        """Build self-contained inline JS for a citation click."""
        return (
            'var i=document.getElementById("__cite-bridge-doc");'
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
        """Generate AI summary from current document search results."""
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
            texts = [
                snippet.text for result in self.results[:20] for snippet in result.snippets[:2]
            ]
            response = await api_client.api.summarize(query=self.query, texts=texts)

            if current_gen == self._summary_generation:
                logger.info(f'Raw doc summary from LLM: {response.summary[:300]}...')
                self.summary_html = self._build_summary_html(response.summary)
                self.summary_text = response.summary
        except Exception as e:
            logger.warning(f'Doc summary generation failed: {e}')
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
            vault_state = await self.get_state(VaultState)
            vault_ids = vault_state.all_selected_vault_ids or None

            # Only pass strategies when not all are selected
            strategies: list[str] | None = (
                self.active_strategies if len(self.active_strategies) < 4 else None
            )

            doc_results = await api_client.api.search_notes(
                query=self.query,
                limit=self.limit,
                vault_ids=vault_ids,
                strategies=strategies,
            )

            new_results = []
            for dr in doc_results:
                title = _extract_title(dr.metadata)
                # Limit to top 2 snippets, capped at 300 chars each for card preview
                snippets = [
                    DocSnippet(
                        text=s.text[:300],
                        score=s.score,
                        node_title=s.node_title,
                        node_level=s.node_level,
                    )
                    for s in dr.snippets[:2]
                ]
                new_results.append(
                    DocResult(
                        note_id=str(dr.note_id),
                        title=title,
                        score=dr.score,
                        snippets=snippets,
                        metadata=dr.metadata,
                    )
                )

            self.results = new_results
            self.mode = 'results' if new_results else 'idle'
            self.has_more = len(new_results) >= self.limit

        except Exception as e:
            logger.warning(f'Document search failed: {e}')
            self.mode = 'idle'
        finally:
            self.is_loading = False

        if self.show_summary and self.results:
            yield DocSearchState.generate_summary

    async def open_details(self, result: DocResult):
        self.selected_result = result
        self.selected_doc_content = ''
        self.selected_doc_metadata_list = [
            {'key': str(k), 'value': str(v)} for k, v in result.metadata.items()
        ]
        self.page_index_nodes = []
        self.is_content_loading = True
        self.is_page_index_loading = True
        self.is_modal_open = True
        yield

        async def _get_content() -> str:
            try:
                note = await api_client.api.get_note(UUID(result.note_id))
                return note.original_text or ''
            except Exception as e:
                logger.warning(f'Failed to load note content for {result.note_id}: {e}')
                return ''

        async def _get_page_index() -> list[PageIndexNode]:
            try:
                raw = await api_client.api.get_note_page_index(UUID(result.note_id))
                return _flatten_page_index(raw) if raw is not None else []
            except Exception as e:
                logger.warning(f'Failed to load page index for {result.note_id}: {e}')
                return []

        content, nodes = await asyncio.gather(_get_content(), _get_page_index())
        self.selected_doc_content = content
        self.page_index_nodes = nodes
        self.is_content_loading = False
        self.is_page_index_loading = False


# ---------------------------------------------------------------------------
# UI Components
# ---------------------------------------------------------------------------


def _doc_strategy_switch(strategy: str) -> rx.Component:
    """Single strategy switch row for the document filter panel."""
    return rx.hstack(
        rx.switch(
            checked=DocSearchState.active_strategies.contains(strategy),  # type: ignore[attr-defined]
            on_change=lambda v: DocSearchState.toggle_strategy(strategy),  # type: ignore[call-arg, arg-type]
            size='1',
        ),
        rx.text(strategy.replace('_', ' ').title(), size='2', color=style.TEXT_COLOR),
        spacing='2',
        align='center',
    )


def doc_filter_panel() -> rx.Component:
    """Collapsible panel for selecting note retrieval strategies."""
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.text('Search Filters', font_weight='bold', size='2', color='white'),
                rx.spacer(),
                rx.icon(
                    rx.cond(DocSearchState.is_filter_panel_open, 'chevron-up', 'chevron-down'),
                    size=16,
                    cursor='pointer',
                ),
                width='100%',
                align='center',
                on_click=DocSearchState.toggle_filter_panel,
                cursor='pointer',
            ),
            rx.cond(
                DocSearchState.is_filter_panel_open,
                rx.vstack(
                    rx.divider(margin_y='2'),
                    rx.text('Retrieval Strategies', size='1', color='gray'),
                    _doc_strategy_switch('semantic'),
                    _doc_strategy_switch('keyword'),
                    _doc_strategy_switch('graph'),
                    _doc_strategy_switch('temporal'),
                    rx.button(
                        'Reset',
                        size='1',
                        variant='soft',
                        color_scheme='gray',
                        on_click=DocSearchState.reset_strategies,
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


def render_page_index_node(node: PageIndexNode) -> rx.Component:
    """Render a single page index node as a tree entry with box-drawing connectors."""
    return rx.box(
        rx.vstack(
            # Title row: monospace prefix + title text
            rx.hstack(
                rx.cond(
                    node.prefix != '',
                    rx.text(
                        node.prefix,
                        font_family='monospace',
                        font_size='12px',
                        color='rgba(120,120,180,0.55)',
                        white_space='pre',
                        flex_shrink='0',
                        line_height='1.5',
                    ),
                    rx.fragment(),
                ),
                rx.text(
                    node.title,
                    font_size=rx.cond(node.depth == 0, '13px', '12px'),
                    font_weight=rx.cond(node.depth == 0, '600', '400'),
                    color=rx.cond(node.depth == 0, 'white', style.TEXT_COLOR),
                    flex='1',
                    line_height='1.5',
                ),
                spacing='0',
                align='baseline',
                width='100%',
            ),
            # Summary row (if present): indented to align with title text
            rx.cond(
                node.summary != '',
                rx.text(
                    node.summary,
                    font_size='11px',
                    color=style.SECONDARY_TEXT,
                    line_height='1.4',
                    padding_left=node.depth * 28,  # type: ignore[operator]
                    opacity='0.8',
                ),
                rx.fragment(),
            ),
            spacing='0',
            align='start',
            width='100%',
        ),
        padding_y='5px',
        border_bottom='1px solid rgba(255,255,255,0.04)',
        width='100%',
        _hover={'background': 'rgba(255,255,255,0.02)'},
    )


def doc_detail_modal() -> rx.Component:
    """Modal showing full note content, page index tree, and metadata."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title(
                rx.cond(
                    DocSearchState.selected_result,
                    DocSearchState.selected_result.title,  # type: ignore[union-attr]
                    'Note Details',
                )
            ),
            rx.cond(
                DocSearchState.selected_result,
                rx.flex(
                    # ── Left column: full note content ──────────────────
                    rx.vstack(
                        rx.text(
                            'Content',
                            font_weight='bold',
                            font_size='12px',
                            color='gray',
                            text_transform='uppercase',
                            letter_spacing='0.05em',
                        ),
                        rx.cond(
                            DocSearchState.is_content_loading,
                            rx.center(rx.spinner(size='2'), width='100%', padding='40px'),
                            rx.cond(
                                DocSearchState.selected_doc_content != '',
                                rx.scroll_area(
                                    rx.box(
                                        rx.markdown(DocSearchState.selected_doc_content),
                                        font_size='13px',
                                        color=style.TEXT_COLOR,
                                        line_height='1.65',
                                    ),
                                    height='480px',
                                    width='100%',
                                ),
                                rx.text(
                                    'No content available.',
                                    color=style.SECONDARY_TEXT,
                                    font_size='13px',
                                ),
                            ),
                        ),
                        spacing='2',
                        align='start',
                        flex='2',
                        min_width='0',
                    ),
                    # ── Right column: page index + metadata ──────────────────
                    rx.vstack(
                        # Page Index
                        rx.text(
                            'Page Index',
                            font_weight='bold',
                            font_size='12px',
                            color='gray',
                            text_transform='uppercase',
                            letter_spacing='0.05em',
                        ),
                        rx.cond(
                            DocSearchState.is_page_index_loading,
                            rx.center(rx.spinner(size='2'), width='100%', padding='16px'),
                            rx.cond(
                                DocSearchState.page_index_nodes,
                                rx.scroll_area(
                                    rx.vstack(
                                        rx.foreach(
                                            DocSearchState.page_index_nodes,
                                            render_page_index_node,
                                        ),
                                        spacing='0',
                                        width='100%',
                                    ),
                                    height='250px',
                                    width='100%',
                                ),
                                rx.text(
                                    'No page index available.',
                                    color=style.SECONDARY_TEXT,
                                    font_size='13px',
                                ),
                            ),
                        ),
                        rx.divider(margin_y='2'),
                        # Metadata
                        rx.text(
                            'Metadata',
                            font_weight='bold',
                            font_size='12px',
                            color='gray',
                            text_transform='uppercase',
                            letter_spacing='0.05em',
                        ),
                        rx.scroll_area(
                            rx.table.root(
                                rx.table.body(
                                    rx.foreach(
                                        DocSearchState.selected_doc_metadata_list,
                                        lambda item: render_kv_row(item['key'], item['value']),
                                    )
                                ),
                                width='100%',
                            ),
                            height='180px',
                            width='100%',
                        ),
                        spacing='2',
                        align='start',
                        flex='1',
                        min_width='240px',
                        border_left=f'1px solid {style.BORDER_COLOR}',
                        padding_left='16px',
                    ),
                    spacing='4',
                    width='100%',
                    align='start',
                ),
                rx.center(rx.spinner(size='2'), width='100%', padding='40px'),
            ),
            rx.dialog.close(
                rx.button('Close', variant='soft', color_scheme='gray', margin_top='16px'),
            ),
            max_width='960px',
            width='90vw',
        ),
        open=DocSearchState.is_modal_open,
        on_open_change=DocSearchState.close_details,
    )


def doc_snippet_preview(snippet: DocSnippet) -> rx.Component:
    """Preview a single snippet inside a result card."""
    return rx.box(
        rx.cond(
            snippet.node_title,
            rx.text(
                snippet.node_title,
                font_size='10px',
                color=style.ACCENT_COLOR,
                font_weight='bold',
                margin_bottom='2px',
            ),
            rx.fragment(),
        ),
        rx.text(snippet.text, font_size='12px', color=style.SECONDARY_TEXT, no_of_lines=2),
        padding='8px',
        bg='rgba(255,255,255,0.03)',
        border_radius='4px',
        border=f'1px solid {style.BORDER_COLOR}',
        width='100%',
    )


def doc_result_card(result: DocResult) -> rx.Component:
    """Card displaying a single document search result."""
    return rx.box(
        rx.hstack(
            rx.text(result.title, font_weight='bold', font_size='14px', flex='1'),
            rx.badge(
                result.score,
                variant='soft',
                color_scheme='blue',
                font_size='10px',
            ),
            width='100%',
            align='center',
            margin_bottom='8px',
        ),
        rx.vstack(
            rx.foreach(
                result.snippets,
                doc_snippet_preview,
            ),
            spacing='2',
            width='100%',
            margin_bottom='8px',
        ),
        rx.button(
            'Details',
            size='1',
            variant='soft',
            on_click=lambda: DocSearchState.open_details(result),  # type: ignore[call-arg, arg-type]
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


def doc_render_summary_html() -> rx.Component:
    """Render markdown-formatted summary with clickable inline citations."""
    return rx.box(
        rx.html(
            DocSearchState.summary_html,
            class_name='doc-summary-html',
        ),
        rx.el.input(
            id='__cite-bridge-doc',
            type='text',
            on_change=DocSearchState.handle_citation_click,
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


def doc_summary_card() -> rx.Component:
    """AI summary card for document search."""
    return rx.cond(
        DocSearchState.show_summary,
        rx.box(
            rx.hstack(
                rx.icon('sparkles', size=16, color=style.ACCENT_COLOR),
                rx.text('AI Summary', font_weight='bold', font_size='14px'),
                rx.spacer(),
                rx.cond(
                    DocSearchState.is_summary_loading,
                    rx.spinner(size='1'),
                    rx.fragment(),
                ),
                width='100%',
                margin_bottom='8px',
            ),
            rx.cond(
                DocSearchState.is_summary_loading,
                rx.center(
                    rx.vstack(
                        rx.spinner(size='2', color=style.ACCENT_COLOR),
                        rx.text(
                            'Generating summary...',
                            color=style.SECONDARY_TEXT,
                            font_size='13px',
                        ),
                        align='center',
                        spacing='2',
                    ),
                    width='100%',
                    padding='20px',
                ),
                rx.cond(
                    DocSearchState.summary_html,
                    doc_render_summary_html(),
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


def doc_search_page() -> rx.Component:
    """Note search page component."""
    return rx.vstack(
        rx.heading('Note Search', size='8'),
        rx.hstack(
            rx.input(
                placeholder='Search notes...',
                value=DocSearchState.query,
                on_change=DocSearchState.set_query,
                on_key_down=DocSearchState.handle_key_down,
                width='100%',
                size='3',
            ),
            rx.button(
                rx.cond(
                    DocSearchState.is_loading,
                    rx.spinner(size='1'),
                    rx.icon('search', size=18),
                ),
                on_click=DocSearchState.perform_search,
                size='3',
                bg=style.ACCENT_COLOR,
                disabled=DocSearchState.is_loading,
            ),
            width='100%',
            spacing='2',
        ),
        # Strategy filter panel
        doc_filter_panel(),
        # Summary toggle
        rx.hstack(
            rx.text('AI Summary', font_size='13px', color=style.SECONDARY_TEXT),
            rx.switch(
                checked=DocSearchState.show_summary,
                on_change=DocSearchState.toggle_summary,
                size='1',
            ),
            spacing='2',
            align='center',
        ),
        # Summary card
        doc_summary_card(),
        rx.match(
            DocSearchState.mode,
            (
                'loading',
                rx.center(
                    rx.vstack(
                        rx.spinner(size='3', color=style.ACCENT_COLOR),
                        rx.heading('Searching notes...', size='5'),
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
                    rx.foreach(DocSearchState.results, doc_result_card),
                    width='100%',
                    spacing='2',
                ),
            ),
            rx.box(
                rx.text('Enter a query to start searching notes.', color=style.SECONDARY_TEXT),
                width='100%',
                padding='40px',
                bg=style.SIDEBAR_BG,
                border_radius='12px',
                border=f'1px solid {style.BORDER_COLOR}',
                text_align='center',
            ),
        ),
        doc_detail_modal(),
        spacing='6',
        width='100%',
    )
