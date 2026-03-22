"""Synthetic test documents and ground-truth check definitions for internal benchmarks."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field


@dataclass
class GroundTruthCheck:
    """A single verifiable assertion against Memex results."""

    name: str
    description: str
    query: str
    check_type: str  # 'keyword_in_results', 'keyword_absent_from_results',
    #   'entity_exists', 'entity_type_check',
    #   'entity_cooccurrence_check', 'entity_mention_check',
    #   'result_ordering', 'llm_judge'
    expected: str | list[str]
    expected_entity_type: str | None = None  # for entity_type_check
    search_type: str = 'memory'  # 'memory' or 'note'
    strategies: list[str] | None = None
    include_superseded: bool | None = None
    top_k: int = 10
    vault_name: str | None = None  # for multi-vault checks
    max_duration_ms: float | None = None  # timing assertion


@dataclass
class SyntheticDoc:
    """A synthetic document with known facts for benchmarking."""

    filename: str
    title: str
    description: str
    content: str
    tags: list[str] = field(default_factory=list)
    vault_name: str | None = None  # target vault for ingestion
    files: dict[str, bytes] = field(default_factory=dict)  # assets

    @property
    def content_b64(self) -> bytes:
        return base64.b64encode(self.content.encode('utf-8'))

    @property
    def files_b64(self) -> dict[str, bytes]:
        return {k: base64.b64encode(v) for k, v in self.files.items()}


@dataclass
class ScenarioGroup:
    """A group of related docs and checks that test a specific capability."""

    name: str
    description: str
    docs: list[SyntheticDoc]
    checks: list[GroundTruthCheck]
    sequential_ingest: bool = False


# ---------------------------------------------------------------------------
# Group 1: Basic Extraction & Retrieval
# ---------------------------------------------------------------------------

_DOC_ALPHA_KICKOFF = SyntheticDoc(
    filename='project-alpha-kickoff.md',
    title='Project Alpha Kickoff',
    description='Kickoff meeting notes for Project Alpha at Acme Corp.',
    tags=['project', 'kickoff', 'acme-corp'],
    content="""\
# Project Alpha Kickoff

**Date:** March 15, 2025
**Lead:** Sarah Chen
**Organization:** Acme Corp
**Status:** Active

## Overview

Project Alpha is a new initiative at Acme Corp to build a next-generation data platform.
The project will use Python 3.12 and PostgreSQL 16 as core technologies. Sarah Chen has been
appointed as the project lead, reporting directly to the CTO.

## Goals

1. Build a unified data ingestion pipeline capable of processing 10,000 events per second.
2. Implement a real-time analytics dashboard using React and D3.js.
3. Deliver Phase 1 (API layer + data pipeline) by June 2025.

## Team

- Sarah Chen (Project Lead)
- David Park (Backend Engineer)
- Maria Santos (Data Engineer)
- James Liu (Frontend Engineer)

## Tech Stack

- Language: Python 3.12
- Database: PostgreSQL 16
- Message Queue: Apache Kafka
- Frontend: React + TypeScript
""",
)

_DOC_ALPHA_UPDATE = SyntheticDoc(
    filename='project-alpha-update.md',
    title='Project Alpha Phase 1 Update',
    description='Status update on Project Alpha Phase 1 completion.',
    tags=['project', 'update', 'acme-corp'],
    content="""\
# Project Alpha — Phase 1 Update

**Date:** June 20, 2025
**Author:** Sarah Chen

## Summary

Phase 1 of Project Alpha has been completed successfully. The API layer and data pipeline
are now operational in the staging environment.

## Key Results

- API layer delivered on schedule with 47 endpoints.
- Data pipeline processing 12,000 events per second — 20% above the original target of 10,000.
- All integration tests passing with 94% code coverage.
- PostgreSQL 16 cluster running with 3 replicas for high availability.

## Next Steps

- Phase 2 planning begins July 2025.
- Focus on the real-time analytics dashboard (React + D3.js).
- Sarah Chen will present results to the Acme Corp board on July 10, 2025.
""",
)

_DOC_BETA_OVERVIEW = SyntheticDoc(
    filename='project-beta-overview.md',
    title='Project Beta Overview',
    description='Overview of Project Beta ML initiative at Acme Corp.',
    tags=['project', 'ml', 'acme-corp'],
    content="""\
# Project Beta Overview

**Date:** August 1, 2025
**Lead:** Marcus Rivera
**Organization:** Acme Corp

## Overview

Project Beta is Acme Corp's machine learning initiative focused on building predictive
analytics capabilities. The project leverages PyTorch for model training and DuckDB for
fast analytical queries on feature data.

## Objectives

1. Train recommendation models on user behavior data.
2. Build a feature store using DuckDB for sub-second query latency.
3. Deploy models via a FastAPI serving layer with GPU support.

## Team

- Marcus Rivera (ML Lead)
- Priya Patel (ML Engineer)
- Tom Wilson (Data Platform Engineer)

## Tech Stack

