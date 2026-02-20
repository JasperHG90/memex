import pytest
import re
from unittest.mock import AsyncMock
from typer.testing import CliRunner
from memex_common.config import (
    MemexConfig,
    PostgresMetaStoreConfig,
    ExtractionConfig,
    PostgresInstanceConfig,
    SecretStr,
    LocalFileStoreConfig,
    ServerConfig,
    MemoryConfig,
    ModelConfig,
)


@pytest.fixture
def runner():
    """Typer CLI Runner fixture."""
    return CliRunner()


@pytest.fixture
def mock_config():
    """Default MemexConfig fixture."""
    return MemexConfig(
        server=ServerConfig(
            meta_store=PostgresMetaStoreConfig(
                instance=PostgresInstanceConfig(
                    host='localhost',
                    port=5432,
                    database='db',
                    user='user',
                    password=SecretStr('pass'),
                )
            ),
            memory=MemoryConfig(
                extraction=ExtractionConfig(model=ModelConfig(model='gemini/test'))
            ),
            file_store=LocalFileStoreConfig(root='/tmp'),
        )
    )


@pytest.fixture
def mock_api():
    """Mock RemoteMemexAPI with async context manager support."""
    mock = AsyncMock()
    mock.__aenter__.return_value = mock
    mock.__aexit__.return_value = None
    return mock


@pytest.fixture(autouse=True)
def disable_config_loading(monkeypatch):
    """Disable loading of local and global configuration files during tests."""
    monkeypatch.setenv('MEMEX_LOAD_LOCAL_CONFIG', 'false')
    monkeypatch.setenv('MEMEX_LOAD_GLOBAL_CONFIG', 'false')


@pytest.fixture(autouse=True)
def disable_color(monkeypatch):
    """Disable ANSI color output for easier parsing of CLI output."""
    monkeypatch.setenv('NO_COLOR', '1')
    monkeypatch.setenv('TERM', 'dumb')


@pytest.fixture
def strip_ansi():
    """Fixture providing a helper to remove ANSI escape sequences from strings."""

    def _strip(text: str) -> str:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|[0-9A-F]{2}|[0-9]{1,3}(?:;[0-9]{1,3})*)')
        return ansi_escape.sub('', text)

    return _strip
