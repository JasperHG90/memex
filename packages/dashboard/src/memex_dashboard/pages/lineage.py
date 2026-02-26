import json
import asyncio
import reflex as rx
import networkx as nx
import urllib.parse
from typing import List, Dict, Any
from pydantic import BaseModel
from .. import style
from ..api import api_client


def flatten_dict(d: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, str]:
    items: List[Any] = []
    for k, v in d.items():
        new_key = f'{parent_key}{sep}{k}' if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        elif isinstance(v, list):
            # For lists, we just jsonify them to keep it somewhat compact but readable
            items.append((new_key, json.dumps(v)))
        else:
            items.append((new_key, str(v)))
    return dict(items)


class LineageNode(BaseModel):
    id: str
    label: str
    full_label: str = ''  # Store untruncated label for details
    type: str
    x: str  # Percent string
    y: str
    raw: Dict[str, Any]


class LineageEdge(BaseModel):
    id: str  # distinct edge id
    u: str
    v: str
    path: str  # SVG Path d string


class NodeInfo(BaseModel):
    type: str = ''
    id: str = ''
    label: str = ''
    raw: List[Dict[str, str]] = []


class LineageState(rx.State):
    nodes: List[LineageNode] = []
    edges: List[LineageEdge] = []
    target_id: str = ''
    target_type: str = 'mental_model'
    search_query: str = ''
    selected_node_info: NodeInfo = NodeInfo()

    # Highlighting
    hovered_node_id: str = ''
    highlighted_node_ids: List[str] = []
    highlighted_edge_ids: List[str] = []

    # Selection options: List of (id, label) pairs
    available_models: List[List[str]] = []
    filtered_models: List[List[str]] = []
    show_suggestions: bool = False

    # Legend data
    legend_items: List[Dict[str, str]] = [
        {'label': 'Asset', 'color': '#8b5cf6'},
        {'label': 'Note', 'color': '#10b981'},
        {'label': 'Memory Unit', 'color': '#3b82f6'},
        {'label': 'Observation', 'color': '#f59e0b'},
        {'label': 'Mental Model', 'color': '#ef4444'},
    ]

    # Graph cache for pathfinding
    _graph_cache: Any = None

    # Modal State
    is_modal_open: bool = False

    def close_modal(self, value: bool = False):
        self.is_modal_open = value

    async def on_load(self):
        # check query params
        query_str = self.router.url.query
        parsed_params = urllib.parse.parse_qs(query_str)
        qp = {k: v[0] for k, v in parsed_params.items()}

        if 'id' in qp and qp['id']:
            self.target_id = qp['id']
            # We need to fetch the name for the input box if possible
            # But we can defer that or just show the ID for now if we don't have a lookup
            # Ideally we'd fetch the entity details here to populate search_query with the name.
            pass

        if 'type' in qp and qp['type']:
            self.target_type = qp['type']

        # Initial suggestions (pre-load but don't show)
        await self.filter_models('')
        self.show_suggestions = False

        if self.target_id:
            await self.fetch_lineage()

    async def filter_models(self, query: str):
        self.search_query = query
        self.show_suggestions = True
        limit = 10

        try:
            if not query or len(query) < 2:
                # Default to top entities using the standard API wrapper (usually stable)
                models = await api_client.api.get_top_entities(limit=limit)
                self.filtered_models = [
                    [str(getattr(m, 'id', '')), getattr(m, 'name', 'Unknown')] for m in models
                ]
            else:
                # ROBUST SEARCH STRATEGY
                # Directly use the underlying httpx client to handle both JSON and NDJSON responses,
                # and apply client-side filtering in case the server is running an older version.
                response = await api_client.api.client.get(
                    'entities', params={'q': query, 'limit': 100}
                )

                raw_items = []
                try:
                    # Case A: Server returns JSON list (New Version)
                    json_data = response.json()
                    if isinstance(json_data, list):
                        raw_items = json_data
                except Exception:
                    # Case B: Server returns NDJSON stream (Old Version)
                    for line in response.text.splitlines():
                        if line.strip():
                            try:
                                raw_items.append(json.loads(line))
                            except Exception:
                                pass

                # Client-side filtering & Mapping
                filtered = []
                q_lower = query.lower()

                for item in raw_items:
                    # Handle both dict (raw) and object (if utilizing other paths)
                    if isinstance(item, dict):
                        name = item.get('name') or item.get('canonical_name') or 'Unknown'
                        eid = item.get('id')
                    else:
                        name = getattr(item, 'name', getattr(item, 'canonical_name', 'Unknown'))
                        eid = getattr(item, 'id', None)

                    if q_lower in name.lower():
                        filtered.append([str(eid), name])

                self.filtered_models = filtered[:limit]

        except Exception as e:
            print(f'Error searching models: {e}')
            self.filtered_models = []

    def select_suggestion(self, model: List[str]):
        self.target_id = model[0]
        self.search_query = model[1]
        self.target_type = 'mental_model'  # Ensure we look for the mental model of this entity
        self.show_suggestions = False
        return LineageState.fetch_lineage

    def on_search_key_down(self, key: str):
        if key == 'Enter':
            if self.show_suggestions and self.filtered_models:
                # Select first suggestion
                return self.select_suggestion(self.filtered_models[0])
            elif self.target_id:
                # If we have a target ID already (e.g. pasted), fetch
                self.show_suggestions = False
                return LineageState.fetch_lineage

    async def hide_suggestions(self):
        # Allow time for click/mouse_down to register
        await asyncio.sleep(0.2)
        self.show_suggestions = False

    async def fetch_lineage(self):
        print(f'Fetching lineage for {self.target_type}:{self.target_id}')
        self.nodes = []
        self.edges = []
        self.highlighted_node_ids = []
        self.highlighted_edge_ids = []
        self.hovered_node_id = ''
        self._graph_cache = None

        try:
            if self.target_type == 'note':
                lineage = await api_client.api.get_note_lineage(note_id=self.target_id, depth=4)
            else:
                lineage = await api_client.api.get_entity_lineage(entity_id=self.target_id, depth=4)
            print(
                f'Lineage fetched. Nodes: {len(lineage.derived_from) if lineage.derived_from else 0}'
            )
            self.generate_layout(lineage)
        except Exception as e:
            print(f'Error fetching lineage: {e}')

    def on_target_change(self, value: str):
        self.target_id = value
        return LineageState.fetch_lineage

    def select_node(self, node_id: str):
        # Find node in list
        node = next((n for n in self.nodes if n.id == node_id), None)
        if not node:
            return

        # 1. Update Details Panel
        raw = node.raw.copy()
        if 'embedding' in raw:
            del raw['embedding']

        # Flatten nested structures for table view
        flat_raw = flatten_dict(raw)

        # IMPORTANT: Convert raw dict to List[Dict[str, str]] primitives for Reflex
        raw_list = []
        for k, v in flat_raw.items():
            # Truncate very long values
            if len(v) > 500:
                v = v[:500] + '...'
            raw_list.append({'key': str(k), 'value': v})

        # Sort by key
        raw_list.sort(key=lambda x: x['key'])

        self.selected_node_info = NodeInfo(
            type=node.type,
            id=node.id,
            label=node.full_label if node.full_label else node.label,
            raw=raw_list,
        )
        self.is_modal_open = True

    def set_hovered_node(self, node_id: str):
        self.hovered_node_id = node_id
        if not node_id:
            self.highlighted_node_ids = []
            self.highlighted_edge_ids = []
            return

        # Path Highlighting Logic
        if self._graph_cache:
            G = self._graph_cache
            if not G.has_node(node_id):
                return

            connected_nodes = {node_id}

            # Find Ancestors (Upstream)
            ancestors = nx.ancestors(G, node_id)
            connected_nodes.update(ancestors)

            # Find Descendants (Downstream)
            descendants = nx.descendants(G, node_id)
            connected_nodes.update(descendants)

            # Update State
            self.highlighted_node_ids = list(connected_nodes)

            # Filter edges that connect two highlighted nodes
            hl_edges = []
            for e in self.edges:
                if e.u in connected_nodes and e.v in connected_nodes:
                    hl_edges.append(e.id)
            self.highlighted_edge_ids = hl_edges

    def generate_layout(self, lineage: Any):
        G = nx.DiGraph()

        layer_map = {
            'asset': 0,
            'note': 1,
            'memory_unit': 2,
            'observation': 3,
            'mental_model': 4,
            'unknown': 5,
        }

        # Track nodes per layer for strict positioning
        layers: Dict[int, List[str]] = {0: [], 1: [], 2: [], 3: [], 4: [], 5: []}

        def process_node(node: Any) -> str:
            entity_type = getattr(node, 'entity_type', 'unknown')
            entity = getattr(node, 'entity', {})
            eid = str(entity.get('id') or entity.get('uuid') or 'unknown')

            # Helper to get value from dict or object
            def get_val(obj, key):
                if isinstance(obj, dict):
                    return obj.get(key)
                return getattr(obj, key, None)

            # Intelligent Labeling Priority
            # 1. Top-level title/name
            label = get_val(entity, 'title') or get_val(entity, 'name')

            # 2. Document Metadata
            if not label:
                meta = get_val(entity, 'doc_metadata')
                if meta:
                    label = (
                        get_val(meta, 'title') or get_val(meta, 'name') or get_val(meta, 'source')
                    )

            # 3. Content snippet (if text/statement exists)
            if not label:
                text = get_val(entity, 'statement') or get_val(entity, 'text')
                if text:
                    label = text[:20] + '...' if len(text) > 20 else text

            # 4. Fallback to ID
            if not label:
                label = eid[:8]

            # Ensure layer is calculated
            layer_idx = layer_map.get(entity_type, 5)

            # Ensure Node Exists with Attributes
            if not G.has_node(eid):
                G.add_node(eid, layer=layer_idx, label=label, type=entity_type, raw=entity)
                layers[layer_idx].append(eid)
            else:
                # Update if incomplete (safeguard for implicit adds)
                if 'label' not in G.nodes[eid]:
                    G.nodes[eid].update(
                        {'layer': layer_idx, 'label': label, 'type': entity_type, 'raw': entity}
                    )
                # Ensure in layers index
                if eid not in layers[layer_idx]:
                    layers[layer_idx].append(eid)

            # If Note, check for Assets
            if entity_type == 'note':
                assets = entity.get('assets', [])
                for asset_path in assets:
                    # Create Asset Node
                    asset_id = f'asset:{asset_path}'
                    asset_name = asset_path.split('/')[-1]
                    if not G.has_node(asset_id):
                        G.add_node(
                            asset_id,
                            layer=0,
                            label=asset_name,
                            type='asset',
                            raw={'path': asset_path},
                        )
                        layers[0].append(asset_id)
                    # Link Asset -> Document
                    G.add_edge(asset_id, eid)

            return eid

        def traverse(node: Any):
            source_id = process_node(node)
            for child in getattr(node, 'derived_from', []):
                target_id = process_node(child)

                # Safeguard access
                s_node = G.nodes.get(source_id)
                t_node = G.nodes.get(target_id)

                if s_node and t_node and 'layer' in s_node and 'layer' in t_node:
                    s_layer = s_node['layer']
                    t_layer = t_node['layer']

                    if t_layer < s_layer:
                        G.add_edge(target_id, source_id)
                    else:
                        G.add_edge(source_id, target_id)

                traverse(child)

        traverse(lineage)
        self._graph_cache = G

        if len(G.nodes) == 0:
            return

        # STRICT COLUMNAR LAYOUT
        # X positions: Fixed percentages
        x_positions = {
            0: 5,  # Asset
            1: 20,  # Note
            2: 45,  # Memory
            3: 70,  # Observation
            4: 85,  # Model
            5: 98,
        }

        node_coords = {}  # id -> (x, y)

        new_nodes = []

        # Assign coordinates
        for layer_idx, nodes_in_layer in layers.items():
            if not nodes_in_layer:
                continue

            count = len(nodes_in_layer)
            # Sort by label for stability
            nodes_in_layer.sort(key=lambda n: str(G.nodes[n]['label']))

            for i, nid in enumerate(nodes_in_layer):
                # Distribute vertically evenly
                y_percent = 10 + (i / max(1, count - 1)) * 80 if count > 1 else 50
                x_percent = x_positions.get(layer_idx, 95)

                node_coords[nid] = (x_percent, y_percent)

                # Get the label from the graph node
                short_label = str(G.nodes[nid]['label'])
                # Retrieve full text if available in raw data, otherwise use label
                raw_data = G.nodes[nid]['raw']
                full_text = short_label
                if raw_data:
                    # Helper to get value from dict or object
                    def get_v(obj, key):
                        if isinstance(obj, dict):
                            return obj.get(key)
                        return getattr(obj, key, None)

                    candidate = get_v(raw_data, 'title') or get_v(raw_data, 'name')
                    if not candidate:
                        meta = get_v(raw_data, 'doc_metadata')
                        if meta:
                            candidate = (
                                get_v(meta, 'title') or get_v(meta, 'name') or get_v(meta, 'source')
                            )
                    if not candidate:
                        candidate = get_v(raw_data, 'statement') or get_v(raw_data, 'text')

                    if candidate:
                        full_text = str(candidate)

                new_nodes.append(
                    LineageNode(
                        id=nid,
                        label=short_label,
                        full_label=full_text,
                        type=str(G.nodes[nid]['type']),
                        x=f'{x_percent}%',
                        y=f'{y_percent}%',
                        raw=G.nodes[nid]['raw'],
                    )
                )

        # Generate Bezier Curves for Edges
        new_edges = []
        for u, v in G.edges:
            if u not in node_coords or v not in node_coords:
                continue

            x1, y1 = node_coords[u]
            x2, y2 = node_coords[v]

            mid_x = (x1 + x2) / 2
            path_d = f'M {x1} {y1} C {mid_x} {y1}, {mid_x} {y2}, {x2} {y2}'

            new_edges.append(LineageEdge(id=f'{u}-{v}', u=u, v=v, path=path_d))

        self.nodes = new_nodes
        self.edges = new_edges


