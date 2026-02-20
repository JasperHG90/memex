import pytest
from pydantic import ValidationError
from memex_common.config import ModelConfig


def test_model_config_valid():
    config = ModelConfig(
        model='ollama/llama3',
        base_url='http://localhost:11434',
        api_key='sk-test',
        max_tokens=100,
        temperature=0.7,
    )
    assert config.model == 'ollama/llama3'
    assert str(config.base_url) == 'http://localhost:11434/'
    assert config.api_key.get_secret_value() == 'sk-test'


def test_model_config_invalid_url():
    with pytest.raises(ValidationError):
        ModelConfig(model='test', base_url='not-a-url')


def test_model_config_defaults():
    config = ModelConfig(model='test')
    assert config.base_url is None
    assert config.api_key is None
    assert config.max_tokens is None
    assert config.temperature is None
