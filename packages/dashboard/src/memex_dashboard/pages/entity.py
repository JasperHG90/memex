import reflex as rx
import networkx as nx
import time
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from .. import style
from ..api import api_client


class GraphNode(BaseModel):
    id: str
    label: str
    x: int
    y: int
    size: int
    color: str


class GraphEdge(BaseModel):
    id: str
    x1: int
    y1: int
    x2: int
    y2: int
    u: str
    v: str
    opacity: str = '0.5'
    stroke_width: str = '1'


class Mention(BaseModel):
    id: str
    unit_text: str
    doc_title: str
    date: str
    type: str


class EntityState(rx.State):
    nodes: List[GraphNode] = []
    edges: List[GraphEdge] = []
    mentions: List[Mention] = []

    selected_entity_id: str | None = None
    selected_entity_name: str = 'Entity Info'

    # Mention Detail Modal
    selected_mention: Optional[Dict[str, Any]] = None
    selected_mention_props: List[List[str]] = []
    is_mention_modal_open: bool = False

    async def open_mention(self, mention_id: str):
        try:
            from uuid import UUID

            api = api_client.api
            # We don't have a direct "get_memory_unit" in RemoteMemexAPI yet,
            # but search can be used or we can add it.
            # For now, let's assume we might need to add it to client or use a generic GET.
            unit = await api._get(f'memories/{mention_id}')
            if unit:
                doc_id = unit.get('document_id')
                doc_title = 'Unknown'
                if doc_id:
                    doc = await api.get_document(UUID(doc_id))
                    doc_title = doc.name or 'Untitled'

                props = []
                for k, v in unit.items():
                    if k not in ['text', 'embedding'] and not isinstance(v, (dict, list)):
                        props.append([str(k), str(v)])

                meta = unit.get('unit_metadata') or {}
                for k, v in meta.items():
                    props.append([f'meta.{k}', str(v)])

                self.selected_mention = {
                    **unit,
                    'doc_title': doc_title,
                    'fact_type': unit.get('fact_type', 'memory_unit'),
                }
                self.selected_mention_props = props
                self.is_mention_modal_open = True
        except Exception as e:
            print(f'Error fetching mention details: {e}')

    def close_mention_modal(self, value: bool = False):
        self.is_mention_modal_open = value

    # Controls
    limit: int = 10

    # Viewport State
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0

    # Highlight
    hovered_node_id: str = ''

    # Drag & Pan State
    drag_node_id: Optional[str] = None
    is_panning: bool = False
    drag_start_pending: bool = False  # New flag
    last_mouse_x: float = 0.0
    last_mouse_y: float = 0.0

    total_entity_count: int = 100

    def start_drag_node(self, node_id: str):
        self.drag_node_id = node_id
        self.drag_start_pending = True

    def start_pan(self):
        if not self.drag_node_id:
            self.is_panning = True
            self.drag_start_pending = True

    last_mouse_move_time: float = 0.0

    def on_mouse_move(self, x: float, y: float):
        # Throttle to ~30 FPS (33ms) to prevent server overload
        now = time.time()
        if now - self.last_mouse_move_time < 0.033:
            return
        self.last_mouse_move_time = now

        if self.drag_start_pending:
            self.last_mouse_x = x
            self.last_mouse_y = y
            self.drag_start_pending = False
            return

        prev_x = self.last_mouse_x
        prev_y = self.last_mouse_y

        self.last_mouse_x = x
        self.last_mouse_y = y

        if self.drag_node_id:
            dx = x - prev_x
            dy = y - prev_y
            move_scale = 1.0 / self.zoom

            for i, n in enumerate(self.nodes):
                if n.id == self.drag_node_id:
                    self.nodes[i].x += int(dx * move_scale)
                    self.nodes[i].y += int(dy * move_scale)
                    for j, e in enumerate(self.edges):
                        if e.u == n.id:
                            self.edges[j].x1 = self.nodes[i].x
                            self.edges[j].y1 = self.nodes[i].y
                        if e.v == n.id:
                            self.edges[j].x2 = self.nodes[i].x
                            self.edges[j].y2 = self.nodes[i].y
                    break

        elif self.is_panning:
            dx = x - prev_x
            dy = y - prev_y
            move_scale = 1.0 / self.zoom
            self.pan_x -= dx * move_scale
            self.pan_y -= dy * move_scale

    def on_mouse_up(self):
        self.drag_node_id = None
        self.is_panning = False
        self.drag_start_pending = False

    async def on_load(self):
        try:
            api = api_client.api
            counts = await api.get_stats_counts()
            self.total_entity_count = counts.entities
        except Exception as e:
            print(f'Error fetching entity count: {e}')

        await self.refresh_graph()

    def set_limit(self, value: int):
        self.limit = value

    def zoom_in(self):
        self.zoom = min(self.zoom * 1.2, 5.0)

    def zoom_out(self):
        self.zoom = max(self.zoom / 1.2, 0.5)

    def pan_left(self):
        self.pan_x -= 100 / self.zoom

    def pan_right(self):
        self.pan_x += 100 / self.zoom

    def pan_up(self):
        self.pan_y -= 100 / self.zoom

    def pan_down(self):
        self.pan_y += 100 / self.zoom

    def reset_view(self):
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

    def on_wheel(self, delta_y: float):
        if delta_y > 0:
            self.zoom = max(self.zoom / 1.1, 0.5)
        else:
            self.zoom = min(self.zoom * 1.1, 5.0)

    @rx.var
    def view_box(self) -> str:
        w = 1000 / self.zoom
        h = 1000 / self.zoom
        x = self.pan_x + (500 - w / 2)
        y = self.pan_y + (500 - h / 2)
        return f'{x} {y} {w} {h}'

    def set_hovered_node(self, node_id: str):
        self.hovered_node_id = node_id

    selected_entity_details: Dict[str, Any] = {}
    selected_entity_details_list: List[List[str]] = []
    is_modal_open: bool = False

    async def open_details(self):
        if not self.selected_entity_id:
            return

        try:
            api = api_client.api
            # get_top_entities or list_entities_ranked can be used to find details
            # Or we can add a get_entity endpoint.
            # For now, we'll use the generic GET.
            entity = await api._get(f'entities/{self.selected_entity_id}')
            if entity:
                self.selected_entity_details = entity
                self.selected_entity_details_list = [[str(k), str(v)] for k, v in entity.items()]
                self.is_modal_open = True
        except Exception as e:
            print(f'Error fetching entity details: {e}')

    def close_modal(self, value: bool = False):
        self.is_modal_open = value

    async def select_node(self, node_id: str, label: str):
        self.selected_entity_id = node_id
        self.selected_entity_name = label
        self.mentions = []

        if not node_id:
            return

        try:
            api = api_client.api
            # We need an endpoint for entity mentions.
            # server.py doesn't have it yet. I should add it or use search.
            # Let's add GET /entities/{id}/mentions to server.py
            results = await api._get(f'entities/{node_id}/mentions', params={'limit': 20})

            new_mentions = []
            for item in results:
                unit = item['unit']
                doc = item['document']

                raw_type = str(unit.get('fact_type', 'memory_unit'))
                clean_type = (
                    raw_type.split('.')[-1].lower() if '.' in raw_type else raw_type.lower()
                )
                doc_title = doc.get('name') or 'Untitled'

                created_at = unit.get('created_at')
                if isinstance(created_at, str):
                    date_str = created_at[:10]
                else:
                    date_str = 'Unknown'

                new_mentions.append(
                    Mention(
                        id=str(unit['id']),
                        unit_text=unit.get('text') or '',
                        doc_title=doc_title,
                        date=date_str,
                        type=clean_type,
                    )
                )

            self.mentions = new_mentions

        except Exception as e:
            print(f'Error fetching mentions: {e}')

    # Filters
    min_connection_strength: int = 1
    min_node_importance: int = 1
    recency_filter: str = 'all'

    # Raw Data Storage (source of truth for filtering)
    raw_entities: List[Dict[str, Any]] = []
    raw_cooccurrences: List[Dict[str, Any]] = []

    # Filter Panel State
    # Filter Panel State
    is_filter_panel_open: bool = False

    def reset_filters(self):
        self.min_connection_strength = 1
        self.min_node_importance = 1
        self.recency_filter = 'all'
        self.apply_filters()

    def toggle_filter_panel(self):
        self.is_filter_panel_open = not self.is_filter_panel_open

    def set_min_connection_strength(self, value: list[float]):
        self.min_connection_strength = int(value[0])

    def set_min_node_importance(self, value: list[float]):
        self.min_node_importance = int(value[0])

    def commit_filters(self, value: list[float]):
        self.apply_filters()

    def set_recency_filter(self, value: str):
        self.recency_filter = value
        # Recency filtering not fully implemented in backend yet
        self.apply_filters()

    def apply_filters(self):
        """Filter raw data and update graph nodes/edges."""
        # 1. Filter Entities
        filtered_entities = []
        valid_entity_ids = set()

        for e in self.raw_entities:
            # Importance Filter
            if e.get('mention_count', 0) >= self.min_node_importance:
                filtered_entities.append(e)
                valid_entity_ids.add(str(e.get('id')))

        # 2. Filter Edges
        filtered_edges = []
        for c in self.raw_cooccurrences:
            u, v = str(c['entity_id_1']), str(c['entity_id_2'])
            count = c.get('cooccurrence_count', 0)

            # Strength Filter & Node Existence Check
            if count >= self.min_connection_strength:
                if u in valid_entity_ids and v in valid_entity_ids:
                    filtered_edges.append(c)

        # 3. Rebuild Graph Layout
        self._recalculate_layout(filtered_entities, filtered_edges)

    def _recalculate_layout(
        self, entities: List[Dict[str, Any]], cooccurrences: List[Dict[str, Any]]
    ):
        try:
            # Build NetworkX Graph
            G = nx.Graph()

            for e in entities:
                G.add_node(str(e['id']), label=e['name'], count=e['mention_count'])

            for c in cooccurrences:
                u, v = str(c['entity_id_1']), str(c['entity_id_2'])
                if u in G and v in G:
                    G.add_edge(u, v, weight=c['cooccurrence_count'])

            if not G.nodes():
                self.nodes = []
                self.edges = []
                return

            # Layout
            pos = nx.spring_layout(
                G,
                seed=42,
                center=(500, 500),
                scale=450,
                k=6.0 / max(len(G.nodes()), 1) ** 0.5,
                iterations=200,
            )

            # Transform
            new_nodes = []
            new_edges = []

            for u, v, data in G.edges(data=True):
                x1, y1 = pos[u]
                x2, y2 = pos[v]
                new_edges.append(
                    GraphEdge(
                        id=f'{u}-{v}',
                        x1=int(x1),
                        y1=int(y1),
                        x2=int(x2),
                        y2=int(y2),
                        u=u,
                        v=v,
                        stroke_width=str(min(5, max(1, data.get('weight', 1) / 2))),
                    )
                )

            max_count = max([G.nodes[n]['count'] for n in G.nodes()]) if G.nodes() else 1

            for n in G.nodes():
                x, y = pos[n]
                count = G.nodes[n]['count']
                size = 3 + (count / max_count) * 12

                new_nodes.append(
                    GraphNode(
                        id=n,
                        label=G.nodes[n]['label'],
                        x=int(x),
                        y=int(y),
                        size=int(size),
                        color=style.ACCENT_COLOR,
                    )
                )

            self.nodes = new_nodes
            self.edges = new_edges

        except Exception as e:
            print(f'Error recalculating layout: {e}')
            self.nodes = []
            self.edges = []

    async def refresh_graph(self):
        try:
            api = api_client.api
            # 1. Fetch Top Entities (Ranked)
            entities = []
            async for entity in api.list_entities_ranked(limit=self.limit):
                # Convert to dict for storage
                entities.append(
                    {
                        'id': str(entity.id),
                        'name': entity.name,
                        'mention_count': entity.mention_count,
                    }
                )

            if not entities:
                self.nodes = []
                self.edges = []
                self.raw_entities = []
                self.raw_cooccurrences = []
                return

            entity_ids = [e['id'] for e in entities]

            # 2. Fetch Co-occurrences (Edges)
            cooccurrences = await api._get(
                'entities/cooccurrences', params={'ids': ','.join(entity_ids)}
            )

            # Store raw data
            self.raw_entities = entities
            self.raw_cooccurrences = cooccurrences

            # Apply filters (will handle layout)
            self.apply_filters()

        except Exception as e:
            print(f'Error generating graph: {e}')
            self.nodes = []
            self.edges = []


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


