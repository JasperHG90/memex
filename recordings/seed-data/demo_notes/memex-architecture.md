# Memex Architecture

## Overview

Memex is a long-term memory system designed for large language models (LLMs). It enables persistent knowledge accumulation across sessions by storing, extracting, and synthesizing information from notes and documents. The system is built on Python with PostgreSQL and pgvector as its storage backbone.

## Core Components

### Storage Layer

Memex uses a dual-storage architecture:

- **FileStore**: Stores raw note content as Markdown files using an fsspec abstraction layer. This makes the storage backend-agnostic, supporting local filesystems, Amazon S3, and Google Cloud Storage transparently.
- **MetaStore**: A PostgreSQL database with pgvector extension that stores metadata, entities, memory units, embeddings, and relationships. PostgreSQL was chosen for its reliability, ACID compliance, and the pgvector extension's support for efficient vector similarity search using HNSW indexes.

### Append-Only Design

Memex follows an append-only design philosophy. Notes are immutable once ingested. Updates create new versions rather than modifying existing entries. This approach provides a complete audit trail, simplifies concurrent access, and aligns with event sourcing patterns used in distributed systems.

## The Hindsight Framework

Memex's memory system is organized around the Hindsight Framework, which consists of three phases:

### 1. Extraction

The extraction phase (`memex_core.memory.extraction`) processes ingested notes to produce structured knowledge:

- **Fact extraction**: An LLM analyzes each note to identify discrete facts and events. Each extracted fact becomes a memory unit with metadata including temporal references.
- **Entity resolution**: Named entities (people, technologies, concepts, organizations) are identified and resolved against existing entities in the knowledge graph. Duplicate detection uses both exact matching and semantic similarity to merge references to the same entity.
- **Embedding generation**: Dense vector embeddings are computed for each memory unit and note chunk, enabling semantic similarity search. The embeddings capture meaning beyond keyword overlap, allowing the system to find conceptually related information.

### 2. Retrieval

The retrieval phase (`memex_core.memory.retrieval`) implements the TEMPR architecture, which combines five complementary search strategies:

- **Temporal**: Finds memories relevant to specific time periods or recent events, weighting newer information higher when appropriate.
- **Entity**: Leverages the knowledge graph to find memories connected to specific entities and their co-occurring entities.
- **Mental Model**: Searches synthesized observations and mental models that represent the system's current understanding of entities and topics.
- **Keyword**: Uses BM25-based full-text search for precise term matching, effective when users search for specific technical terms or names.
- **Semantic**: Performs vector similarity search using pgvector's HNSW indexes, finding conceptually related content even without shared keywords.

Results from all strategies are combined using Reciprocal Rank Fusion (RRF), which merges multiple ranked lists into a unified ranking without requiring score calibration across different strategies.

### 3. Reflection

The reflection phase (`memex_core.memory.reflect`) synthesizes accumulated memories into higher-level understanding:

- A distributed reflection queue uses PostgreSQL's `SELECT ... FOR UPDATE SKIP LOCKED` pattern for atomic task claiming, allowing multiple workers to process reflection tasks concurrently without conflicts.
- For each entity, the reflection system examines recent memories, identifies patterns and trends, and generates observations that form the entity's mental model.
- Mental models are living documents that evolve as new information is ingested, providing the system with a continuously updated understanding of each entity.

## API Architecture

### MemexAPI

The `MemexAPI` class (`memex_core.api`) is the primary interface for programmatic access. It exposes methods for ingestion, retrieval, reflection, and entity management.

### FastAPI Server

The REST server (`memex_core.server`) provides HTTP endpoints for all operations. It supports both synchronous and background processing, with NDJSON streaming for large result sets.

### MCP Server

The MCP (Model Context Protocol) server (`memex_mcp.server`) enables LLM tools to interact with Memex natively, providing search, note management, and reflection capabilities as tool calls.

## Vault System

Memex organizes knowledge into vaults, which act as isolated namespaces. Each vault has its own set of notes, entities, and memory units. The vault system supports:

- **Active vault**: The primary write target for new ingestions.
- **Attached vaults**: Read-only vaults included in search and retrieval operations.

This allows separation of concerns, such as keeping personal notes separate from project-specific knowledge while still enabling cross-vault search.

## Technology Stack

- **Python 3.12+**: Core language with full async/await support via asyncio.
- **PostgreSQL + pgvector**: Primary data store with vector search capabilities.
- **FastAPI**: High-performance async web framework.
- **uv**: Modern Python package manager for the monorepo workspace.
- **React + Vite**: Dashboard web interface for visual exploration.
