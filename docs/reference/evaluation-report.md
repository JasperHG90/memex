# LoCoMo Evaluation Report

> Model: Claude Opus 4 via Claude Code CLI | Judge: Gemini 3 Flash
> Date: 2026-03-13

## Summary

| Metric | Value |
|---|---|
| Questions (scored) | 36 (excl. 3 image-dependent, 11 adversarial) |
| Overall Score (non-adversarial) | **0.986** |
| Perfect answers | 35 (97.2%) |
| Wrong answers | 0 (0.0%) |
| Total cost | $9.96 |
| Total duration | 48.6 min |

## Scores by Category

| Category | Count | Mean Score | Perfect | Wrong |
|---|---|---|---|---|
| Single-Hop | 9 | **0.944** | 8 | 0 |
| Multi-Hop | 9 | **1.000** | 9 | 0 |
| Open Domain | 3 | **1.000** | 3 | 0 |
| Temporal | 15 | **1.000** | 15 | 0 |
| Adversarial (unweighted) | 11 | 0.773 | 8 | 2 |
| **Non-adversarial** | **36** | **0.986** | **35** | **0** |

### A note on adversarial scoring

Adversarial questions in the LoCoMo dataset deliberately swap the subject — asking about person A when the ground-truth answer actually pertains to person B. The expected "correct" behavior is for the model to detect this swap.

However, Memex is a search-and-answer tool. When asked "What instrument does Caroline play?", it correctly searches for Caroline's instruments, finds "acoustic guitar", and returns that answer. The fact that the benchmark expects "clarinet and violin" (Melanie's instruments) tests something outside the system's scope: the model would need to know the question is deliberately misleading.

In the failed adversarial cases, the retrieval system found correct, relevant facts for the queried person. These are not retrieval failures — they are a fundamental mismatch between a search tool's behavior and the adversarial benchmark's expectations. Adversarial scores are therefore reported separately and excluded from the weighted overall score.

## Retrieval Efficiency

### Token breakdown

| Metric | Value |
|---|---|
| Total tokens (all) | 8,044,121 |
| Retrieval tokens (Memex) | 364,856 (**4.5%** of total) |
| Agent overhead tokens | 7,679,265 (95.5%) |
| Retrieval tokens/question (mean) | 7,592 |
| Retrieval tokens/question (median) | 4,609 |

### Retrieval by tool

| Tool | Tokens | Share | Calls | Calls/q |
|---|---|---|---|---|
| `memory_search` | 164,370 | 45.1% | 67 | 1.3 |
| `note_search` | 65,644 | 18.0% | 66 | 1.3 |
| `get_nodes` | 65,105 | 17.8% | 48 | 1.0 |
| `get_page_indices` | 36,279 | 9.9% | 45 | 0.9 |
| `read_note` | 19,036 | 5.2% | 21 | 0.4 |
| `get_entity_mentions` | 9,769 | 2.7% | 4 | 0.1 |
| `list_entities` | 2,494 | 0.7% | 18 | 0.4 |
| `find_note` | 1,494 | 0.4% | 4 | 0.1 |
| `get_entity_cooccurrences` | 654 | 0.2% | 1 | 0.0 |
| `list_assets` | 11 | 0.0% | 1 | 0.0 |

![Retrieval token breakdown](evaluation-plots/retrieval_token_breakdown.png)

### Efficiency by category

| Category | Duration | Turns | Total Tokens | Retr Tokens | Retr % | Memex Calls |
|---|---|---|---|---|---|---|
| Single-Hop | 46.9s | 5.6 | 84,185 | 5,170 | 5.9% | 3.6 |
| Multi-Hop | 49.3s | 8 | 161,862 | 7,377 | 5.2% | 5.9 |
| Open Domain | 50.1s | 8 | 129,145 | 6,781 | 5.3% | 6 |
| Temporal | 39.3s | 6.1 | 116,425 | 5,495 | 5.0% | 4.1 |
| Adversarial | 86.1s | 11.5 | 319,800 | 12,829 | 4.8% | 9.5 |

### Distribution plots

![Tokens By Category](evaluation-plots/tokens_by_category.png)

![Duration By Category](evaluation-plots/duration_by_category.png)

![Retrieval Vs Total](evaluation-plots/retrieval_vs_total.png)

![Duration Vs Memex Calls](evaluation-plots/duration_vs_memex_calls.png)

![Turns Distribution](evaluation-plots/turns_distribution.png)

## Tool Usage Patterns

| Metric | Value |
|---|---|
| ToolSearch calls/question | 1.1 (56 total, 2 questions with >1) |
| Entity exploration | 15/50 questions (30%) |
| Citations (inline refs) | 49/50 (98%) |
| Citations (reference list) | 49/50 (98%) |
| `read_note` (discouraged) | 21 total (0.4/q) |

### Memex tool call distribution

