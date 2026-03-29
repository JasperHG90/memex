"""Configuration for memex note sync.

Uses Pydantic BaseSettings with layered sources:
1. Environment variables (prefix: MEMEX_SYNC_)
2. TOML config file (note-sync.toml)
3. Defaults

Env var nesting uses double underscore: MEMEX_SYNC_SYNC__BATCH_SIZE
"""

from __future__ import annotations

import tomllib
import warnings
from enum import Enum
from pathlib import Path
from typing import Any

import platformdirs
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_BASE_EXCLUDES = ['.obsidian', '.trash', '.git', 'node_modules']
DEFAULT_ASSET_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', '.webp']
CONFIG_FILENAME = 'note-sync.toml'
LEGACY_CONFIG_FILENAME = 'obsidian-sync.toml'


class ExcludeConfig(BaseModel):
    """Patterns for excluding files and directories from sync."""

    base: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BASE_EXCLUDES),
        description='Directory/file patterns always excluded. '
        'Defaults to Obsidian internals: .obsidian, .trash, .git, node_modules.',
    )
    extends_exclude: list[str] = Field(
        default_factory=list,
        description='Additional glob patterns to exclude beyond the base set. '
        'Examples: "templates/", "daily-notes/", "_archive/**".',
    )
    ignore_folders: list[str] = Field(
        default_factory=list,
        description='Folder names whose notes are never synced. '
        'Unlike base/extends_exclude (which use glob matching), these match '
        'exact folder names at any depth. Examples: ["private", "scratch"].',
    )
    frontmatter_skip_key: str = Field(
        default='agents',
        description='Frontmatter key checked to skip a note. '
        'If the note has this key set to the skip value, it is excluded from sync.',
    )
    frontmatter_skip_value: str = Field(
        default='skip',
        description='Value of the frontmatter skip key that causes the note to be skipped. '
        'For example, with key="agents" and value="skip", a note with '
        '"agents: skip" in its frontmatter will not be synced.',
    )

    @property
    def all_patterns(self) -> list[str]:
        """Combined list of all exclude patterns (base + user extensions)."""
        return self.base + self.extends_exclude


class AssetConfig(BaseModel):
    """Settings for uploading assets (images, PDFs, etc.) referenced in notes."""

    enabled: bool = Field(
        default=True,
        description='Whether to upload assets referenced in notes (images, PDFs, etc.).',
    )
    max_size_mb: int = Field(
        default=50,
        ge=0,
        description='Maximum asset file size in megabytes. Assets larger than this are skipped.',
    )
    extends_include: list[str] = Field(
        default_factory=list,
        description='Additional file extensions to include as assets, '
        f'beyond the defaults: {DEFAULT_ASSET_EXTENSIONS}. '
        'Example: [".mp3", ".wav"].',
    )


class SyncConfig(BaseModel):
    """Settings controlling the sync behavior."""

    state_file: str = Field(
        default='.memex-sync.db',
        description='Filename for the SQLite sync state database, stored in the vault root. '
        'Tracks which files have been synced, their modification times, and sync metadata.',
    )
    batch_size: int = Field(
        default=32,
        ge=1,
        le=100,
        description='Number of notes per batch when ingesting via the batch API. Range: 1-100.',
    )
    exclude: ExcludeConfig = Field(
        default_factory=ExcludeConfig,
        description='File and directory exclusion patterns.',
    )
    assets: AssetConfig = Field(
        default_factory=AssetConfig,
        description='Asset upload settings.',
    )
    note_key_prefix: str = Field(
        default='obsidian',
        description='Prefix for the note_key used for idempotent ingestion. '
        'The full key is "{prefix}:{folder-name}:{relative-path}". '
        'Change to e.g. "notes" or "markdown" for non-Obsidian folders.',
    )
    default_tags: list[str] = Field(
        default_factory=lambda: ['obsidian'],
        description='Tags applied to every synced note. '
        'Change to e.g. ["notes"] for non-Obsidian folders.',
    )


class WatchMode(str, Enum):
    """Available watch modes for continuous sync."""

    events = 'events'
    """Event-driven mode using watchdog filesystem monitoring. Reactive and efficient."""
    poll = 'poll'
    """Polling mode using periodic scans. Simpler, works everywhere."""
    off = 'off'
    """Watch mode disabled."""


class WatchConfig(BaseModel):
    """Settings for continuous background sync (watch mode)."""

    mode: WatchMode = Field(
        default=WatchMode.events,
        description='Watch strategy: "events" (watchdog, reactive), '
        '"poll" (periodic scan), or "off" (disabled).',
    )
    debounce_seconds: int = Field(
        default=5,
        ge=1,
        description='For event mode: seconds to wait after the last filesystem event '
        'before triggering a sync. Prevents rapid re-syncs during editing.',
    )
    poll_interval_seconds: int = Field(
        default=300,
        ge=10,
        description='For poll mode: seconds between each sync cycle.',
    )