def render_kv_row(k: str, v: str) -> rx.Component:
    return rx.table.row(
        rx.table.cell(
            rx.text(k, font_weight='bold', color='gray', font_size='10px'), padding_y='1'
        ),
        rx.table.cell(rx.text(v, font_size='10px', color='white'), padding_y='1'),
        border_bottom=f'1px solid {style.BORDER_COLOR}',
    )


def controls() -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(f'Limit: {EntityState.limit}', size='1', color='gray'),
            rx.slider(
                min=5,
                max=EntityState.total_entity_count,
                default_value=[10],
                on_value_commit=lambda v: EntityState.set_limit(v[0]),  # type: ignore
                width='150px',
            ),
            spacing='1',
        ),
        rx.divider(orientation='vertical', height='20px'),
        rx.hstack(
            rx.icon_button(
                rx.icon('zoom-in', size=16), on_click=EntityState.zoom_in, variant='ghost', size='1'
            ),
            rx.icon_button(
                rx.icon('zoom-out', size=16),
                on_click=EntityState.zoom_out,
                variant='ghost',
                size='1',
            ),
            rx.icon_button(
                rx.icon('maximize', size=16),
                on_click=EntityState.reset_view,
                variant='ghost',
                size='1',
            ),
            spacing='1',
        ),
        rx.divider(orientation='vertical', height='20px'),
        rx.hstack(
            rx.icon_button(
                rx.icon('arrow-left', size=16),
                on_click=EntityState.pan_left,
                variant='ghost',
                size='1',
            ),
            rx.icon_button(
                rx.icon('arrow-right', size=16),
                on_click=EntityState.pan_right,
                variant='ghost',
                size='1',
            ),
            rx.icon_button(
                rx.icon('arrow-up', size=16), on_click=EntityState.pan_up, variant='ghost', size='1'
            ),
            rx.icon_button(
                rx.icon('arrow-down', size=16),
                on_click=EntityState.pan_down,
                variant='ghost',
                size='1',
            ),
            spacing='1',
        ),
        rx.spacer(),
        rx.button(
            rx.icon('refresh-cw', size=16),
            'Regenerate',
            on_click=EntityState.refresh_graph,
            variant='outline',
            size='2',
        ),
        width='100%',
        padding='16px',
        border_bottom=f'1px solid {style.BORDER_COLOR}',
        bg=style.BG_COLOR,
        align='center',
    )


