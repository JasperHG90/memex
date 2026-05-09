---
tags: [tech-stack, project-nexus, infrastructure, migration]
description: Project Nexus technology stack after the July 2025 migration.
---

# Project Nexus — Tech Stack (July 2025)

## Updated Stack

Following the Q2 2025 migration, Project Nexus has updated its stack:

- **Language:** Python 3.12 (upgraded from 3.11)
- **Web Framework:** FastAPI (migrated from Django 4.2)
- **Database:** PostgreSQL 16 (migrated from MySQL 8.0)
- **CI/CD:** GitHub Actions (migrated from Jenkins)
- **Deployment:** Docker + Kubernetes on AWS EKS (unchanged)
- **Monitoring:** Datadog (unchanged)

## Migration Rationale

- FastAPI provides async support and automatic OpenAPI docs, better suited for our API-first architecture.
- PostgreSQL 16 offers pgvector for AI workloads and superior JSON/JSONB support.
- GitHub Actions integrates natively with our GitHub repos, reducing maintenance overhead.
- Python 3.12 brings performance improvements and better type hints.