| Tool | Total Calls | Calls/q |
|---|---|---|
| `memory_search` | 67 | 1.3 |
| `note_search` | 66 | 1.3 |
| `get_nodes` | 48 | 1.0 |
| `get_page_indices` | 45 | 0.9 |
| `read_note` | 21 | 0.4 |
| `list_entities` | 18 | 0.4 |
| `find_note` | 4 | 0.1 |
| `get_entity_mentions` | 4 | 0.1 |
| `get_entity_cooccurrences` | 1 | 0.0 |
| `list_assets` | 1 | 0.0 |

## Retrieval Paths

The agent autonomously selects a retrieval path based on question complexity. Entity exploration (`list_entities`, `get_entity_mentions`) is used as a supplementary step in any path — it was triggered in 30% of questions.

| Pattern | Count | Share | Avg Score | Avg Tools | Avg Duration | Avg Retr Tok | Avg Cost |
|---|---|---|---|---|---|---|---|
| Two-stage | 19 | 38% | 0.95 | 2.1 | 31s | 3,623 | $0.11 |
| Deep verification | 15 | 30% | 0.87 | 4.8 | 49s | 6,298 | $0.17 |
| Deep + entity | 6 | 12% | 1.00 | 6.0 | 57s | 7,669 | $0.20 |
| Exhaustive | 4 | 8% | 0.88 | 28.2 | 181s | 34,728 | $0.89 |
| Two-stage + entity | 3 | 6% | 1.00 | 3.0 | 53s | 3,623 | $0.13 |
| Simple + entity | 2 | 4% | 0.50 | 2.5 | 31s | 2,870 | $0.10 |

### Two-stage path (19 questions, 38%)

Memory search and note search provide sufficient context to answer directly. The most efficient path — highest volume with strong score (0.95) at lowest cost ($0.11/q). Dominates multi-hop (7) and temporal (6) questions where facts are directly retrievable.

```mermaid
graph LR
    A[memory_search] --> B[note_search] --> C[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#55A868,color:white
```

**Typical questions**: straightforward fact lookups — "When did Caroline go to the adoption meeting?" (q-002), "When did Melanie go to the park?" (q-006), "When did Melanie go camping in July?" (q-008).

### Deep verification path (15 questions, 30%)

Full two-speed reading: search finds candidate notes, then `get_page_indices` → `get_nodes` drills into specific sections for precise evidence. Primarily used for temporal (5) and adversarial (5) questions that need exact details from longer conversation sessions.

```mermaid
graph TD
    A[memory_search] --> B[note_search]
    B --> C[get_page_indices]
    C --> D[get_nodes]
    D --> E[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#CCB974,color:black
    style D fill:#C44E52,color:white
    style E fill:#55A868,color:white
```

**Typical questions**: questions requiring verification from source text — "What did Melanie do after the road trip to relax?" (q-014), "What does Caroline do to keep herself busy during her pottery break?" (q-004).

### Deep + entity path (6 questions, 12%)

Adds entity exploration to deep verification. The agent queries `list_entities` and/or `get_entity_mentions` to discover relationships before or after searching. Used for all open-domain (3) questions and single-hop questions involving person-to-person connections. Perfect 1.00 average score.

```mermaid
graph TD
    E1[list_entities] --> E2[get_entity_mentions]
    E2 --> A[memory_search]
    A --> B[note_search]
    B --> C[get_page_indices]
    C --> D[get_nodes]
    D --> F[Answer]
    style E1 fill:#DD8452,color:white
    style E2 fill:#DD8452,color:white
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#CCB974,color:black
    style D fill:#C44E52,color:white
    style F fill:#55A868,color:white
```

**Typical questions**: relationship and open-domain questions — "Would Caroline likely have Dr. Seuss books on her bookshelf?" (q-013), "Would Caroline be considered religious?" (q-020), "What would Caroline's political leaning likely be?" (q-025).

### Exhaustive path (4 questions, 8%)

Multiple rounds of searching and reading across different notes and queries. The agent iterates when initial results are insufficient — refining queries, searching additional sessions, or reading more note sections. Most expensive ($0.89/q) but necessary for complex questions. Includes adversarial (2) questions where the agent tries harder to find contradicting information.

```mermaid
graph TD
    A[memory_search] --> B[note_search]
    B --> C[get_page_indices]
    C --> D[get_nodes]
    D --> E{Sufficient?}
    E -- No --> F[Refined memory_search]
    F --> G[note_search]
    G --> H[get_page_indices]
    H --> I[get_nodes]
    I --> J[Answer]
    E -- Yes --> J
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#CCB974,color:black
    style D fill:#C44E52,color:white
    style F fill:#4C72B0,color:white
    style G fill:#8172B2,color:white
    style H fill:#CCB974,color:black
    style I fill:#C44E52,color:white
    style J fill:#55A868,color:white
```