def mention_card(m: Mention) -> rx.Component:
    return rx.box(
        rx.hstack(
            type_badge('memory_unit'),
            rx.badge(
                m.type.replace('_', ' '),
                variant='soft',
                color_scheme='gray',
                text_transform='capitalize',
            ),
            rx.spacer(),
            rx.text(m.date, font_size='10px', color='gray'),
            width='100%',
            margin_bottom='4px',
        ),
        rx.text(m.unit_text, font_weight='medium', font_size='12px', color='white', no_of_lines=2),
        rx.text(
            f'Source: {m.doc_title}',
            font_size='10px',
            color=style.SECONDARY_TEXT,
            margin_top='2px',
            font_style='italic',
        ),
        padding='12px',
        border_bottom=f'1px solid {style.BORDER_COLOR}',
        width='100%',
        cursor='pointer',
        on_click=lambda: EntityState.open_mention(m.id),  # type: ignore
        _hover={'bg': style.HOVER_COLOR},
    )


def mention_details_modal() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title('Mention Details'),
            rx.cond(
                EntityState.selected_mention,
                rx.vstack(
                    rx.hstack(
                        type_badge('memory_unit'),
                        rx.badge(
                            EntityState.selected_mention['fact_type'].to(str).replace('_', ' '),  # type: ignore
                            variant='soft',
                            color_scheme='gray',
                            text_transform='capitalize',
                        ),
                        rx.spacer(),
                        rx.text(
                            'Source: ',
                            EntityState.selected_mention['doc_title'],  # type: ignore
                            size='1',
                            color='gray',
                        ),
                        width='100%',
                        align='center',
                    ),
                    rx.divider(),
                    rx.text.strong('Content:'),
                    rx.scroll_area(
                        rx.text(EntityState.selected_mention['text'].to(str), size='2'),  # type: ignore
                        height='150px',
                    ),
                    rx.divider(),
                    rx.text.strong('Metadata:'),
                    rx.scroll_area(
                        rx.table.root(
                            rx.table.body(
                                rx.foreach(
                                    EntityState.selected_mention_props,
                                    lambda item: render_kv_row(item[0], item[1]),
                                )
                            ),
                            width='100%',
                        ),
                        height='200px',
                    ),
                    spacing='3',
                    align='stretch',
                ),
                rx.spinner(),
            ),
            rx.dialog.close(
                rx.button('Close', variant='soft', color_scheme='gray', margin_top='16px'),
            ),
        ),
        open=EntityState.is_mention_modal_open,
        on_open_change=EntityState.close_mention_modal,
    )


