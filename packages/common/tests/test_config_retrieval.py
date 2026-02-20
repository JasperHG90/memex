from memex_common.config import (
    RetrievalConfig,
    MemexConfig,
    PostgresMetaStoreConfig,
    PostgresInstanceConfig,
    ExtractionConfig,
    ModelConfig,
    ServerConfig,
    MemoryConfig,
)


def test_retrieval_config_defaults():
    config = RetrievalConfig()
    assert config.token_budget == 2000


def test_retrieval_config_override():
    config = RetrievalConfig(token_budget=5000)
    assert config.token_budget == 5000


def test_memex_config_has_retrieval_defaults():
    # Setup minimal valid MemexConfig
    meta_store_data = {
        'host': 'localhost',
        'database': 'memex',
        'user': 'admin',
        'password': 'password',
    }

    config = MemexConfig(
        server=ServerConfig(
            meta_store=PostgresMetaStoreConfig(instance=PostgresInstanceConfig(**meta_store_data)),
            memory=MemoryConfig(
                extraction=ExtractionConfig(model=ModelConfig(model='gemini/flash'))
            ),
        )
    )

    assert config.server.memory.retrieval is not None
    assert config.server.memory.retrieval.token_budget == 2000
