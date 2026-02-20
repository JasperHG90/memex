import reflex as rx
import plotly.express as px
import plotly.graph_objects as go
import httpx
from .. import style
from ..api import api_client


client = httpx.AsyncClient(timeout=1.0)


class OverviewState(rx.State):
    metrics: dict = {
        'total_memories': 0,
        'total_entities': 0,
        'pending_reflections': 0,
    }
    recent_memories: list[dict] = []
    token_usage_graph: go.Figure = go.Figure()

    # Server Stats
    server_stats: dict = {
        'cpu_usage': '0%',
        'memory_usage': '0 MB',
        'requests': '0',
        'status': 'Unknown',
    }

    # Detail Modal State
    selected_memory_title: str = ''
    selected_memory_content: str = ''
    selected_memory_metadata: list[list[str]] = []
    is_modal_open: bool = False

    async def on_load(self):
        """Initial load - non-blocking."""
        return [OverviewState.fetch_db_stats, OverviewState.fetch_server_stats]

    async def fetch_db_stats(self):
        """Fetch statistics via API in background."""
        try:
            api = api_client.api

            # 1. Counts
            counts = await api.get_stats_counts()
            self.metrics = {
                'total_memories': counts.memories,
                'total_entities': counts.entities,
                'pending_reflections': counts.reflection_queue,
            }

            # 2. Recent Documents
            recent_docs = await api.get_recent_documents(limit=5)

            parsed_memories = []
            for d in recent_docs:
                meta = d.doc_metadata or {}
                title = meta.get('title') or meta.get('name') or meta.get('source')
                if not title:
                    title = f'Document {str(d.id)[:8]}'

                preview_text = meta.get('description') or meta.get('summary')
                if not preview_text:
                    preview_text = 'Click to view details'

                created = d.created_at.strftime('%Y-%m-%d')
                parsed_memories.append(
                    {
                        'id': str(d.id),
                        'title': str(title),
                        'preview': str(preview_text),
                        'date': str(created),
                    }
                )
            self.recent_memories = parsed_memories

            # 3. Token Usage
            usage_resp = await api.get_token_usage()
            from datetime import datetime, timedelta, timezone

            now_utc = datetime.now(timezone.utc)
            daily_usage = {}
            for i in range(7):
                d_obj = now_utc - timedelta(days=i)
                d_str = d_obj.strftime('%Y-%m-%d')
                daily_usage[d_str] = 0

            for entry in usage_resp.usage:
                d_str = entry.date.strftime('%Y-%m-%d')
                if d_str in daily_usage:
                    daily_usage[d_str] = entry.total_tokens

            sorted_date_keys = sorted(daily_usage.keys())
            dates = [d[5:] for d in sorted_date_keys]
            tokens = [daily_usage[d] for d in sorted_date_keys]

            fig = px.bar(
                x=dates,
                y=tokens,
                labels={'x': 'Date', 'y': 'Tokens'},
                template='plotly_dark',
            )
            fig.update_layout(
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                margin=dict(t=20, l=20, r=20, b=20),
                font=dict(color=style.SECONDARY_TEXT),
                autosize=True,
                xaxis=dict(type='category'),
            )
            fig.update_traces(marker_color=style.ACCENT_COLOR)
            self.token_usage_graph = fig

        except Exception as e:
            print(f'Error fetching API metrics: {e}')
            pass

    async def open_details(self, memory_id: str):
        try:
            from uuid import UUID

            api = api_client.api
            doc = await api.get_document(UUID(memory_id))
            if doc:
                meta = doc.doc_metadata
                self.selected_memory_title = doc.name or 'Untitled'
                self.selected_memory_content = doc.original_text or ''
                self.selected_memory_metadata = [[str(k), str(v)] for k, v in meta.items()]
                self.is_modal_open = True
        except Exception as e:
            print(f'Error fetching details: {e}')

    def close_details(self, value: bool = False):
        self.is_modal_open = value

    async def tick(self):
        """No-op for legacy events."""
        pass

    async def fetch_server_stats(self):
        """Fetch server metrics from the /metrics endpoint."""
        try:
            base_url = 'http://localhost:8000'
            # Try to use api client base url if set
            if api_client.api.client.base_url:
                base_url = str(api_client.api.client.base_url).rstrip('/')

            candidate_urls = [
                f'{base_url}/api/v1/metrics',
                'http://localhost:8000/api/v1/metrics',
                'http://127.0.0.1:8000/api/v1/metrics',
            ]

            metrics_text = ''
            async with httpx.AsyncClient(timeout=1.0) as client:
                for url in candidate_urls:
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            metrics_text = resp.text
                            break
                    except Exception:
                        continue

            if metrics_text:
                lines = metrics_text.split('\n')
                req_total = 0.0
                mem_bytes = 0.0
                cpu_seconds = 0.0

                for line in lines:
                    if line.startswith('#'):
                        continue
                    if 'http_requests_total' in line:
                        parts = line.split(' ')
                        if len(parts) >= 2:
                            req_total += float(parts[-1])
                    if 'process_resident_memory_bytes' in line:
                        parts = line.split(' ')
                        if len(parts) >= 2:
                            mem_bytes = float(parts[-1])
                    if 'process_cpu_seconds_total' in line:
                        parts = line.split(' ')
                        if len(parts) >= 2:
                            cpu_seconds = float(parts[-1])

                self.server_stats = {
                    'cpu_usage': f'{cpu_seconds:.2f}s',
                    'memory_usage': f'{mem_bytes / 1024 / 1024:.0f} MB',
                    'requests': str(int(req_total)),
                    'status': 'Healthy',
                }
            else:
                self.server_stats['status'] = 'Unreachable'
        except Exception as ex:
            self.server_stats['status'] = f'Error: {ex}'