def details_modal() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title('Entity Details'),
            rx.cond(
                EntityState.selected_entity_details,
                rx.vstack(
                    rx.badge(EntityState.selected_entity_name, variant='soft', color_scheme='blue'),
                    rx.text.strong('Properties:'),
                    rx.scroll_area(
                        rx.table.root(
                            rx.table.body(
                                rx.foreach(
                                    EntityState.selected_entity_details_list,
                                    lambda item: render_kv_row(item[0], item[1]),
                                )
                            ),
                            width='100%',
                        ),
                        height='400px',
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
        open=EntityState.is_modal_open,
        on_open_change=EntityState.close_modal,
    )


def filter_panel() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.text('Graph Filters', font_weight='bold', size='2', color='white'),
                rx.spacer(),
                rx.icon(
                    rx.cond(EntityState.is_filter_panel_open, 'chevron-up', 'chevron-down'),
                    size=16,
                    cursor='pointer',
                ),
                width='100%',
                align='center',
                on_click=EntityState.toggle_filter_panel,
                cursor='pointer',
            ),
            rx.cond(
                EntityState.is_filter_panel_open,
                rx.vstack(
                    rx.divider(margin_y='2'),
                    rx.text(
                        f'Min Connection Strength: {EntityState.min_connection_strength}',
                        size='1',
                        color='gray',
                    ),
                    rx.slider(
                        min=1,
                        max=10,
                        value=[EntityState.min_connection_strength],
                        on_change=EntityState.set_min_connection_strength,
                        on_value_commit=EntityState.commit_filters,
                        width='100%',
                    ),
                    rx.text(
                        f'Min Node Importance: {EntityState.min_node_importance}',
                        size='1',
                        color='gray',
                    ),
                    rx.slider(
                        min=1,
                        max=20,
                        value=[EntityState.min_node_importance],
                        on_change=EntityState.set_min_node_importance,
                        on_value_commit=EntityState.commit_filters,
                        width='100%',
                    ),
                    rx.text('Recency', size='1', color='gray'),
                    rx.select(
                        ['all', 'last_week', 'last_month', 'last_year'],
                        value=EntityState.recency_filter,
                        on_change=EntityState.set_recency_filter,
                        width='100%',
                        size='1',
                    ),
                    rx.button(
                        'Reset Filters',
                        size='1',
                        variant='soft',
                        color_scheme='gray',
                        width='100%',
                        on_click=EntityState.reset_filters,
                    ),
                    spacing='3',
                    width='100%',
                    padding_top='2',
                ),
            ),
            bg=style.SIDEBAR_BG,
            border=f'1px solid {style.BORDER_COLOR}',
            border_radius='8px',
            padding='12px',
            width='250px',
            shadow='lg',
        ),
        position='absolute',
        top='16px',
        right='16px',
        z_index='10',
    )


