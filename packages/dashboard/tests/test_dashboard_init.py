from memex_dashboard.state import State
from memex_dashboard.api import api_client


def test_state_init():
    state = State()
    assert state.current_page == 'Overview'


def test_api_client_initialization():
    assert api_client is not None
    assert api_client.api is not None
    # Check base_url
    assert str(api_client._client.base_url) == 'http://127.0.0.1:8000/api/v1/'
