---
tags: [project, adr, alpha]
description: Decision record — why PostgreSQL was chosen for Project Alpha.
publish_date: 2025-02-28
---

# Tech Stack Decision Record — PostgreSQL choice for Project Alpha

**Date:** February 28, 2025
**Status:** Accepted

## Context

Project Alpha needs a primary datastore for transactional + analytical
workloads with vector search for the recommendation surface.

## Decision

We chose **PostgreSQL 16** with the `pgvector` extension.

## Why PostgreSQL

1. **ACID guarantees** — Project Alpha's billing data requires strong
   consistency. NoSQL alternatives (DynamoDB, MongoDB) were rejected on
   this basis.
2. **JSONB + pgvector** — a single store covers structured rows, schemaless
   document columns, and 1536-dimensional embeddings without operational
   sprawl.
3. **Team familiarity** — every engineer on the founding team has run
   PostgreSQL in production. Onboarding cost is effectively zero.
4. **Mature ecosystem** — Alembic for migrations, asyncpg + SQLModel for
   the application layer, well-understood pg_dump/pg_restore for backups.

## Rejected alternatives

- DynamoDB: no relational joins, transactional limits, lock-in.
- Cassandra: write-amplification under our update-heavy pipeline.
- ClickHouse: analytical-only, doesn't fit the transactional surface.