def side_panel() -> rx.Component:
    return rx.box(
        rx.hstack(
            rx.heading(EntityState.selected_entity_name, size='4', color='white'),
            rx.spacer(),
            rx.icon(
                'x',
                size=16,
                cursor='pointer',
                on_click=lambda: EntityState.select_node('', 'Entity Info'),  # type: ignore
            ),
            width='100%',
            align='center',
            margin_bottom='16px',
        ),
        rx.cond(
            EntityState.selected_entity_id,
            rx.vstack(
                rx.hstack(
                    rx.button(
                        'Details',
                        width='50%',
                        variant='soft',
                        on_click=EntityState.open_details,
                    ),
                    rx.button(
                        'View Lineage',
                        width='50%',
                        variant='outline',
                        on_click=rx.redirect(
                            f'/lineage?id={EntityState.selected_entity_id}&type=mental_model'
                        ),
                    ),
                    width='100%',
                    spacing='2',
                    margin_bottom='16px',
                ),
                rx.text('Mentions', font_weight='bold', size='2', color='gray'),
                rx.scroll_area(
                    rx.vstack(
                        rx.foreach(EntityState.mentions, mention_card), spacing='0', width='100%'
                    ),
                    height='500px',
                ),
            ),
            rx.box(
                rx.text('Select a node to view details.', color='gray', font_size='12px'),
                padding_y='20px',
            ),
        ),
        width='300px',
        height='100%',
        bg=style.SIDEBAR_BG,
        border_left=f'1px solid {style.BORDER_COLOR}',
        padding='24px',
    )