- Framework: PyTorch 2.1
- Feature Store: DuckDB
- Serving: FastAPI + NVIDIA Triton
- Orchestration: Apache Airflow
""",
)

GROUP_BASIC = ScenarioGroup(
    name='basic_extraction',
    description='Tests basic fact extraction, keyword search, and entity linking.',
    docs=[_DOC_ALPHA_KICKOFF, _DOC_ALPHA_UPDATE, _DOC_BETA_OVERVIEW],
    checks=[
        GroundTruthCheck(
            name='search_project_alpha',
            description='Searching "Project Alpha" returns both Alpha docs.',
            query='Project Alpha',
            check_type='keyword_in_results',
            expected=['Sarah Chen', 'Phase 1'],
        ),
        GroundTruthCheck(
            name='who_leads_alpha',
            description='Query about Alpha leadership returns Sarah Chen.',
            query='Who leads Project Alpha?',
            check_type='keyword_in_results',
            expected=['Sarah Chen'],
        ),
        GroundTruthCheck(
            name='entity_acme_corp',
            description='Entity "Acme Corp" exists and links to multiple projects.',
            query='Acme Corp',
            check_type='entity_exists',
            expected=['Acme Corp'],
        ),
        GroundTruthCheck(
            name='keyword_postgresql',
            description='Keyword search for PostgreSQL finds relevant results.',
            query='PostgreSQL 16',
            check_type='keyword_in_results',
            expected=['PostgreSQL'],
            strategies=['keyword'],
        ),
        GroundTruthCheck(
            name='semantic_data_platform',
            description='Semantic search for "data platform" surfaces Project Alpha.',
            query='building a data platform for event processing',
            check_type='keyword_in_results',
            expected=['Project Alpha'],
            strategies=['semantic'],
        ),
        GroundTruthCheck(
            name='note_search_alpha',
            description='Note search returns the Alpha kickoff document.',
            query='Project Alpha kickoff meeting',
            check_type='keyword_in_results',
            expected=['Project Alpha'],
            search_type='note',
        ),
        GroundTruthCheck(
            name='sarah_chen_entity_type',
            description='Sarah Chen is classified as a Person entity.',
            query='Sarah Chen',
            check_type='entity_type_check',
            expected='Sarah Chen',
            expected_entity_type='Person',
        ),
        GroundTruthCheck(
            name='acme_corp_entity_type',
            description='Acme Corp is classified as an Organization entity.',
            query='Acme Corp',
            check_type='entity_type_check',
            expected='Acme Corp',
            expected_entity_type='Organization',
        ),
    ],
)

# ---------------------------------------------------------------------------
# Group 2: Contradiction & Update
# ---------------------------------------------------------------------------

_DOC_TECH_STACK_V1 = SyntheticDoc(
    filename='tech-stack-v1.md',
    title='Project Nexus Tech Stack (January 2025)',
    description='Project Nexus technology stack as of January 2025.',
    tags=['tech-stack', 'project-nexus', 'infrastructure'],
    content="""\
# Project Nexus — Tech Stack (January 2025)

## Current Stack

Project Nexus uses the following technologies:

- **Language:** Python 3.11
- **Web Framework:** Django 4.2
- **Database:** MySQL 8.0
- **CI/CD:** Jenkins (self-hosted)
- **Deployment:** Docker + Kubernetes on AWS EKS
- **Monitoring:** Datadog

## Notes

- Django was selected for its ORM and admin interface.
- MySQL 8.0 was chosen for its JSON support and replication capabilities.
- Jenkins pipelines are maintained by the DevOps team.
""",
)

_DOC_TECH_STACK_V2 = SyntheticDoc(
    filename='tech-stack-v2.md',
    title='Project Nexus Tech Stack (July 2025)',
    description='Project Nexus technology stack after migration in July 2025.',
    tags=['tech-stack', 'project-nexus', 'infrastructure', 'migration'],
    content="""\
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
""",
)

GROUP_CONTRADICTION = ScenarioGroup(
    name='contradiction',
    description='Tests contradiction detection and supersession handling.',
    docs=[_DOC_TECH_STACK_V1, _DOC_TECH_STACK_V2],
    sequential_ingest=True,
    checks=[
        GroundTruthCheck(
            name='latest_python_version',
            description='Query about Python version should rank 3.12 above 3.11.',
            query='What Python version does Project Nexus use?',
            check_type='result_ordering',
            expected=['Python 3.12', 'Python 3.11'],
        ),
        GroundTruthCheck(
            name='current_framework',
            description='Current web framework should be FastAPI.',
            query='What web framework does Project Nexus use?',
            check_type='keyword_in_results',
            expected=['FastAPI'],
        ),
        GroundTruthCheck(
            name='current_database',
            description='Current database should be PostgreSQL.',
            query='What database does Project Nexus use?',
            check_type='keyword_in_results',
            expected=['PostgreSQL 16'],
        ),
        GroundTruthCheck(
            name='superseded_filtered',
            description='With include_superseded=False, old facts should be downranked.',
            query='What CI/CD system does Project Nexus use?',
            check_type='keyword_in_results',
            expected=['GitHub Actions'],
            include_superseded=False,
        ),
        GroundTruthCheck(
            name='superseded_included',
            description='With include_superseded=True, both old and new CI/CD facts appear.',
            query='What CI/CD system does Project Nexus use?',
            check_type='keyword_in_results',
            expected=['GitHub Actions', 'Jenkins'],
            include_superseded=True,
        ),
        GroundTruthCheck(
            name='llm_judge_migration',
            description='LLM judge: does the response correctly describe the migration?',
            query='Describe the Project Nexus tech stack migration that happened in 2025.',
            check_type='llm_judge',
            expected='Project Nexus migrated from Django to FastAPI, MySQL to PostgreSQL, '
            'Jenkins to GitHub Actions, and Python 3.11 to 3.12 in Q2 2025.',
        ),
    ],
)

# ---------------------------------------------------------------------------
# Group 3: Entity Resolution & Graph
# ---------------------------------------------------------------------------

_DOC_TEAM_MEETING = SyntheticDoc(
    filename='team-meeting-notes.md',
    title='AI Research Lab Team Meeting',
    description='Weekly meeting notes from the AI Research Lab.',
    tags=['meeting', 'ai-research', 'nlp'],
    content="""\