def metric_card(title: str, value: str, icon: str, trend: str = '') -> rx.Component:
    return rx.box(
        rx.hstack(
            rx.vstack(
                rx.text(title, size='2', color=style.SECONDARY_TEXT),
                rx.heading(value, size='6'),
                spacing='1',
            ),
            rx.spacer(),
            rx.center(
                rx.icon(icon, size=24, color=style.ACCENT_COLOR),
                width='48px',
                height='48px',
                bg='rgba(59, 130, 246, 0.1)',
                border_radius='12px',
            ),
            width='100%',
            align='center',
        ),
        padding='24px',
        bg=style.SIDEBAR_BG,
        border_radius='12px',
        border=f'1px solid {style.BORDER_COLOR}',
        width='100%',
    )


def token_usage_chart() -> rx.Component:
    return rx.box(
        rx.heading('Token Usage', size='4', margin_bottom='4'),
        rx.box(
            rx.plotly(
                data=OverviewState.token_usage_graph,
                use_resize_handler=True,
                style={'width': '100%', 'height': '300px'},
            ),
            width='100%',
        ),
        padding='24px',
        bg=style.SIDEBAR_BG,
        border_radius='12px',
        border=f'1px solid {style.BORDER_COLOR}',
        width='100%',
    )


def server_stats_panel() -> rx.Component:
    return rx.box(
        rx.hstack(
            rx.heading('Server Health', size='4'),
            rx.spacer(),
            rx.button(
                rx.icon('refresh-cw', size=16),
                variant='ghost',
                size='2',
                on_click=OverviewState.fetch_server_stats,
            ),
            width='100%',
            align='center',
            margin_bottom='4',
        ),
        rx.grid(
            rx.vstack(
                rx.text('Status', color='gray', size='2'),
                rx.badge(OverviewState.server_stats['status'], color_scheme='green'),
            ),
            rx.vstack(
                rx.text('Memory', color='gray', size='2'),
                rx.text(OverviewState.server_stats['memory_usage'], weight='bold'),
            ),
            rx.vstack(
                rx.text('CPU Time', color='gray', size='2'),
                rx.text(OverviewState.server_stats['cpu_usage'], weight='bold'),
            ),
            rx.vstack(
                rx.text('Requests', color='gray', size='2'),
                rx.text(OverviewState.server_stats['requests'], weight='bold'),
            ),
            columns='2',
            spacing='4',
        ),
        padding='24px',
        bg=style.SIDEBAR_BG,
        border_radius='12px',
        border=f'1px solid {style.BORDER_COLOR}',
        width='100%',
    )