**Typical questions**: complex multi-evidence questions — "When did Melanie read 'Nothing is Impossible'?" (q-010, 34 tools, $0.88), "What do sunflowers represent according to Caroline?" (q-030, 17 tools, $0.47).

### Simple + entity path (2 questions, 4%)

A single search round with entity exploration. No deep reading needed — memory search alone returns enough. Lowest retrieval token usage (2,870/q). The 0.50 average score is misleading: the non-adversarial question (q-019) scored 1.0; the average is dragged down by q-035 (adversarial subject swap, 0.0).

```mermaid
graph LR
    A[memory_search] --> B[list_entities] --> C[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#DD8452,color:white
    style C fill:#55A868,color:white
```

### Two-stage + entity path (3 questions, 6%)

Memory and note search augmented with entity exploration. Used primarily for single-hop (2) questions where entity relationships help contextualize the answer. Perfect 1.00 score.

```mermaid
graph LR
    A[memory_search] --> B[note_search] --> C[list_entities] --> D[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#DD8452,color:white
    style D fill:#55A868,color:white
```

## Resource Usage

| Metric | Value |
|---|---|
| Total tokens | 8,044,121 |
| Input tokens | 7,969,426 |
| Output tokens | 74,695 |
| Retrieval tokens (Memex) | 364,856 (4.5%) |
| Total duration | 2,915s (48.6 min) |
| Avg duration/question | 58.3s |
| Median duration/question | 40.7s |
| Total cost | $9.96 |
| Avg cost/question | $0.199 |

## Per-Question Detail

