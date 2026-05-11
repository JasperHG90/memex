---
tags: [architecture, overview, alpha]
description: Project Alpha architecture overview — service map, data flow.
---

# Project Alpha — Architecture Overview

High-level view of the system. Detailed component docs live alongside
each service's repo.

## Services

- **API gateway** — Python/FastAPI, terminates TLS, routes to internal
  services, owns rate limiting.
- **Ingest service** — Python/asyncio, reads from Apache Kafka, writes
  to PostgreSQL.
- **Search service** — Python, queries PostgreSQL + pgvector, serves
  retrieval requests to the frontend.
- **Recommendation service** — Python, batches embeddings, writes to
  the recommendation table in PostgreSQL.
- **Frontend** — React + TypeScript, talks to the API gateway only.

## Data flow

1. Producers publish events to Kafka topics.
2. The ingest service consumes events, normalizes them, and persists
   to PostgreSQL (`events`, `entities` tables).
3. A background job derives embeddings for new entities and writes
   them into the `entity_embeddings` table (pgvector column).
4. The search service queries `entity_embeddings` for similarity
   search, joined with relational data from `entities`.
5. The recommendation service runs nightly and pre-computes
   recommendation candidates.

## Storage

- **PostgreSQL 16** is the single source of truth. JSONB for schemaless
  rows, pgvector for embeddings, native columns for everything else.
- **No Redis** anymore (sunset November 2025 — see Q3 retro). Caching
  is per-replica in-process LRU.
- **Object store (S3)** holds asset uploads referenced from notes.

## Observability

OpenTelemetry across every service; Prometheus for metrics; Grafana
dashboards owned per-service.

## Security boundary

The API gateway is the only public-facing surface. All internal services
are reachable only via the cluster's service network.