class TomlConfigSource(PydanticBaseSettingsSource):
    """Loads configuration from a TOML file.

    Search order:
    1. Explicit path (if provided via _config_path)
    2. vault_path / note-sync.toml
    3. vault_path / obsidian-sync.toml (legacy fallback)
    4. ~/.config/memex/note-sync.toml
    5. ~/.config/memex/obsidian-sync.toml (legacy fallback)
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        vault_path: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        super().__init__(settings_cls)
        self._vault_path = vault_path
        self._config_path = config_path

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        candidates: list[Path] = []

        if self._config_path is not None:
            candidates.append(self._config_path)
        elif self._vault_path is not None:
            candidates.append(self._vault_path / CONFIG_FILENAME)
            candidates.append(self._vault_path / LEGACY_CONFIG_FILENAME)

        global_dir = Path(platformdirs.user_config_dir('memex', appauthor=False))
        candidates.append(global_dir / CONFIG_FILENAME)
        candidates.append(global_dir / LEGACY_CONFIG_FILENAME)

        for path in candidates:
            if path.is_file():
                with open(path, 'rb') as f:
                    data = tomllib.load(f)
                # Backward compat: strip [server] section from old TOMLs
                # (extra='forbid' would reject it)
                if 'server' in data:
                    server_data = data.pop('server')
                    # Promote vault_id to top level if present
                    if 'vault_id' in server_data and 'vault_id' not in data:
                        data['vault_id'] = server_data['vault_id']
                    warnings.warn(
                        f'The [server] section in {path} is deprecated. '
                        'Server connection is now configured in the main memex config '
                        '(~/.config/memex/config.yaml). Only vault_id was preserved.',
                        DeprecationWarning,
                        stacklevel=2,
                    )
                return data
        return {}


class NoteSyncConfig(BaseSettings):
    """Root configuration for memex note sync.

    Values are loaded from (highest priority first):
    1. Environment variables with prefix MEMEX_SYNC_ (nested via __)
    2. TOML config file (note-sync.toml)
    3. Field defaults

    Example env vars:
        MEMEX_SYNC_VAULT_ID=personal
        MEMEX_SYNC_SYNC__BATCH_SIZE=64
        MEMEX_SYNC_WATCH__MODE=poll
    """

    model_config = SettingsConfigDict(
        env_prefix='MEMEX_SYNC_',
        env_nested_delimiter='__',
        extra='forbid',
    )

    vault_id: str | None = Field(
        default=None,
        description='Override vault for sync. Falls back to main memex config write_vault.',
    )
    sync: SyncConfig = Field(
        default_factory=SyncConfig,
        description='Sync behavior settings (exclusions, assets, batching).',
    )
    watch: WatchConfig = Field(
        default_factory=WatchConfig,
        description='Continuous watch/background sync settings.',
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Priority: init > env > TOML file > defaults.

        The TOML source is injected by load_config() via module-level _active_toml_source.
        """
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if _active_toml_source is not None:
            sources.append(_active_toml_source)
        return tuple(sources)


# Module-level slot for the TOML source (set by load_config before instantiation).
_active_toml_source: TomlConfigSource | None = None


def load_config(vault_path: Path, config_path: Path | None = None) -> NoteSyncConfig:
    """Load configuration with layered sources.

    Priority (highest first):
    1. Environment variables (MEMEX_SYNC_*)
    2. TOML file (explicit path > vault root > ~/.config/memex/)
    3. Field defaults

    Args:
        vault_path: Path to the notes directory.
        config_path: Explicit path to a TOML config file. If None,
            searches vault root then global config dir.
    """
    global _active_toml_source
    _active_toml_source = TomlConfigSource(
        NoteSyncConfig,
        vault_path=vault_path,
        config_path=config_path,
    )
    try:
        return NoteSyncConfig()
    finally:
        _active_toml_source = None


DEFAULT_CONFIG_TOML = """\
# Server connection is configured in the main memex config (~/.config/memex/config.yaml)
# Uncomment to override which vault notes are synced to:
# vault_id = ""

[sync]
state_file = ".memex-sync.db"
batch_size = 32
note_key_prefix = "obsidian"
default_tags = ["obsidian"]

[sync.exclude]
base = [".obsidian", ".trash", ".git", "node_modules"]
extends_exclude = []
ignore_folders = []
frontmatter_skip_key = "agents"
frontmatter_skip_value = "skip"

[sync.assets]
enabled = true
max_size_mb = 50
extends_include = []

[watch]
mode = "events"
debounce_seconds = 5
poll_interval_seconds = 300
"""