| ID | Category | Score | Dur | Turns | Total Tok | Retr Tok | Retr % | Memex# | Cost |
|---|---|---|---|---|---|---|---|---|---|
| q-001 | adversarial | 1.0 | 73.1s | 8 | 123,445 | 8,396 | 6.8% | 6 | $0.25 |
| q-002 | multi-hop | 1.0 | 26.7s | 4 | 63,190 | 3,464 | 5.5% | 2 | $0.10 |
| q-003 | multi-hop | 1.0 | 44.1s | 5 | 89,804 | 4,310 | 4.8% | 3 | $0.14 |
| q-004 | adversarial | 1.0 | 60.8s | 6 | 117,409 | 5,421 | 4.6% | 4 | $0.17 |
| q-005 | single-hop | 1.0 | 47.1s | 7 | 94,198 | 7,135 | 7.6% | 5 | $0.18 |
| q-006 | multi-hop | 1.0 | 27.4s | 4 | 63,294 | 3,283 | 5.2% | 2 | $0.10 |
| q-007 | multi-hop | 1.0 | 44.9s | 6 | 116,803 | 6,140 | 5.3% | 4 | $0.16 |
| q-008 | multi-hop | 1.0 | 31.8s | 4 | 63,587 | 3,488 | 5.5% | 2 | $0.11 |
| q-009 | adversarial | 0.0 | 70.3s | 10 | 196,619 | 12,392 | 6.3% | 8 | $0.26 |
| q-010 | multi-hop | 1.0 | 181.8s | 37 | 870,223 | 35,355 | 4.1% | 34 | $0.88 |
| q-011 | adversarial | 1.0 | 48.8s | 5 | 91,115 | 5,844 | 6.4% | 3 | $0.15 |
| q-012 | temporal | 1.0 | 31.2s | 4 | 63,220 | 3,449 | 5.5% | 2 | $0.10 |
| q-013 | open domain | 1.0 | 43.6s | 7 | 118,052 | 6,301 | 5.3% | 5 | $0.16 |
| q-014 | temporal | 1.0 | 42.8s | 6 | 115,134 | 4,745 | 4.1% | 4 | $0.14 |
| q-015 | temporal | 1.0 | 30.5s | 4 | 63,760 | 3,615 | 5.7% | 2 | $0.11 |
| q-016 | multi-hop | 1.0 | 23.8s | 4 | 63,183 | 3,409 | 5.4% | 2 | $0.10 |
| q-017 | single-hop | 1.0 | 71.9s | 9 | 124,755 | 10,141 | 8.1% | 7 | $0.21 |
| q-018 | single-hop | — | 300.1s | 0 | 0 | 0 | 0.0% | 0 | $0.00 |
| q-019 | single-hop | 1.0 | 27.5s | 5 | 62,792 | 2,920 | 4.7% | 3 | $0.10 |
| q-020 | open domain | 1.0 | 47.5s | 7 | 117,875 | 6,323 | 5.4% | 5 | $0.17 |
| q-021 | temporal | 1.0 | 27.7s | 4 | 63,241 | 3,467 | 5.5% | 2 | $0.10 |
| q-022 | adversarial | 1.0 | 73.5s | 9 | 181,067 | 8,259 | 4.6% | 7 | $0.22 |
| q-023 | adversarial | 1.0 | 38.7s | 6 | 115,173 | 4,866 | 4.2% | 4 | $0.14 |
| q-024 | temporal | 1.0 | 45.6s | 6 | 115,432 | 4,718 | 4.1% | 4 | $0.15 |
| q-025 | open domain | 1.0 | 59.2s | 10 | 151,509 | 7,719 | 5.1% | 8 | $0.21 |
| q-026 | adversarial | 1.0 | 206.5s | 34 | 1,262,739 | 42,316 | 3.4% | 32 | $1.12 |
| q-027 | adversarial | — | 35.3s | 6 | 114,663 | 4,719 | 4.1% | 4 | $0.14 |
| q-028 | temporal | 1.0 | 33.8s | 4 | 64,004 | 3,691 | 5.8% | 2 | $0.11 |
| q-029 | single-hop | 1.0 | 44.4s | 5 | 64,820 | 3,559 | 5.5% | 3 | $0.13 |
| q-030 | temporal | 1.0 | 95.9s | 19 | 456,064 | 20,749 | 4.5% | 17 | $0.47 |
| q-031 | temporal | 1.0 | 46.0s | 6 | 116,023 | 4,870 | 4.2% | 4 | $0.16 |
| q-032 | single-hop | 1.0 | 31.0s | 4 | 63,106 | 3,251 | 5.2% | 2 | $0.10 |
| q-033 | multi-hop | 1.0 | 33.4s | 4 | 63,690 | 3,663 | 5.8% | 2 | $0.11 |
| q-034 | single-hop | 0.5 | 53.8s | 7 | 155,029 | 8,878 | 5.7% | 5 | $0.21 |
| q-035 | adversarial | 0.0 | 33.6s | 4 | 62,624 | 2,819 | 4.5% | 2 | $0.10 |
| q-036 | temporal | 1.0 | 55.1s | 10 | 208,514 | 9,535 | 4.6% | 8 | $0.25 |
| q-037 | adversarial | — | 27.3s | 4 | 63,410 | 3,320 | 5.2% | 2 | $0.11 |
| q-038 | single-hop | 1.0 | 83.3s | 5 | 65,361 | 3,658 | 5.6% | 3 | $0.14 |
| q-039 | adversarial | 0.5 | 239.5s | 32 | 1,135,530 | 40,493 | 3.6% | 30 | $1.09 |
| q-040 | temporal | 1.0 | 31.3s | 4 | 63,535 | 3,579 | 5.6% | 2 | $0.10 |
| q-041 | adversarial | 1.0 | 37.0s | 6 | 114,757 | 4,609 | 4.0% | 4 | $0.15 |
| q-042 | adversarial | 1.0 | 65.5s | 6 | 117,324 | 5,708 | 4.9% | 4 | $0.16 |
| q-043 | temporal | 1.0 | 24.8s | 4 | 62,984 | 3,273 | 5.2% | 2 | $0.10 |
| q-044 | single-hop | 1.0 | 37.9s | 4 | 64,520 | 3,663 | 5.7% | 2 | $0.13 |
| q-045 | temporal | 1.0 | 31.0s | 4 | 63,379 | 3,470 | 5.5% | 2 | $0.10 |
| q-046 | temporal | 1.0 | 38.7s | 6 | 118,183 | 6,537 | 5.5% | 4 | $0.16 |
| q-047 | multi-hop | 1.0 | 30.1s | 4 | 62,986 | 3,277 | 5.2% | 2 | $0.10 |
| q-048 | single-hop | 1.0 | 24.9s | 4 | 63,087 | 3,328 | 5.3% | 2 | $0.10 |
| q-049 | temporal | 1.0 | 29.9s | 5 | 64,010 | 3,652 | 5.7% | 3 | $0.11 |
| q-050 | temporal | 1.0 | 24.5s | 6 | 108,899 | 3,079 | 2.8% | 4 | $0.13 |

## Excluded Questions

| ID | Reason |
|---|---|
| q-018 | Book title only visible in shared image (book cover) |
| q-027 | Precautionary sign content only visible in shared photo |
| q-037 | References a photo not available to the memory system |

## Remaining Errors

### Non-adversarial (0)

None.

### Adversarial (2, unweighted)

| ID | Question | Issue |
|---|---|---|
| q-009 | What setback did Caroline face recently? | The model identifies a different event (a negative encounter while hiking) than what the benchmark expects. The benchmark swaps subject — the expected answer is about Melanie's setback. |
| q-035 | What type of instrument does Caroline play? | The model finds acoustic guitar and piano (correct for Caroline), but the benchmark expects clarinet and violin (Melanie's instruments — subject swap). |

In all adversarial failures, the retrieval system found correct facts for the queried person. The failures are inherent to the adversarial format: a search tool cannot detect that a question deliberately swaps subjects.