def render_kv_row(k: str, v: str) -> rx.Component:
    return rx.table.row(
        rx.table.cell(
            rx.text(k, font_weight='bold', color='gray', font_size='10px'), padding_y='1'
        ),
        rx.table.cell(rx.text(v, font_size='10px', color='white'), padding_y='1'),
        border_bottom=f'1px solid {style.BORDER_COLOR}',
    )


def detail_modal() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title('Node Details'),
            rx.vstack(
                rx.badge(
                    LineageState.selected_node_info.type.replace('_', ' '),
                    variant='solid',
                    bg=rx.match(
                        LineageState.selected_node_info.type,
                        ('asset', '#8b5cf6'),
                        ('note', '#10b981'),
                        ('memory_unit', '#3b82f6'),
                        ('observation', '#f59e0b'),
                        ('mental_model', '#ef4444'),
                        '#888',
                    ),
                    color='white',
                    text_transform='capitalize',
                ),
                rx.text(
                    rx.text.strong('ID: '),
                    LineageState.selected_node_info.id,
                    font_size='10px',
                    color='gray',
                ),
                rx.divider(),
                # Content / Label
                rx.text(rx.text.strong('Content/Title:')),
                rx.text(LineageState.selected_node_info.label, size='2', color='white'),
                rx.divider(),
                # Properties Table
                rx.text(rx.text.strong('Properties:')),
                rx.scroll_area(
                    rx.table.root(
                        rx.table.body(
                            rx.foreach(
                                LineageState.selected_node_info.raw,
                                lambda item: render_kv_row(item['key'], item['value']),
                            )
                        ),
                        width='100%',
                    ),
                    height='300px',
                ),
                rx.dialog.close(
                    rx.button('Close', variant='soft', color_scheme='gray', margin_top='16px'),
                ),
                spacing='4',
                align='stretch',
            ),
        ),
        open=LineageState.is_modal_open,
        on_open_change=LineageState.close_modal,
    )


