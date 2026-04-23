from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hermes_runner import agent as agent_module
from hermes_runner.config import HermesRunnerSettings


class _FakeAgent:
    instances: list['_FakeAgent'] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.conversations: list[dict[str, Any]] = []
        _FakeAgent.instances.append(self)

    def run_conversation(self, user_message: str, task_id: str | None = None) -> dict[str, Any]:
        payload = {
            'user_message': user_message,
            'task_id': task_id,
        }
        self.conversations.append(payload)
        return {
            'final_response': 'mock review output',
            'messages': [{'role': 'user', 'content': user_message}],
            'task_id': task_id,
        }


@pytest.fixture
def fake_run_agent(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAgent]:
    _FakeAgent.instances = []
    module = types.ModuleType('run_agent')
    module.AIAgent = _FakeAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'run_agent', module)
    return _FakeAgent


@pytest.fixture
def base_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HermesRunnerSettings:
    prompt = tmp_path / 'sys.md'
    prompt.write_text('SYS', encoding='utf-8')
    monkeypatch.setenv('HERMES_RUNNER_API_KEY', 'k')
    monkeypatch.setenv('HERMES_RUNNER_SYSTEM_PROMPT_FILE', str(prompt))
    monkeypatch.setenv('HERMES_RUNNER_USER_MESSAGE', 'USR')
    return HermesRunnerSettings()  # type: ignore[call-arg]


def test_build_agent_passes_settings(
    fake_run_agent: type[_FakeAgent],
    base_settings: HermesRunnerSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('HERMES_RUNNER_ENABLED_TOOLSETS', 'web')
    monkeypatch.setenv('HERMES_RUNNER_DISABLED_TOOLSETS', 'terminal,files')
    monkeypatch.setenv('HERMES_RUNNER_MAX_ITERATIONS', '3')
    settings = HermesRunnerSettings()  # type: ignore[call-arg]

    built = agent_module.build_agent(settings)
    assert isinstance(built, _FakeAgent)
    kw = built.kwargs
    assert kw['model'] == 'ollama-cloud/glm-5.1'
    assert kw['ephemeral_system_prompt'] == 'SYS'
    assert kw['api_key'] == 'k'
    assert kw['max_iterations'] == 3
    assert kw['base_url'] == 'https://ollama.com/v1'
    assert kw['enabled_toolsets'] == ['web']
    assert kw['disabled_toolsets'] == ['terminal', 'files']
    assert kw['skip_context_files'] is True
    assert kw['skip_memory'] is True
    assert kw['quiet_mode'] is True


def test_build_agent_omits_empty_extras(
    fake_run_agent: type[_FakeAgent],
    base_settings: HermesRunnerSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('HERMES_RUNNER_BASE_URL', '')
    settings = HermesRunnerSettings()  # type: ignore[call-arg]
    built = agent_module.build_agent(settings)
    assert isinstance(built, _FakeAgent)
    assert 'base_url' not in built.kwargs
    assert 'enabled_toolsets' not in built.kwargs
    assert 'disabled_toolsets' not in built.kwargs


def test_run_invokes_conversation(
    fake_run_agent: type[_FakeAgent],
    base_settings: HermesRunnerSettings,
) -> None:
    built = agent_module.build_agent(base_settings)
    assert isinstance(built, _FakeAgent)
    result = agent_module.run(base_settings, built)
    assert result['final_response'] == 'mock review output'
    assert built.conversations == [{'user_message': 'USR', 'task_id': 'hermes-runner'}]


def test_main_writes_outputs(
    fake_run_agent: type[_FakeAgent],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt = tmp_path / 'sys.md'
    prompt.write_text('SYS', encoding='utf-8')
    runner_temp = tmp_path / 'runner_temp'
    runner_temp.mkdir()
    out_file = tmp_path / 'agent-out.txt'

    monkeypatch.setenv('HERMES_RUNNER_API_KEY', 'k')
    monkeypatch.setenv('HERMES_RUNNER_SYSTEM_PROMPT_FILE', str(prompt))
    monkeypatch.setenv('HERMES_RUNNER_USER_MESSAGE', 'USR')
    monkeypatch.setenv('HERMES_RUNNER_OUTPUT_FILE', str(out_file))
    monkeypatch.setenv('RUNNER_TEMP', str(runner_temp))

    from hermes_runner.__main__ import main

    exit_code = main()
    assert exit_code == 0

    assert (runner_temp / 'hermes-response.txt').read_text(encoding='utf-8') == 'mock review output'
    trajectory = json.loads((runner_temp / 'hermes-trajectory.json').read_text(encoding='utf-8'))
    assert trajectory == [{'role': 'user', 'content': 'USR'}]
    assert out_file.read_text(encoding='utf-8') == 'mock review output'
    captured = capsys.readouterr()
    assert 'mock review output' in captured.out
