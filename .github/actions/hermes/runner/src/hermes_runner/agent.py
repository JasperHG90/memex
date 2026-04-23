"""Thin wrapper around Hermes ``AIAgent`` so tests can patch construction."""

from __future__ import annotations

from typing import Any, Protocol

from .config import HermesRunnerSettings


class _AgentLike(Protocol):
    def run_conversation(self, user_message: str, task_id: str | None = ...) -> dict[str, Any]: ...


def build_agent(settings: HermesRunnerSettings) -> _AgentLike:
    from run_agent import AIAgent  # imported lazily so tests can stub it

    kwargs: dict[str, Any] = {
        'model': settings.model,
        'ephemeral_system_prompt': settings.system_prompt,
        'api_key': settings.api_key.get_secret_value(),
        'max_iterations': settings.max_iterations,
        'quiet_mode': settings.quiet_mode,
        'skip_context_files': True,
        'skip_memory': True,
    }
    if settings.base_url:
        kwargs['base_url'] = settings.base_url
    if settings.enabled_toolsets:
        kwargs['enabled_toolsets'] = settings.enabled_toolsets
    if settings.disabled_toolsets:
        kwargs['disabled_toolsets'] = settings.disabled_toolsets
    return AIAgent(**kwargs)


def run(settings: HermesRunnerSettings, agent: _AgentLike) -> dict[str, Any]:
    return agent.run_conversation(
        user_message=settings.user_message,
        task_id='hermes-runner',
    )