# AI Research Lab — Weekly Meeting

**Date:** September 5, 2025
**Attendees:** Dr. Elena Vasquez, Dr. Raj Mehta, Lisa Chang

## Updates

### NLP Pipeline (Dr. Elena Vasquez)
Dr. Elena Vasquez presented progress on the NLP pipeline. The new transformer-based
tokenizer has reduced preprocessing time by 40%. She is collaborating with the
Data Engineering team to integrate the pipeline with the production data lake.

### Embedding Model (Dr. Raj Mehta)
Dr. Raj Mehta demonstrated the latest embedding model benchmarks. The model achieves
state-of-the-art performance on STS-B with a cosine similarity score of 0.891.

### Infrastructure (Lisa Chang)
Lisa Chang reported that GPU cluster utilization is at 78%. She recommends adding
4 more A100 GPUs to support the upcoming training runs.

## Action Items

- Dr. Elena Vasquez: Complete NLP pipeline integration by September 20.
- Dr. Raj Mehta: Prepare embedding model for production deployment.
- Lisa Chang: Submit GPU procurement request.
""",
)

_DOC_CONFERENCE_TALK = SyntheticDoc(
    filename='conference-talk.md',
    title='NeurIPS 2025 Keynote by Elena Vasquez',
    description='Summary of Elena Vasquez keynote talk at NeurIPS 2025.',
    tags=['conference', 'neurips', 'ai-research', 'nlp'],
    content="""\
# NeurIPS 2025 — Keynote Summary

**Speaker:** Elena Vasquez (AI Research Lab)
**Title:** "Scaling Transformer Architectures for Real-World NLP"

## Talk Summary

Elena Vasquez from the AI Research Lab delivered a keynote on scaling transformer
architectures for production NLP systems. She discussed how her team's NLP pipeline
processes over 1 million documents per day using a novel chunking strategy.

## Key Points

1. Traditional attention mechanisms don't scale beyond 16K tokens efficiently.
2. Their proposed "sliding window + global tokens" approach maintains quality at 128K context.
3. The NLP pipeline at AI Research Lab uses this architecture in production.
4. Collaboration with Data Engineering was critical to achieving production-grade throughput.

## Q&A Highlights

- Asked about embedding models, she referenced her colleague Dr. Raj Mehta's work
  on sentence embeddings as complementary to the pipeline.