def column_header(label: str, left: str) -> rx.Component:
    return rx.text(
        label,
        position='absolute',
        left=left,
        top='10px',
        transform='translateX(-50%)',
        color=style.SECONDARY_TEXT,
        font_size='10px',
        font_weight='bold',
        text_transform='uppercase',
        letter_spacing='1px',
    )


def legend_item(item: Dict[str, str]) -> rx.Component:
    return rx.hstack(
        rx.box(width='10px', height='10px', bg=item['color'], border_radius='50%'),
        rx.text(item['label'], size='1', color=style.SECONDARY_TEXT),
        align='center',
        spacing='2',
    )


def lineage_graph_view() -> rx.Component:
    return rx.box(
        # Empty State Overlay
        rx.cond(
            (LineageState.nodes.length() == 0) & (LineageState.target_id != ''),  # type: ignore
            rx.center(
                rx.vstack(
                    rx.icon('circle-alert', size=40, color='gray'),
                    rx.text('No lineage data found for this entity.', color='gray'),
                    rx.text(
                        'Try selecting a different model or verifying the ID.',
                        size='1',
                        color='gray',
                    ),
                    spacing='2',
                ),
                position='absolute',
                top='0',
                left='0',
                width='100%',
                height='100%',
                z_index='30',
                bg='rgba(0,0,0,0.4)',
            ),
        ),
        # Legend
        rx.hstack(
            rx.foreach(LineageState.legend_items, legend_item),
            position='absolute',
            bottom='16px',
            right='16px',
            bg='rgba(0,0,0,0.5)',
            padding='8px',
            border_radius='8px',
            spacing='4',
            z_index='20',
        ),
        # Column Headers
        column_header('Assets', '5%'),
        column_header('Notes', '20%'),
        column_header('Memory Units', '45%'),
        column_header('Observations', '70%'),
        column_header('Mental Models', '85%'),
        # Edges Layer (SVG)
        rx.el.svg(
            rx.foreach(
                LineageState.edges,
                lambda e: rx.el.path(
                    d=e.path,
                    fill='none',
                    # Highlight logic
                    stroke=rx.cond(
                        LineageState.highlighted_edge_ids.contains(e.id),  # type: ignore
                        '#fff',  # White if highlighted
                        '#444',  # Dim if not
                    ),
                    stroke_width=rx.cond(
                        LineageState.highlighted_edge_ids.contains(e.id),  # type: ignore
                        '2.5',
                        '1.5',
                    ),
                    opacity=rx.cond(
                        LineageState.highlighted_edge_ids.length() > 0,  # type: ignore
                        rx.cond(
                            LineageState.highlighted_edge_ids.contains(e.id),  # type: ignore
                            '0.5',  # Lit up
                            '0.1',  # Dim others
                        ),
                        '0.6',  # Default
                    ),
                    style={'transition': 'all 0.2s ease'},
                ),
            ),
            width='100%',
            height='100%',
            view_box='0 0 100 100',
            preserve_aspect_ratio='none',
            position='absolute',
            top='0',
            left='0',
            pointer_events='none',
            style={'overflow': 'visible'},
        ),
        # Nodes Layer
        rx.foreach(
            LineageState.nodes,
            lambda n: rx.box(
                rx.tooltip(
                    rx.box(
                        width='40px',
                        height='40px',
                        bg=rx.match(
                            n.type,
                            ('asset', '#8b5cf6'),
                            ('note', '#10b981'),
                            ('memory_unit', '#3b82f6'),
                            ('observation', '#f59e0b'),
                            ('mental_model', '#ef4444'),
                            '#888',
                        ),
                        border_radius='50%',
                        # Highlighting Styles
                        border=rx.cond(
                            LineageState.highlighted_node_ids.contains(n.id),  # type: ignore
                            '2px solid white',
                            '2px solid #222',
                        ),
                        opacity=rx.cond(
                            (LineageState.highlighted_node_ids.length() > 0)  # type: ignore
                            & (~LineageState.highlighted_node_ids.contains(n.id)),  # type: ignore
                            '0.2',  # Fade out non-highlighted
                            '1.0',
                        ),
                        _hover={
                            'border': '2px solid white',
                            'cursor': 'pointer',
                            'transform': 'scale(1.2)',
                        },
                        transition='all 0.2s ease',
                    ),
                    content=n.label,
                ),
                position='absolute',
                left=n.x,
                top=n.y,
                transform='translate(-50%, -50%)',
                on_click=LineageState.select_node(n.id),  # type: ignore
                on_mouse_enter=LineageState.set_hovered_node(n.id),  # type: ignore
                on_mouse_leave=LineageState.set_hovered_node(''),  # type: ignore
                z_index='10',
            ),
        ),
        position='relative',
        width='100%',
        height='100%',
        bg=style.SIDEBAR_BG,
        border_radius='12px',
        border=f'1px solid {style.BORDER_COLOR}',
        overflow='hidden',
    )


