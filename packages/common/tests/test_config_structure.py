import os
from unittest.mock import patch

from memex_common.config import (
    DocSearchStrategiesConfig,
    DocumentConfig,
    MemexConfig,
    ModelConfig,
    PageIndexTextSplitting,
    SearchStrategiesConfig,
)


def test_config_nested_structure():
    config = MemexConfig(
        server={
            'memory': {
                'extraction': {'model': {'model': 'gemini/flash'}},
                'reflection': {'model': {'model': 'gemini/pro'}},
            }
        },
    )

    assert config.server.memory.extraction.model.model == 'gemini/flash'
    assert config.server.memory.reflection.model.model == 'gemini/pro'


def test_config_env_vars():
    with patch.dict(
        os.environ,
        {
            'MEMEX_SERVER__MEMORY__EXTRACTION__MODEL__MODEL': 'gemini/env',
        },
    ):
        config = MemexConfig()
        assert config.server.memory.extraction.model.model == 'gemini/env'


def test_config_server_url_default():
    config = MemexConfig()
    assert config.server_url == 'http://127.0.0.1:8000'


def test_config_server_url_override():
    config = MemexConfig(server_url='http://localhost:9000')
    assert config.server_url == 'http://localhost:9000'


def test_config_server_url_derived_from_custom_host_port():
    config = MemexConfig(server={'host': '10.0.0.1', 'port': 9000})
    assert config.server_url == 'http://10.0.0.1:9000'


def test_default_model_propagation():
    """All sub-models inherit the server default_model when not explicitly set."""
    config = MemexConfig()
    expected = 'gemini/gemini-3-flash-preview'
    assert config.server.default_model.model == expected
    assert config.server.memory.extraction.model.model == expected
    assert config.server.memory.reflection.model.model == expected
    assert config.server.document.model.model == expected


def test_default_model_override_propagates():
    """A custom default_model propagates to all sub-configs."""
    config = MemexConfig(server={'default_model': {'model': 'custom/model'}})
    assert config.server.memory.extraction.model.model == 'custom/model'
    assert config.server.memory.reflection.model.model == 'custom/model'
    assert config.server.document.model.model == 'custom/model'


def test_default_model_sub_override_preserved():
    """Sub-config explicit model is preserved; others get the default."""
    config = MemexConfig(
        server={
            'default_model': {'model': 'a'},
            'memory': {'extraction': {'model': {'model': 'b'}}},
        }
    )
    assert config.server.memory.extraction.model.model == 'b'
    assert config.server.memory.reflection.model.model == 'a'
    assert config.server.document.model.model == 'a'


def test_token_fields_on_page_index_text_splitting():
    """PageIndexTextSplitting has token-based fields with reasonable defaults."""
    ts = PageIndexTextSplitting()
    assert ts.scan_chunk_size_tokens == 6000
    assert ts.short_doc_threshold_tokens == 500
    assert ts.max_node_length_tokens == 1250
    assert ts.block_token_target == 2000
    assert ts.min_node_tokens == 0


def test_search_strategies_config_defaults():
    s = SearchStrategiesConfig()
    assert s.semantic is True
    assert s.keyword is True
    assert s.graph is True
    assert s.temporal is True
    assert s.mental_model is True


def test_search_strategies_config_override():
    s = SearchStrategiesConfig(semantic=False, mental_model=False)
    assert s.semantic is False
    assert s.keyword is True
    assert s.mental_model is False


def test_doc_search_strategies_config_defaults():
    s = DocSearchStrategiesConfig()
    assert s.semantic is True
    assert s.keyword is True
    assert s.graph is True
    assert s.temporal is True
    assert not hasattr(s, 'mental_model')


def test_document_config_defaults():
    d = DocumentConfig()
    assert d.model is None
    assert isinstance(d.search_strategies, DocSearchStrategiesConfig)
    assert d.search_strategies.semantic is True


def test_document_config_with_model():
    d = DocumentConfig(model={'model': 'gemini/gemini-3-flash-preview'})
    assert isinstance(d.model, ModelConfig)
    assert d.model.model == 'gemini/gemini-3-flash-preview'


def test_server_config_document_strategies_via_memex_config():
    config = MemexConfig(
        server={
            'document': {
                'search_strategies': {'semantic': False, 'keyword': True},
            }
        }
    )
    assert config.server.document.search_strategies.semantic is False
    assert config.server.document.search_strategies.keyword is True


def test_retrieval_strategies_defaults():
    config = MemexConfig()
    strategies = config.server.memory.retrieval.retrieval_strategies
    assert strategies.semantic is True
    assert strategies.keyword is True
    assert strategies.mental_model is True