- She emphasized that the AI Research Lab is open-sourcing the chunking component.
""",
)

GROUP_ENTITY_RESOLUTION = ScenarioGroup(
    name='entity_resolution',
    description='Tests entity resolution across name variants and graph co-occurrence.',
    docs=[_DOC_TEAM_MEETING, _DOC_CONFERENCE_TALK],
    checks=[
        GroundTruthCheck(
            name='elena_entity_resolution',
            description='Name variants "Dr. Elena Vasquez" and "Elena Vasquez" resolve to same entity.',
            query='Elena Vasquez',
            check_type='entity_exists',
            expected=['Elena Vasquez'],
        ),
        GroundTruthCheck(
            name='graph_cooccurrence',
            description='Graph query for Elena Vasquez surfaces related entities.',
            query='Elena Vasquez NLP pipeline',
            check_type='keyword_in_results',
            expected=['NLP pipeline'],
            strategies=['graph'],
        ),
        GroundTruthCheck(
            name='cross_doc_facts',
            description='Search returns facts from both meeting notes and conference talk.',
            query='What does Elena Vasquez work on?',
            check_type='keyword_in_results',
            expected=['transformer', 'NLP'],
        ),
        GroundTruthCheck(
            name='ai_research_lab_entity',
            description='AI Research Lab exists as an entity.',
            query='AI Research Lab',
            check_type='entity_exists',
            expected=['AI Research Lab'],
        ),
        GroundTruthCheck(
            name='elena_entity_type',
            description='Elena Vasquez is classified as a Person entity.',
            query='Elena Vasquez',
            check_type='entity_type_check',
            expected='Elena Vasquez',
            expected_entity_type='Person',
        ),
        GroundTruthCheck(
            name='ai_research_lab_entity_type',
            description='AI Research Lab is classified as an Organization entity.',
            query='AI Research Lab',
            check_type='entity_type_check',
            expected='AI Research Lab',
            expected_entity_type='Organization',
        ),
        GroundTruthCheck(
            name='elena_cooccurs_with_raj',
            description='Elena Vasquez co-occurs with Raj Mehta.',
            query='Elena Vasquez',
            check_type='entity_cooccurrence_check',
            expected=['Raj Mehta'],
        ),
        GroundTruthCheck(
            name='elena_cooccurs_with_ai_lab',
            description='Elena Vasquez co-occurs with AI Research Lab.',
            query='Elena Vasquez',
            check_type='entity_cooccurrence_check',
            expected=['AI Research Lab'],
        ),
        GroundTruthCheck(
            name='elena_mentions_nlp',
            description='Elena Vasquez mentions reference NLP.',
            query='Elena Vasquez',
            check_type='entity_mention_check',
            expected=['NLP'],
        ),
        GroundTruthCheck(
            name='multi_hop_gpu_infrastructure',
            description='Multi-hop: Elena → AI Research Lab → GPU infrastructure → Lisa Chang.',
            query='Who manages the GPU infrastructure that supports the NLP pipeline?',
            check_type='keyword_in_results',
            expected=['Lisa Chang', 'GPU'],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Group 4: Reflection & Mental Models
# ---------------------------------------------------------------------------

_DOC_SARAH_PROFILE = SyntheticDoc(
    filename='sarah-chen-profile.md',
    title='Sarah Chen Professional Profile',
    description='Professional profile of Sarah Chen, engineering leader at Acme Corp.',
    tags=['profile', 'leadership', 'acme-corp'],
    content="""\
# Sarah Chen — Professional Profile

**Role:** Senior Engineering Leader, Acme Corp
**Experience:** 12 years in software engineering and technical leadership

## Background

Sarah Chen is a senior engineering leader at Acme Corp with 12 years of experience
in building large-scale distributed systems. She led Project Alpha from inception to
successful Phase 1 delivery in Q2 2025.

## Leadership Style

Known for her data-driven decision making and ability to align cross-functional teams.
She organizes teams into focused pods with weekly cross-pod syncs. She reports directly
to the CTO and presents results to the Acme Corp board quarterly.

## Key Achievements

- Delivered Project Alpha Phase 1 on schedule (API layer + data pipeline)
- Pipeline exceeded targets: 12,000 events/sec vs 10,000 target
- Achieved 94% code coverage on integration tests
- Selected PostgreSQL 16 over MySQL for pgvector support
""",
)

_DOC_SARAH_TECH = SyntheticDoc(
    filename='sarah-chen-tech-decisions.md',
    title='Sarah Chen Technical Decisions Log',
    description='Technical decision log for Sarah Chen on Project Alpha.',
    tags=['decisions', 'architecture', 'acme-corp'],
    content="""\
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
""",
)

GROUP_REFLECTION = ScenarioGroup(
    name='reflection',
    description='Tests reflection produces mental models and mental_model retrieval strategy.',
    docs=[_DOC_SARAH_PROFILE, _DOC_SARAH_TECH],
    checks=[
        GroundTruthCheck(
            name='reflection_mental_model',
            description='Reflection on a key entity produces observations with evidence.',
            query='Sarah Chen',
            check_type='llm_judge',
            expected='Mental model should reference Project Alpha, her role as project lead, '
            'and Phase 1 completion.',
        ),
        GroundTruthCheck(
            name='mental_model_strategy',
            description='Mental model retrieval strategy returns results for reflected entity.',
            query='Sarah Chen leadership',
            check_type='keyword_in_results',
            expected=['Sarah Chen'],
            strategies=['mental_model'],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Group 5: Temporal Reasoning
# ---------------------------------------------------------------------------

_DOC_Q1_REVIEW = SyntheticDoc(
    filename='quarterly-review-q1.md',
    title='Quarterly Business Review Q1 2025',
    description='Q1 2025 quarterly business review results.',
    tags=['quarterly-review', 'business', 'q1-2025'],
    content="""\
# Quarterly Business Review — Q1 2025

**Period:** January - March 2025
**Prepared by:** CFO Office

## Financial Highlights

- Revenue grew 15% quarter-over-quarter to $12.5M.
- Operating margin improved to 18% from 15% in Q4 2024.
- R&D spending increased by 25% to support new initiatives.

## Headcount

- 12 new hires across Engineering (8), Product (2), and Sales (2).
- Total headcount: 156 employees.

## Product Milestones

