# Memex Common (`memex-common`)

Shared data models, configuration, and utilities used across all Memex Python packages. This package ensures type consistency between Core, CLI, and MCP.

## Modules

### `config.py` — Configuration

Pydantic Settings-based configuration system with layered resolution (env vars, local YAML, global YAML, defaults).

Key classes:
- `MemexConfig` — Top-level settings container.
- `ServerConfig` — API server, storage, memory subsystem configuration.
- `DashboardConfig` — Dashboard host and port.
- `ModelConfig` — Reusable LLM model configuration block (model ID, base URL, API key, temperature).
- `ExtractionConfig`, `RetrievalConfig`, `ReflectionConfig` — Memory subsystem settings.
- `AuthConfig`, `RateLimitConfig`, `CircuitBreakerConfig`, `LoggingConfig` — Server middleware settings.
- `PostgresMetaStoreConfig`, `LocalFileStoreConfig` — Storage backend configuration.
- `parse_memex_config()` — Factory function for loading config with overrides.

### `schemas.py` — Data Transfer Objects

Pydantic models shared across API boundaries (REST server, CLI, MCP).

Key classes:
- `IngestionPayload` — Note ingestion request (title, content, tags, metadata).
- `MemoryUnit` — Atomic fact or observation extracted from a note.
- `VaultDTO`, `NoteDTO`, `EntityDTO` — API response models.
- `SummaryRequest`, `SummaryResponse` — AI summary endpoint models.
- `CreateVaultRequest` — Vault creation payload.

### `types.py` — Type Definitions

Enumerations and type aliases:
- `MemexTypes` — Memory unit type enumeration (fact, experience, observation).
- `FactTypes` — Fact sub-type enumeration.
- `ReasoningEffort` — LLM reasoning effort levels.

### `exceptions.py` — Error Hierarchy

Standardized exception classes:
- `MemexError` — Base exception for all Memex operations.
- `VaultNotFoundError`, `NoteNotFoundError`, `EntityNotFoundError` — Resource lookup failures.
- `IngestionError`, `ExtractionError` — Pipeline failures.
- `ConfigurationError` — Invalid configuration.

### `mixins.py` — Shared Mixins

- `VaultMixin` — Adds `vault_id` field to Pydantic models that are vault-scoped.

### `client.py` — HTTP Client

- `MemexClient` — Async HTTP client for the Memex REST API, used by CLI and MCP packages.

## Installation

This package is automatically installed as a dependency of `memex-core`, `memex-cli`, and `memex-mcp`. For standalone use:

```bash
uv add memex-common
```

## Documentation

- [Configuration Reference](../../docs/reference/configuration.md)
