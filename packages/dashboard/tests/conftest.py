import pytest
import os
from unittest.mock import patch


@pytest.fixture(autouse=True)
def setup_test_env():
    with patch.dict(
        os.environ,
        {
            'MEMEX_SERVER_HOST': 'localhost',
            'MEMEX_SERVER_PORT': '8000',
            'MEMEX_LOAD_LOCAL_CONFIG': 'false',
        },
    ):
        yield