- Project Alpha launched on March 15, 2025.
- Mobile app v2.0 released with 4.7-star rating.
- Customer NPS score: 72 (up from 65).
""",
)

_DOC_Q2_REVIEW = SyntheticDoc(
    filename='quarterly-review-q2.md',
    title='Quarterly Business Review Q2 2025',
    description='Q2 2025 quarterly business review results.',
    tags=['quarterly-review', 'business', 'q2-2025'],
    content="""\
# Quarterly Business Review — Q2 2025

**Period:** April - June 2025
**Prepared by:** CFO Office

## Financial Highlights

- Revenue grew 22% quarter-over-quarter to $15.3M.
- Operating margin held steady at 18%.
- Customer acquisition cost decreased 10% due to product-led growth.

## Headcount

- 8 new hires across Engineering (5), ML (2), and Design (1).
- Total headcount: 164 employees.

## Product Milestones

- Project Alpha Phase 1 completed on June 20, 2025.
- Project Beta (ML initiative) kicked off in May 2025.
- Data pipeline processing 12,000 events/second (target was 10,000).
""",
)

GROUP_TEMPORAL = ScenarioGroup(
    name='temporal',
    description='Tests temporal filtering and recency-aware retrieval.',
    docs=[_DOC_Q1_REVIEW, _DOC_Q2_REVIEW],
    checks=[
        GroundTruthCheck(
            name='temporal_q2_revenue',
            description='Query about latest revenue returns Q2 figure ($15.3M).',
            query='What is the most recent quarterly revenue?',
            check_type='keyword_in_results',
            expected=['15.3'],
        ),
        GroundTruthCheck(
            name='temporal_headcount',
            description='Latest headcount is 164.',
            query='How many employees does the company have?',
            check_type='keyword_in_results',
            expected=['164'],
        ),
        GroundTruthCheck(
            name='temporal_recency',
            description='Recency ranking puts Q2 results above Q1.',
            query='quarterly business review results',
            check_type='result_ordering',
            expected=['Q2 2025', 'Q1 2025'],
            strategies=['temporal'],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Group 6: Vault Isolation
# ---------------------------------------------------------------------------

_DOC_VAULT_A_GAMMA = SyntheticDoc(
    filename='project-gamma.md',
    title='Project Gamma Overview',
    description='Project Gamma uses Elixir and Phoenix for real-time features.',
    tags=['project', 'elixir'],
    vault_name='bench-vault-a',
    content="""\
# Project Gamma — Real-Time Platform

**Lead:** Carla Ruiz
**Organization:** Polaris Labs

## Overview

Project Gamma is a real-time collaboration platform built at Polaris Labs.
The system uses Elixir and the Phoenix framework for WebSocket-based communication.
It supports 50,000 concurrent connections per node using the BEAM virtual machine.

## Tech Stack

- Language: Elixir 1.16
- Framework: Phoenix LiveView
- Database: CockroachDB
- Message Broker: RabbitMQ
""",
)

_DOC_VAULT_B_DELTA = SyntheticDoc(
    filename='project-delta.md',
    title='Project Delta Overview',
    description='Project Delta uses Scala and Akka for distributed processing.',
    tags=['project', 'scala'],
    vault_name='bench-vault-b',
    content="""\
# Project Delta — Distributed Processing Engine

**Lead:** Henrik Johansen
**Organization:** Nordic Data Systems

## Overview

Project Delta is a distributed data processing engine built at Nordic Data Systems.
The system uses Scala and the Akka framework for actor-based concurrency.
It processes 2 billion events per day across a 200-node cluster.

## Tech Stack

- Language: Scala 3.4
- Framework: Akka Cluster + Akka Streams
- Database: Apache Cassandra
- Orchestration: Apache Flink
""",
)

GROUP_VAULT_ISOLATION = ScenarioGroup(
    name='vault_isolation',
    description='Tests that search results respect vault boundaries.',
    docs=[_DOC_VAULT_A_GAMMA, _DOC_VAULT_B_DELTA],
    checks=[
        GroundTruthCheck(
            name='vault_a_contains_gamma',
            description='Vault A search finds Project Gamma.',
            query='real-time platform',
            check_type='keyword_in_results',
            expected=['Elixir'],
            vault_name='bench-vault-a',
        ),
        GroundTruthCheck(
            name='vault_a_excludes_delta',
            description='Vault A search does NOT contain Project Delta content.',
            query='distributed processing engine',
            check_type='keyword_absent_from_results',
            expected=['Scala', 'Akka'],
            vault_name='bench-vault-a',
        ),
        GroundTruthCheck(
            name='vault_b_contains_delta',
            description='Vault B search finds Project Delta.',
            query='distributed processing engine',
            check_type='keyword_in_results',
            expected=['Scala'],
            vault_name='bench-vault-b',
        ),
        GroundTruthCheck(
            name='vault_b_excludes_gamma',
            description='Vault B search does NOT contain Project Gamma content.',
            query='real-time collaboration platform',
            check_type='keyword_absent_from_results',
            expected=['Elixir', 'Phoenix'],
            vault_name='bench-vault-b',
        ),
        GroundTruthCheck(
            name='vault_a_entity_isolation',
            description='Entity "Polaris Labs" exists only in vault A.',
            query='Polaris Labs',
            check_type='entity_exists',
            expected=['Polaris Labs'],
            vault_name='bench-vault-a',
        ),
        GroundTruthCheck(
            name='vault_b_entity_isolation',
            description='Entity "Nordic Data Systems" exists only in vault B.',
            query='Nordic Data Systems',
            check_type='entity_exists',
            expected=['Nordic Data Systems'],
            vault_name='bench-vault-b',
        ),
    ],
)

# ---------------------------------------------------------------------------
# Group 7: Entity Edge Cases
# ---------------------------------------------------------------------------

_DOC_RODRIGUEZ_SYMPOSIUM = SyntheticDoc(
    filename='rodriguez-symposium.md',
    title='IEEE Quantum Symposium 2025 — J. Rodriguez',
    description='Dr. J. Rodriguez presents at IEEE symposium on quantum error correction.',
    tags=['quantum', 'conference', 'research'],
    content="""\
