# Memex Core (`memex-core`)

The engine powering Memex. It implements the "Hindsight" framework for memory consolidation, retrieval, and reflection.

## Key Features

- **Extraction Pipeline**: Converts unstructured text into structured memory units (facts, observations).
- **TEMPR Retrieval**: A 4-channel retrieval system (Temporal, Entity, Mental Model, Semantic).
- **Reflection Engine**: Periodically synthesizes insights from raw memories to form high-level mental models.
- **Storage Abstraction**: Backends for files (Local) and metadata (PostgreSQL).

## Documentation

- [Hindsight Framework](../../docs/explanation/hindsight-framework.md)
- [Extraction Pipeline](../../docs/explanation/extraction-pipeline.md)
- [REST API Reference](../../docs/reference/rest-api.md)