def memory_item(item: dict) -> rx.Component:
    return rx.hstack(
        rx.icon('file-text', size=18, color=style.SECONDARY_TEXT),
        rx.vstack(
            rx.text(item['title'], size='2', weight='medium'),
            rx.text(item['preview'], size='1', color=style.SECONDARY_TEXT),
            spacing='1',
        ),
        rx.spacer(),
        rx.text(item['date'], size='1', color=style.SECONDARY_TEXT),
        # Make clickable - Open Modal
        cursor='pointer',
        _hover={'bg': 'rgba(255,255,255,0.05)'},
        on_click=lambda: OverviewState.open_details(item['id']),  # type: ignore
        padding='12px',
        border_bottom=f'1px solid {style.BORDER_COLOR}',
        width='100%',
        align='center',
    )


def render_kv_row(k: str, v: str) -> rx.Component:
    return rx.table.row(
        rx.table.cell(
            rx.text(k, font_weight='bold', color='gray', font_size='10px'), padding_y='1'
        ),
        rx.table.cell(rx.text(v, font_size='10px', color='white'), padding_y='1'),
        border_bottom=f'1px solid {style.BORDER_COLOR}',
    )


def memory_detail_modal() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title('Document Details'),
            rx.cond(
                OverviewState.selected_memory_title != '',
                rx.vstack(
                    rx.badge('Document', variant='soft', color_scheme='blue'),
                    rx.text.strong(OverviewState.selected_memory_title),
                    rx.divider(),
                    rx.text.strong('Content:'),
                    rx.scroll_area(
                        rx.markdown(OverviewState.selected_memory_content),
                        height='300px',
                    ),
                    rx.divider(),
                    rx.text.strong('Metadata:'),
                    rx.scroll_area(
                        rx.table.root(
                            rx.table.body(
                                rx.foreach(
                                    OverviewState.selected_memory_metadata,
                                    lambda item: render_kv_row(item[0], item[1]),
                                )
                            ),
                            width='100%',
                        ),
                        height='150px',
                    ),
                    spacing='4',
                    align='stretch',
                ),
                rx.spinner(),
            ),
            rx.dialog.close(
                rx.button('Close', variant='soft', color_scheme='gray', margin_top='16px')
            ),
        ),
        open=OverviewState.is_modal_open,
        on_open_change=OverviewState.close_details,
    )


def recent_memories_feed() -> rx.Component:
    return rx.box(
        rx.heading('Recent Memories', size='4', margin_bottom='4'),
        rx.vstack(
            rx.foreach(OverviewState.recent_memories, memory_item),
            width='100%',
            spacing='0',
        ),
        padding='24px',
        bg=style.SIDEBAR_BG,
        border_radius='12px',
        border=f'1px solid {style.BORDER_COLOR}',
        width='100%',
    )


def overview_page() -> rx.Component:
    return rx.vstack(
        rx.heading('Overview', size='8'),
        rx.grid(
            metric_card(
                'Ingested Documents',
                OverviewState.metrics['total_memories'].to_string(),
                'file-text',
            ),
            metric_card('Entities', OverviewState.metrics['total_entities'].to_string(), 'users'),
            metric_card(
                'Pending Reflections',
                OverviewState.metrics['pending_reflections'].to_string(),
                'loader',
            ),
            columns=rx.breakpoints(initial='1', sm='2', md='3'),
            spacing='4',
            width='100%',
        ),
        rx.flex(
            rx.vstack(
                token_usage_chart(),
                server_stats_panel(),
                spacing='4',
                flex='1 1 500px',  # Grow, Shrink, Basis
                width='100%',
            ),
            rx.box(
                recent_memories_feed(),
                flex='1 1 350px',
                width='100%',
            ),
            spacing='4',
            width='100%',
            flex_wrap='wrap',
            align_items='start',
        ),
        memory_detail_modal(),
        spacing='6',
        width='100%',
        on_mount=OverviewState.on_load,
    )