# IEEE Quantum Computing Symposium 2025

**Speaker:** Dr. J. Rodriguez (QuantumTech Labs)
**Topic:** Advances in Topological Quantum Error Correction

## Presentation Summary

Dr. J. Rodriguez, a senior quantum computing researcher at QuantumTech Labs, presented
a breakthrough in topological quantum error correction. Rodriguez's work on topological
qubits has achieved a 99.7% gate fidelity rate, drawing attention from DARPA and NSF.

## Key Results

- Demonstrated 99.7% gate fidelity on a 12-qubit topological processor
- Error correction overhead reduced by 40% compared to surface codes
- Partnership with DARPA to scale to 50 qubits by 2026

## Collaborators

Rodriguez acknowledged his colleague Dr. Amara Osei at QuantumTech Labs for her work
on quantum control systems that enabled the high-fidelity measurements.
""",
)

_DOC_RODRIGUEZ_AWARD = SyntheticDoc(
    filename='rodriguez-award.md',
    title='2025 Quantum Innovation Award — Juan Rodriguez',
    description='Juan Rodriguez receives the Quantum Innovation Award.',
    tags=['quantum', 'award', 'research'],
    content="""\
# 2025 Quantum Innovation Award

**Recipient:** Juan Rodriguez, PhD
**Affiliation:** QuantumTech Labs
**Field:** Quantum Error Correction

## Award Citation

Juan Rodriguez received the 2025 Quantum Innovation Award for his groundbreaking
work at QuantumTech Labs on topological quantum error correction. Rodriguez, who
holds a PhD from MIT, has published 30 papers on quantum error correction and
topological qubits.

## Research Impact

His topological qubit architecture has become the foundation for QuantumTech Labs'
commercial quantum processor roadmap. The 99.7% gate fidelity breakthrough was
highlighted as the year's most significant advance in quantum computing.

## Background

Rodriguez joined QuantumTech Labs in 2019 after completing his PhD at MIT under
Professor Sarah Kim. He leads a team of 8 researchers in the Quantum Hardware division.
""",
)

_DOC_OSEI_PROFILE = SyntheticDoc(
    filename='osei-profile.md',
    title='Dr. Amara Osei — Quantum Control Systems',
    description='Profile of Dr. Amara Osei at QuantumTech Labs.',
    tags=['quantum', 'research', 'profile'],
    content="""\
# Dr. Amara Osei — Research Profile

**Title:** Principal Scientist, Quantum Control Systems
**Affiliation:** QuantumTech Labs
**Specialty:** Quantum measurement and control

## Research

Dr. Amara Osei leads the Quantum Control Systems group at QuantumTech Labs.
Her team developed the high-fidelity measurement protocols used by Juan Rodriguez's
topological qubit experiments. Osei's control systems achieve sub-nanosecond timing
precision critical for maintaining quantum coherence.

## Publications

