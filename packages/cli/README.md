# Memex CLI (`memex-cli`)

The command-line interface for Memex, your personal AI knowledge vault.

## Overview

The `memex` CLI allows you to:
- **Ingest** documents, URLs, and folders.
- **Search** your knowledge base using semantic and keyword strategies.
- **Manage** vaults, entities, and memories.
- **Run** the Memex server and dashboard.

## Installation

```bash
uv tool install memex-cli
```

## Quick Start

```bash
# Initialize configuration
memex config init

# Start server (required for all operations)
memex server start

# Ingest a webpage
memex memory add --url "https://example.com"

# Search for answers
memex memory search "What are the key points?"
```

## Documentation

For a complete command reference, see the [CLI Reference](../../docs/reference/cli-commands.md).
