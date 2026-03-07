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
    check_type: str  # 'keyword_in_results', 'entity_exists', 'result_ordering', 'llm_judge'
    expected: str | list[str]
    search_type: str = 'memory'  # 'memory' or 'note'
    strategies: list[str] | None = None
    include_superseded: bool | None = None
    top_k: int = 10


@dataclass
class SyntheticDoc:
    """A synthetic document with known facts for benchmarking."""

    filename: str
    title: str
    description: str
    content: str
    tags: list[str] = field(default_factory=list)

    @property
    def content_b64(self) -> bytes:
        return base64.b64encode(self.content.encode('utf-8'))


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
    ],
)

# ---------------------------------------------------------------------------
# Group 2: Contradiction & Update
# ---------------------------------------------------------------------------

_DOC_TECH_STACK_V1 = SyntheticDoc(
    filename='tech-stack-v1.md',
    title='Engineering Tech Stack (January 2025)',
    description='Current engineering technology stack as of January 2025.',
    tags=['tech-stack', 'engineering', 'infrastructure'],
    content="""\
# Engineering Tech Stack — January 2025

## Current Stack

Our engineering team uses the following technologies:

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
    title='Engineering Tech Stack (July 2025)',
    description='Updated engineering technology stack after migration in July 2025.',
    tags=['tech-stack', 'engineering', 'infrastructure', 'migration'],
    content="""\
# Engineering Tech Stack — July 2025

## Updated Stack

Following the Q2 2025 migration initiative, our stack has been updated:

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
            query='What Python version does the engineering team use?',
            check_type='result_ordering',
            expected=['Python 3.12', 'Python 3.11'],
        ),
        GroundTruthCheck(
            name='current_framework',
            description='Current web framework should be FastAPI.',
            query='What web framework does the team use?',
            check_type='keyword_in_results',
            expected=['FastAPI'],
        ),
        GroundTruthCheck(
            name='current_database',
            description='Current database should be PostgreSQL.',
            query='What database does the engineering team use?',
            check_type='keyword_in_results',
            expected=['PostgreSQL 16'],
        ),
        GroundTruthCheck(
            name='superseded_filtered',
            description='With include_superseded=False, old facts should be downranked.',
            query='What CI/CD system is used?',
            check_type='keyword_in_results',
            expected=['GitHub Actions'],
            include_superseded=False,
        ),
        GroundTruthCheck(
            name='llm_judge_migration',
            description='LLM judge: does the response correctly describe the migration?',
            query='Describe the tech stack migration that happened in 2025.',
            check_type='llm_judge',
            expected='The team migrated from Django to FastAPI, MySQL to PostgreSQL, '
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
    ],
)

# ---------------------------------------------------------------------------
# Group 4: Reflection (uses entities from Groups 1-3)
# ---------------------------------------------------------------------------

GROUP_REFLECTION = ScenarioGroup(
    name='reflection',
    description='Tests reflection produces mental models with evidence from source docs.',
    docs=[],  # No new docs — uses existing entities
    checks=[
        GroundTruthCheck(
            name='reflection_mental_model',
            description='Reflection on a key entity produces observations with evidence.',
            query='Sarah Chen',
            check_type='llm_judge',
            expected='Mental model should reference Project Alpha, her role as project lead, '
            'and Phase 1 completion.',
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
# All scenario groups
# ---------------------------------------------------------------------------

ALL_GROUPS: list[ScenarioGroup] = [
    GROUP_BASIC,
    GROUP_CONTRADICTION,
    GROUP_ENTITY_RESOLUTION,
    GROUP_REFLECTION,
    GROUP_TEMPORAL,
]


def get_group(name: str) -> ScenarioGroup | None:
    """Get a scenario group by name."""
    for group in ALL_GROUPS:
        if group.name == name:
            return group
    return None