Amara Osei has co-authored 15 papers with J. Rodriguez on quantum error correction,
including their landmark Nature paper on topological qubit control. She also
collaborates with Professor Kim at MIT on quantum feedback mechanisms.
""",
)

GROUP_ENTITY_EDGE_CASES = ScenarioGroup(
    name='entity_edge_cases',
    description='Tests entity resolution with abbreviated names and title variations.',
    docs=[_DOC_RODRIGUEZ_SYMPOSIUM, _DOC_RODRIGUEZ_AWARD, _DOC_OSEI_PROFILE],
    checks=[
        GroundTruthCheck(
            name='abbreviated_name_resolution',
            description='"J. Rodriguez" and "Juan Rodriguez" resolve to the same entity.',
            query='Juan Rodriguez',
            check_type='entity_exists',
            expected=['Rodriguez'],
        ),
        GroundTruthCheck(
            name='title_variation_resolution',
            description='"Dr. Amara Osei" and "Amara Osei" resolve to the same entity.',
            query='Amara Osei',
            check_type='entity_exists',
            expected=['Osei'],
        ),
        GroundTruthCheck(
            name='cross_doc_entity_cooccurrence',
            description='Rodriguez co-occurs with Amara Osei across documents.',
            query='Rodriguez',
            check_type='entity_cooccurrence_check',
            expected=['Osei'],
        ),
        GroundTruthCheck(
            name='quantumtech_labs_entity',
            description='QuantumTech Labs exists as an organization entity.',
            query='QuantumTech Labs',
            check_type='entity_type_check',
            expected='QuantumTech Labs',
            expected_entity_type='Organization',
        ),
        GroundTruthCheck(
            name='cross_doc_facts_rodriguez',
            description='Search connects facts about Rodriguez across symposium and award docs.',
            query='What did Juan Rodriguez achieve in quantum computing?',
            check_type='keyword_in_results',
            expected=['topological', '99.7%'],
        ),
    ],
)

# ---------------------------------------------------------------------------
# Group 8: Scale Stress
# ---------------------------------------------------------------------------

_SCALE_DEPARTMENTS: list[tuple[str, str, int, str, str]] = [
    (
        'Engineering',
        'Ruby Martinez',
        45,
        'Python, Go, Kubernetes',
        'Building a new CI/CD platform using ArgoCD and Tekton pipelines.',
    ),
    (
        'Marketing',
        'Tom Bradley',
        22,
        'HubSpot, Google Analytics, Figma',
        'Launching a brand refresh campaign targeting 12 international markets.',
    ),
    (
        'Sales',
        'Nina Patel',
        38,
        'Salesforce, Gong, Outreach',
        'Expanding into the Asia-Pacific region with 5 new satellite offices.',
    ),
    (
        'Product',
        "Kevin O'Brien",
        15,
        'Jira, Amplitude, Miro',
        'Redesigning the onboarding flow to reduce first-week churn by 20%.',
    ),
    (
        'Data Science',
        'Mei-Lin Zhao',
        12,
        'Snowflake, dbt, Jupyter',
        'Building a customer lifetime value prediction model using gradient boosting.',
    ),
    (
        'Customer Success',
        'Aisha Johnson',
        28,
        'Zendesk, Gainsight, Slack',
        'Implementing a proactive customer health score system with churn alerts.',
    ),
    (
        'Finance',
        'Roberto Escobar',
        10,
        'NetSuite, Stripe, Looker',
        'Migrating billing from annual to monthly cycles with usage-based pricing.',
    ),
    (
        'Legal',
        'Catherine Wu',
        6,
        'DocuSign, Ironclad, OneTrust',
        'Drafting GDPR and CCPA compliance frameworks for EU and California expansion.',
    ),
    (
        'HR',
        'David Okafor',
        14,
        'Workday, Greenhouse, Culture Amp',
        'Rolling out a new 360-degree performance review framework company-wide.',
    ),
    (
        'Security',
        'Yuki Tanaka',
        8,
        'CrowdStrike, Snyk, HashiCorp Vault',
        'Achieving SOC 2 Type II certification and implementing zero-trust access.',
    ),
    (
        'AI Research',
        'Dr. Alexei Volkov',
        5,
        'PyTorch, Weights & Biases, Ray',
        'Developing a retrieval-augmented generation system for internal knowledge.',
    ),
]


def _build_scale_docs() -> list[SyntheticDoc]:
    docs = []
    for dept, lead, headcount, tools, initiative in _SCALE_DEPARTMENTS:
        slug = dept.lower().replace(' ', '-')
        docs.append(
            SyntheticDoc(
                filename=f'dept-{slug}.md',
                title=f'{dept} Department Overview',
                description=f'Overview of the {dept} department at TechCo Global.',
                tags=['department', slug, 'techco'],
                content=f"""\
# {dept} Department — TechCo Global

**Department Head:** {lead}
**Headcount:** {headcount} employees
**Core Tools:** {tools}

## Current Initiative

{initiative}

## Responsibilities

The {dept} department at TechCo Global is responsible for all {dept.lower()}-related
activities. The team of {headcount} reports to {lead}, who is the current head of
the department.
""",
            )
        )
    return docs


_SCALE_DOCS = _build_scale_docs()

# Historical leadership doc for temporal range testing
_DOC_ENGINEERING_PREDECESSOR = SyntheticDoc(
    filename='dept-engineering-history.md',
    title='Engineering Department Historical Leadership',
    description='Former leadership of the Engineering department at TechCo Global.',
    tags=['department', 'engineering', 'techco', 'historical'],
    content="""\
# Engineering Department — TechCo Global (Historical Leadership)

**Former Department Head:** Alex Chen
**Tenure:** January 2020 to December 2023

