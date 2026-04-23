from __future__ import annotations

from pathlib import Path

import pytest

from hermes_runner.config import HermesRunnerSettings


@pytest.fixture
def prompt_file(tmp_path: Path) -> Path:
    path = tmp_path / 'system.md'
    path.write_text('system prompt body', encoding='utf-8')
    return path


def _base_env(monkeypatch: pytest.MonkeyPatch, prompt_file: Path) -> None:
    monkeypatch.setenv('HERMES_RUNNER_API_KEY', 'secret-abc')
    monkeypatch.setenv('HERMES_RUNNER_SYSTEM_PROMPT_FILE', str(prompt_file))
    monkeypatch.setenv('HERMES_RUNNER_USER_MESSAGE', 'hi')


def test_defaults_apply(monkeypatch: pytest.MonkeyPatch, prompt_file: Path) -> None:
    _base_env(monkeypatch, prompt_file)
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    assert settings.model == 'ollama-cloud/glm-5.1'
    assert settings.provider == 'ollama-cloud'
    assert settings.base_url == 'https://ollama.com/v1'
    assert settings.max_iterations == 40
    assert settings.enabled_toolsets == []
    assert settings.disabled_toolsets == []
    assert settings.output_file is None


def test_env_override(monkeypatch: pytest.MonkeyPatch, prompt_file: Path) -> None:
    _base_env(monkeypatch, prompt_file)
    monkeypatch.setenv('HERMES_RUNNER_MODEL', 'anthropic/claude-sonnet-4')
    monkeypatch.setenv('HERMES_RUNNER_PROVIDER', 'anthropic')
    monkeypatch.setenv('HERMES_RUNNER_BASE_URL', '')
    monkeypatch.setenv('HERMES_RUNNER_MAX_ITERATIONS', '12')
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    assert settings.model == 'anthropic/claude-sonnet-4'
    assert settings.provider == 'anthropic'
    assert settings.base_url is None
    assert settings.max_iterations == 12


def test_toolsets_csv_parsing(monkeypatch: pytest.MonkeyPatch, prompt_file: Path) -> None:
    _base_env(monkeypatch, prompt_file)
    monkeypatch.setenv('HERMES_RUNNER_ENABLED_TOOLSETS', 'web, terminal ,files')
    monkeypatch.setenv('HERMES_RUNNER_DISABLED_TOOLSETS', '')
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    assert settings.enabled_toolsets == ['web', 'terminal', 'files']
    assert settings.disabled_toolsets == []


def test_api_key_is_secret(monkeypatch: pytest.MonkeyPatch, prompt_file: Path) -> None:
    _base_env(monkeypatch, prompt_file)
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    assert settings.api_key.get_secret_value() == 'secret-abc'
    assert 'secret-abc' not in repr(settings)
    assert 'secret-abc' not in repr(settings.api_key)


def test_system_prompt_reads_file(monkeypatch: pytest.MonkeyPatch, prompt_file: Path) -> None:
    _base_env(monkeypatch, prompt_file)
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    assert settings.system_prompt == 'system prompt body'


def test_user_message_at_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prompt_file: Path
) -> None:
    body = tmp_path / 'msg.txt'
    body.write_text('loaded from file', encoding='utf-8')
    _base_env(monkeypatch, prompt_file)
    monkeypatch.setenv('HERMES_RUNNER_USER_MESSAGE', f'@{body}')
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    assert settings.user_message == 'loaded from file'


def test_user_message_literal_passthrough(
    monkeypatch: pytest.MonkeyPatch, prompt_file: Path
) -> None:
    _base_env(monkeypatch, prompt_file)
    monkeypatch.setenv('HERMES_RUNNER_USER_MESSAGE', 'inline text')
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    assert settings.user_message == 'inline text'
