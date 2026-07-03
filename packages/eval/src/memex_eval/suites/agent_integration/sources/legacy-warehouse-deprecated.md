---
tags: [legacy, deprecated, warehouse]
description: Legacy Redshift warehouse — DEPRECATED, replaced by PostgreSQL.
publish_date: 2025-06-15
---

# Legacy Data Warehouse (DEPRECATED)

> **STATUS:** Deprecated. Read-only since June 2025. Sunset planned
> December 2025.

## What this used to be

Before Project Alpha, Acme Corp ran analytics on **Amazon Redshift**.
The warehouse stored a denormalized copy of transactional data,
refreshed nightly via a glue-job pipeline.

## Why we left

Redshift's cost-per-query model didn't fit our query mix: small,
frequent, low-latency reads. Reservation tiers were either over- or
under-provisioned depending on the hour.

## Current data warehouse

Project Alpha's PostgreSQL 16 + pgvector setup IS now the warehouse.
Analytics workloads ride on read-replicas; the primary serves OLTP.

See `architecture-overview.md` and `tech-stack-decision-record.md`
for the current setup.

## Migration notes (historical)

The June 2025 cutover moved ~14TB from Redshift to PostgreSQL over a
weekend. The legacy Redshift cluster is preserved read-only for audit
queries against pre-June data only.