def lineage_page() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.heading('Lineage Explorer', size='8'),
            rx.spacer(),
            rx.box(
                rx.input(
                    placeholder='Search entities...',
                    value=LineageState.search_query,
                    on_change=LineageState.filter_models,
                    on_blur=LineageState.hide_suggestions,
                    on_key_down=LineageState.on_search_key_down,
                    width='300px',
                    bg=style.SIDEBAR_BG,
                    border=f'1px solid {style.BORDER_COLOR}',
                ),
                rx.cond(
                    LineageState.show_suggestions & (LineageState.filtered_models.length() > 0),  # type: ignore
                    rx.card(
                        rx.vstack(
                            rx.foreach(
                                LineageState.filtered_models,
                                lambda m: rx.box(
                                    rx.text(m[1], size='2'),
                                    width='100%',
                                    padding='8px',
                                    _hover={
                                        'bg': style.ACCENT_COLOR,
                                        'cursor': 'pointer',
                                        'color': 'white',
                                    },
                                    on_mouse_down=LineageState.select_suggestion(m),  # type: ignore
                                ),
                            ),
                            align='stretch',
                            spacing='0',
                        ),
                        position='absolute',
                        top='100%',
                        right='0',
                        width='300px',
                        max_height='300px',
                        overflow_y='auto',
                        z_index='50',
                        bg=style.SIDEBAR_BG,
                        border=f'1px solid {style.BORDER_COLOR}',
                        padding='0',
                    ),
                ),
                position='relative',
                z_index='50',
            ),
            width='100%',
            align='center',
        ),
        rx.box(
            lineage_graph_view(),
            width='100%',
            height='650px',
        ),
        detail_modal(),
        spacing='4',
        width='100%',
        on_mount=LineageState.on_load,
    )