class GraphContainer(rx.el.Div):
    def get_event_triggers(self):
        return {
            'on_mouse_move': lambda e0: [e0.clientX, e0.clientY],
            'on_wheel': lambda e0: [e0.deltaY],
            'on_mouse_down': lambda e0: [],
            'on_mouse_up': lambda e0: [],
            'on_mouse_leave': lambda e0: [],
        }

    @classmethod
    def create(cls, *children, **props):
        # Ensure styles are passed correctly if not handled automatically
        return super().create(*children, **props)


def graph_view_container() -> rx.Component:
    """Wrapper to handle mouse events using GraphContainer which supports event args."""
    return GraphContainer.create(
        filter_panel(),
        rx.el.svg(
            # Edges
            rx.foreach(
                EntityState.edges,
                lambda e: rx.el.line(
                    x1=e.x1,
                    y1=e.y1,
                    x2=e.x2,
                    y2=e.y2,
                    stroke=rx.cond(
                        (EntityState.hovered_node_id == e.u) | (EntityState.hovered_node_id == e.v),
                        'white',  # Highlighted
                        'rgba(255, 255, 255, 0.2)',  # Off-white/Dim default
                    ),
                    stroke_width=e.stroke_width,
                    opacity=rx.cond(
                        (EntityState.hovered_node_id == e.u) | (EntityState.hovered_node_id == e.v),
                        '1.0',
                        e.opacity,
                    ),
                    transition='all 0.2s ease',
                ),
            ),
            # Nodes
            rx.foreach(
                EntityState.nodes,
                lambda n: rx.el.circle(
                    cx=n.x,
                    cy=n.y,
                    r=n.size,
                    fill=rx.cond(
                        EntityState.selected_entity_id == n.id,
                        'white',  # Selected
                        n.color,  # Default
                    ),
                    stroke=style.BG_COLOR,
                    stroke_width='2',
                    cursor='grab',
                    on_click=EntityState.select_node(n.id, n.label),  # type: ignore
                    on_mouse_enter=EntityState.set_hovered_node(n.id),  # type: ignore
                    on_mouse_leave=EntityState.set_hovered_node(''),  # type: ignore
                    on_mouse_down=lambda: EntityState.start_drag_node(n.id),  # type: ignore
                    transition='all 0.1s ease',  # Faster transition for dragging
                    opacity='1.0',
                ),
            ),
            # Labels (only visible on hover or if large enough)
            rx.foreach(
                EntityState.nodes,
                lambda n: rx.el.text(
                    n.label,
                    x=n.x,
                    y=n.y + 15,  # Simplified offset to avoid complex var math
                    text_anchor='middle',
                    fill='white',
                    font_size='12px',
                    font_family='Inter, sans-serif',
                    opacity='0.8',  # Always visible but slightly dim
                    pointer_events='none',
                ),
            ),
            width='100%',
            height='100%',
            view_box=EntityState.view_box,
            preserve_aspect_ratio='xMidYMid meet',
        ),
        style={
            'width': '100%',
            'height': '100%',
            'background-color': style.SIDEBAR_BG,
            'border-radius': '12px',
            'border': f'1px solid {style.BORDER_COLOR}',
            'position': 'relative',
            'overflow': 'hidden',
        },
        on_mouse_move=EntityState.on_mouse_move,
        on_mouse_down=EntityState.start_pan,
        on_mouse_up=EntityState.on_mouse_up,
        on_mouse_leave=EntityState.on_mouse_up,
        on_wheel=EntityState.on_wheel,
    )


def graph_view() -> rx.Component:
    return graph_view_container()


def entity_page() -> rx.Component:
    return rx.vstack(
        controls(),
        rx.hstack(
            graph_view(),
            side_panel(),
            width='100%',
            height='650px',
            spacing='0',
        ),
        details_modal(),
        mention_details_modal(),
        width='100%',
        height='100%',
        spacing='0',
        on_mount=EntityState.on_load,
    )