Alex Chen led the Engineering Department at TechCo Global from 2020 to 2023,
overseeing the initial cloud migration and establishing the team's core practices.
Alex built the department from 12 to 35 engineers before handing over to Ruby Martinez.
""",
)

GROUP_SCALE = ScenarioGroup(
    name='scale',
    description='Tests retrieval quality with many documents and specific fact lookup.',
    sequential_ingest=True,
    docs=[*_SCALE_DOCS, _DOC_ENGINEERING_PREDECESSOR],
    checks=[
        GroundTruthCheck(
            name='scale_find_engineering_lead',
            description='Find the Engineering department lead among 10 departments.',
            query='Who leads the Engineering department at TechCo Global?',
            check_type='keyword_in_results',
            expected=['Ruby Martinez'],
        ),
        GroundTruthCheck(
            name='scale_find_security_tools',
            description='Find Security department tools among 10 departments.',
            query='What tools does the Security team at TechCo use?',
            check_type='keyword_in_results',
            expected=['CrowdStrike', 'Snyk'],
        ),
        GroundTruthCheck(
            name='scale_find_ai_initiative',
            description='Find the AI Research initiative among 10 departments.',
            query='What is the AI Research department working on at TechCo?',
            check_type='keyword_in_results',
            expected=['retrieval-augmented generation'],
        ),
        GroundTruthCheck(
            name='scale_specific_headcount',
            description='Retrieve a specific headcount among 10 departments.',
            query='How many people are in the Legal department at TechCo Global?',
            check_type='keyword_in_results',
            expected=['6'],
        ),
        GroundTruthCheck(
            name='scale_entity_exists',
            description='Entity for a department lead is created from scale docs.',
            query='Mei-Lin Zhao',
            check_type='entity_exists',
            expected=['Mei-Lin Zhao'],
        ),
        GroundTruthCheck(
            name='scale_retrieval_speed',
            description='Retrieval completes within 30 seconds even with many documents.',
            query='TechCo Global department overview',
            check_type='keyword_in_results',
            expected=['TechCo'],
            max_duration_ms=30000,
        ),
        GroundTruthCheck(
            name='scale_current_vs_former_lead',
            description='Current head (ongoing) should rank above former head (ended tenure).',
            query='Who leads the Engineering department at TechCo Global?',
            check_type='result_ordering',
            expected=['Ruby Martinez', 'Alex Chen'],
            top_k=30,
        ),
        GroundTruthCheck(
            name='scale_former_lead_query',
            description='Query about former head should surface the predecessor.',
            query='Who headed the Engineering department at TechCo Global before Ruby Martinez?',
            check_type='keyword_in_results',
            expected=['Alex Chen'],
            top_k=20,
        ),
    ],
)

# ---------------------------------------------------------------------------
# Group 9: Asset Ingestion
# ---------------------------------------------------------------------------

_DOC_WITH_ASSET = SyntheticDoc(
    filename='architecture-overview.md',
    title='System Architecture Overview',
    description='Architecture document with a system diagram asset.',
    tags=['architecture', 'diagram', 'infrastructure'],
    files={
        'system-diagram.png': b'\x89PNG\r\n\x1a\n' + b'\x00' * 8 + b'fake-benchmark-image-data',
    },
    content="""\
# System Architecture Overview

![System Diagram](system-diagram.png)

## Microservices Architecture

The platform uses a microservices architecture with 5 core services:

1. **API Gateway** — Routes requests using Kong with rate limiting and auth.
2. **User Service** — Manages authentication via OAuth2 and OIDC.
3. **Order Service** — Processes orders with CQRS and event sourcing patterns.
4. **Notification Service** — Sends emails and push notifications via AWS SES and FCM.
5. **Analytics Service** — Collects telemetry using OpenTelemetry and exports to Grafana.

## Communication

Services communicate via gRPC for synchronous calls and Apache Kafka for async events.
The event schema registry uses Protobuf for type-safe message contracts.

## Deployment

All services are containerized with Docker and deployed on Kubernetes (EKS).
Infrastructure is managed with Terraform and monitored with Prometheus + Grafana.
""",
)

GROUP_ASSETS = ScenarioGroup(
    name='assets',
    description='Tests that notes with file assets are ingested and searchable.',
    docs=[_DOC_WITH_ASSET],
    checks=[
        GroundTruthCheck(
            name='asset_note_searchable',
            description='Note with asset is ingested and content is searchable.',
            query='microservices architecture API Gateway',
            check_type='keyword_in_results',
            expected=['Kong', 'rate limiting'],
        ),
        GroundTruthCheck(
            name='asset_note_search',
            description='Note search finds the architecture document.',
            query='system architecture diagram',
            check_type='keyword_in_results',
            expected=['Architecture'],
            search_type='note',
        ),
    ],
)

# ---------------------------------------------------------------------------
# All scenario groups
# ---------------------------------------------------------------------------

ALL_GROUPS: list[ScenarioGroup] = [
    GROUP_BASIC,
    GROUP_CONTRADICTION,
    GROUP_ENTITY_RESOLUTION,
    GROUP_REFLECTION,
    GROUP_TEMPORAL,
    GROUP_VAULT_ISOLATION,
    GROUP_ENTITY_EDGE_CASES,
    GROUP_SCALE,
    GROUP_ASSETS,
]


def get_group(name: str) -> ScenarioGroup | None:
    """Get a scenario group by name."""
    for group in ALL_GROUPS:
        if group.name == name:
            return group
    return None
