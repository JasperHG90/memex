"""Settings loaded from HERMES_RUNNER_* environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: Any) -> list[str]:
    if value is None or value == '':
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [item.strip() for item in str(value).split(',') if item.strip()]


class HermesRunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix='HERMES_RUNNER_',
        env_file=None,
        extra='ignore',
    )

    model: str = 'glm-5.1:cloud'
    provider: str = 'ollama-cloud'
    base_url: str | None = 'https://ollama.com/v1'
    api_key: SecretStr
    system_prompt_file: Path
    user_message: str = Field(..., min_length=1)
    max_iterations: int = 40
    enabled_toolsets: Annotated[list[str], NoDecode] = Field(default_factory=list)
    disabled_toolsets: Annotated[list[str], NoDecode] = Field(default_factory=list)
    output_file: Path | None = None
    quiet_mode: bool = True

    @field_validator('enabled_toolsets', 'disabled_toolsets', mode='before')
    @classmethod
    def _coerce_toolsets(cls, value: Any) -> list[str]:
        return _split_csv(value)

    @field_validator('base_url', mode='before')
    @classmethod
    def _blank_base_url_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == '':
            return None
        return value

    @field_validator('output_file', mode='before')
    @classmethod
    def _blank_output_file_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == '':
            return None
        return value

    @field_validator('user_message', mode='after')
    @classmethod
    def _resolve_at_prefix(cls, value: str) -> str:
        if value.startswith('@'):
            path = Path(value[1:])
            return path.read_text(encoding='utf-8')
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def system_prompt(self) -> str:
        return self.system_prompt_file.read_text(encoding='utf-8')
