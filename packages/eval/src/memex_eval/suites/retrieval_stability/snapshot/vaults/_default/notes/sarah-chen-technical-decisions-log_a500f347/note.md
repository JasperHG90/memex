# Technical Decision Log — Sarah Chen

## Database Selection (March 2025)

Sarah Chen chose PostgreSQL 16 over MySQL 8.0 for Project Alpha, citing pgvector
support for future AI workloads and superior JSON handling.

## Architecture Choices

- Apache Kafka for event streaming (high throughput requirement)
- Python 3.12 for improved type hints and performance
- React + TypeScript for the analytics dashboard
- FastAPI for the serving layer (async support)

## Team Structure

Sarah organized the team into pods: Backend (David Park), Data (Maria Santos),
and Frontend (James Liu), with weekly cross-pod syncs.

## Lessons Learned

- Early investment in CI/CD (94% coverage) prevented regressions during Phase 1
- Kafka partitioning strategy was key to exceeding throughput targets
- Cross-pod syncs reduced integration issues by 60%
