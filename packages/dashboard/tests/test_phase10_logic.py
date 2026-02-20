import pytest
from unittest.mock import AsyncMock, patch
from memex_dashboard.pages.search import SearchState, SearchResult
from memex_dashboard.pages.lineage import LineageState, LineageNode
from memex_dashboard.pages.entity import EntityState, GraphNode, GraphEdge
from memex_dashboard.state import State
from memex_common.schemas import NoteDTO


@pytest.fixture
def mock_api_client():
    with patch('memex_dashboard.pages.search.api_client') as mock:
        yield mock


@pytest.fixture
def mock_api_client_lineage():
    with patch('memex_dashboard.pages.lineage.api_client') as mock:
        yield mock


@pytest.fixture
def mock_api_client_state():
    with patch('memex_dashboard.state.api_client') as mock:
        yield mock


class TestSearchLogic:
    def test_open_details_metadata_rendering(self):
        """Test that metadata is correctly converted to List[List[str]]."""
        state = SearchState()

        # Happy Path: Metadata with various types
        result = SearchResult(
            id='123',
            text='Test content',
            metadata={'author': 'me', 'year': 2026, 'tags': ['a', 'b']},
        )

        state.open_details(result)

        assert state.is_modal_open is True
        assert state.selected_result == result
        # Verify conversion to string pairs
        expected = [
            {'key': 'author', 'value': 'me'},
            {'key': 'year', 'value': '2026'},
            {'key': 'tags', 'value': "['a', 'b']"},
        ]
        # Sort to compare regardless of dict order
        state.metadata_list.sort(key=lambda x: x['key'])
        expected.sort(key=lambda x: x['key'])
        assert state.metadata_list == expected

    def test_open_details_empty_metadata(self):
        """Test edge case with empty metadata."""
        state = SearchState()
        result = SearchResult(id='123', text='Test', metadata={})

        state.open_details(result)

        assert state.metadata_list == []
        assert state.is_modal_open is True


class TestLineageLogic:
    def test_select_node_metadata_flattening(self):
        """Test that node metadata is flattened and modal is opened."""
        state = LineageState()

        # Complex nested metadata
        raw_data = {
            'name': 'Node A',
            'info': {'created': '2026-02-03', 'nested': {'val': 1}},
            'list': [1, 2],
        }
        node = LineageNode(
            id='node1', label='Node A', type='document', x='10%', y='10%', raw=raw_data
        )
        state.nodes = [node]

        state.select_node(node.id)

        assert state.is_modal_open is True
        assert state.selected_node_info.id == 'node1'
        assert state.selected_node_info.label == 'Node A'

        # Verify Flattening
        # keys: name, info.created, info.nested.val, list
        keys = [item['key'] for item in state.selected_node_info.raw]
        assert 'name' in keys
        assert 'info.created' in keys
        assert 'info.nested.val' in keys
        assert 'list' in keys

        # Verify Values are strings
        vals = {item['key']: item['value'] for item in state.selected_node_info.raw}
        assert vals['info.nested.val'] == '1'
        assert vals['list'] == '[1, 2]'


class TestEntityInteraction:
    def test_drag_node_logic(self):
        """Test dragging a node updates its position."""
        with patch('time.time') as mock_time:
            mock_time.side_effect = [100.0, 100.1, 100.2, 100.3, 100.4]
            state = EntityState()
            # Setup initial state
            node = GraphNode(id='n1', label='N1', x=100, y=100, size=10, color='red')
            state.nodes = [node]
            state.zoom = 1.0

            # 1. Start Drag
            state.start_drag_node('n1')
            # Initialize start position (simulating first move event after down)
            state.on_mouse_move(x=500, y=500)

            assert state.drag_node_id == 'n1'
            assert state.last_mouse_x == 500
            assert state.last_mouse_y == 500

            # 2. Move Mouse (10px right, 10px down)
            state.on_mouse_move(x=510, y=510)

            assert state.nodes[0].x == 110  # 100 + 10
            assert state.nodes[0].y == 110  # 100 + 10
            assert state.last_mouse_x == 510
            assert state.last_mouse_y == 510

            # 3. Stop Drag
            state.on_mouse_up()
            assert state.drag_node_id is None

    def test_drag_node_with_edges(self):
        """Test that dragging a node updates connected edges."""
        with patch('time.time') as mock_time:
            mock_time.side_effect = [100.0, 100.1, 100.2, 100.3]
            state = EntityState()
            node1 = GraphNode(id='n1', label='N1', x=100, y=100, size=10, color='red')
            node2 = GraphNode(id='n2', label='N2', x=200, y=200, size=10, color='blue')
            edge = GraphEdge(id='e1', u='n1', v='n2', x1=100, y1=100, x2=200, y2=200)

            state.nodes = [node1, node2]
            state.edges = [edge]
            state.zoom = 1.0

            state.start_drag_node('n1')
            state.on_mouse_move(x=0, y=0)  # Initialize start
            state.on_mouse_move(x=50, y=0)  # Move n1 +50 X

            assert state.nodes[0].x == 150
            assert state.edges[0].x1 == 150  # Edge start moved
            assert state.edges[0].x2 == 200  # Edge end stayed

    def test_pan_logic(self):
        """Test panning the canvas."""
        with patch('time.time') as mock_time:
            mock_time.side_effect = [100.0, 100.1, 100.2, 100.3]
            state = EntityState()
            state.pan_x = 0
            state.pan_y = 0
            state.zoom = 1.0

            # 1. Start Pan
            state.start_pan()
            state.on_mouse_move(x=100, y=100)  # Initialize start
            assert state.is_panning is True

            # 2. Move Mouse (Drag map right => move viewbox left? No, usually drag right means pan x decreases or increases depending on implementation)
            # Logic in code: self.pan_x -= dx * scale
            # If I drag mouse RIGHT (dx > 0), pan_x decreases.
            # (Moving "camera" left relative to content, or content right)

            state.on_mouse_move(x=150, y=100)  # dx = +50

            assert state.pan_x == -50.0
            assert state.pan_y == 0.0

            # 3. Stop Pan
            state.on_mouse_up()
            assert state.is_panning is False


class TestQuickNote:
    @pytest.mark.asyncio
    async def test_save_quick_note_success(self, mock_api_client_state):
        """Test successful quick note saving with correct NoteDTO."""
        state = State()
        state.quick_note_content = 'Important idea'

        # Setup mock
        mock_api_client_state.api.ingest = AsyncMock()

        # Mock get_state to return a VaultState with no writer_vault_id
        mock_vault_state = type('VaultState', (), {'writer_vault_id': None})()
        with patch.object(State, 'get_state', AsyncMock(return_value=mock_vault_state)):
            await state.save_quick_note()

        # Verify API call
        mock_api_client_state.api.ingest.assert_called_once()
        call_args = mock_api_client_state.api.ingest.call_args[0][0]

        assert isinstance(call_args, NoteDTO)
        assert call_args.name == 'Quick Note'
        # NoteDTO.content stores Base64 encoded bytes
        assert call_args.content == b'SW1wb3J0YW50IGlkZWE='
        assert 'dashboard' in call_args.tags

        # Verify State Reset
        assert state.quick_note_content == ''
        assert state.is_quick_note_open is False

    @pytest.mark.asyncio
    async def test_save_quick_note_empty(self, mock_api_client_state):
        """Test that empty notes are ignored."""
        state = State()
        state.quick_note_content = ''
        mock_api_client_state.api.ingest = AsyncMock()

        await state.save_quick_note()

        mock_api_client_state.api.ingest.assert_not_called()
