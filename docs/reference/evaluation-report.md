# LoCoMo Evaluation Report

> Model: Claude Opus 4 via Claude Code CLI | Judge: Gemini 3 Flash
> Date: 2026-03-11

## Summary

| Metric | Value |
|---|---|
| Questions (scored) | 36 (excl. 3 image-dependent, 11 adversarial) |
| Overall Score (non-adversarial) | **0.958** |
| Perfect answers | 34 (94.4%) |
| Wrong answers | 1 (2.8%) |
| Total cost | $8.98 |
| Total duration | 46.0 min |

## Scores by Category

| Category | Count | Mean Score | Perfect | Wrong |
|---|---|---|---|---|
| Single-Hop | 9 | **0.889** | 8 | 1 |
| Multi-Hop | 9 | **1.000** | 9 | 0 |
| Open Domain | 3 | **1.000** | 3 | 0 |
| Temporal | 15 | **0.967** | 14 | 0 |
| Adversarial (unweighted) | 11 | 0.795 | 8 | 2 |
| **Non-adversarial** | **36** | **0.958** | **34** | **1** |

### A note on adversarial scoring

Adversarial questions in the LoCoMo dataset deliberately swap the subject — asking about person A when the ground-truth answer actually pertains to person B. The expected "correct" behavior is for the model to detect this swap.

However, Memex is a search-and-answer tool. When asked "What instrument does Caroline play?", it correctly searches for Caroline's instruments, finds "acoustic guitar", and returns that answer. The fact that the benchmark expects "clarinet and violin" (Melanie's instruments) tests something outside the system's scope: the model would need to know the question is deliberately misleading.

In the failed adversarial cases, the retrieval system found correct, relevant facts for the queried person. These are not retrieval failures — they are a fundamental mismatch between a search tool's behavior and the adversarial benchmark's expectations. Adversarial scores are therefore reported separately and excluded from the weighted overall score.

## Retrieval Efficiency

### Token breakdown

| Metric | Value |
|---|---|
| Total tokens (all) | 6,507,762 |
| Retrieval tokens (Memex) | 346,954 (**5.3%** of total) |
| Agent overhead tokens | 6,160,808 (94.7%) |
| Retrieval tokens/question (mean) | 7,006 |
| Retrieval tokens/question (median) | 4,734 |

### Retrieval by tool

| Tool | Tokens | Share | Calls | Calls/q |
|---|---|---|---|---|
| `memory_search` | 182,235 | 52.5% | 70 | 1.4 |
| `get_nodes` | 55,162 | 15.9% | 39 | 0.8 |
| `note_search` | 54,957 | 15.8% | 57 | 1.1 |
| `get_page_indices` | 29,491 | 8.5% | 45 | 0.9 |
| `get_entity_mentions` | 13,950 | 4.0% | 5 | 0.1 |
| `read_note` | 5,824 | 1.7% | 6 | 0.1 |
| `list_entities` | 3,735 | 1.1% | 28 | 0.6 |
| `list_notes` | 1,589 | 0.5% | 1 | 0.0 |
| `list_assets` | 11 | 0.0% | 1 | 0.0 |

![Retrieval token breakdown](evaluation-plots/retrieval_token_breakdown.png)

### Efficiency by category

| Category | Duration | Turns | Total Tokens | Retr Tokens | Retr % | Memex Calls |
|---|---|---|---|---|---|---|
| Single-Hop | 55.5s | 5.4 | 73,468 | 4,928 | 6.3% | 3.4 |
| Multi-Hop | 47.7s | 6.6 | 130,042 | 6,261 | 5.8% | 4.4 |
| Open Domain | 60.0s | 8 | 120,229 | 7,811 | 6.6% | 6 |
| Temporal | 43.6s | 6.1 | 102,658 | 5,445 | 5.5% | 4.1 |
| Adversarial | 77.7s | 10 | 225,034 | 11,227 | 5.2% | 8 |

### Distribution plots

![Tokens By Category](evaluation-plots/tokens_by_category.png)

![Duration By Category](evaluation-plots/duration_by_category.png)

![Retrieval Vs Total](evaluation-plots/retrieval_vs_total.png)

![Duration Vs Memex Calls](evaluation-plots/duration_vs_memex_calls.png)

![Turns Distribution](evaluation-plots/turns_distribution.png)

## Tool Usage Patterns

| Metric | Value |
|---|---|
| ToolSearch calls/question | 1.0 (51 total, only 1 question with >1) |
| Entity exploration | 23/50 questions (46%) |
| Citations (inline refs) | 50/50 (100%) |
| Citations (reference list) | 50/50 (100%) |
| `read_note` (discouraged) | 6 total (0.1/q) |

### Memex tool call distribution

| Tool | Total Calls | Calls/q |
|---|---|---|
| `memory_search` | 70 | 1.4 |
| `note_search` | 57 | 1.1 |
| `get_page_indices` | 45 | 0.9 |
| `get_nodes` | 39 | 0.8 |
| `list_entities` | 28 | 0.6 |
| `read_note` | 6 | 0.1 |
| `get_entity_mentions` | 5 | 0.1 |
| `list_assets` | 1 | 0.0 |
| `list_notes` | 1 | 0.0 |

## Retrieval Paths

The agent autonomously selects a retrieval path based on question complexity. Entity exploration (`list_entities`, `get_entity_mentions`) is used as a supplementary step in any path — it was triggered in 46% of questions.

| Pattern | Count | Share | Avg Score | Avg Tools | Avg Duration | Avg Retr Tok | Avg Cost |
|---|---|---|---|---|---|---|---|
| Simple + entity | 3 | 6% | 0.67 | 2.3 | 33s | 2,627 | $0.09 |
| Two-stage | 14 | 28% | 0.98 | 2.0 | 35s | 3,551 | $0.10 |
| Two-stage + entity | 4 | 8% | 0.75 | 3.0 | 53s | 3,478 | $0.12 |
| Deep verification | 11 | 22% | 0.91 | 4.1 | 46s | 4,975 | $0.14 |
| Deep + entity | 7 | 14% | 0.86 | 5.6 | 57s | 7,348 | $0.18 |
| Exhaustive | 11 | 22% | 0.86 | 11.0 | 95s | 15,389 | $0.36 |

### Two-stage path (14 questions, 28%)

Memory search and note search provide sufficient context to answer directly. The most efficient path — highest score (0.98) at lowest cost ($0.10/q). Dominates multi-hop (6) and temporal (5) questions where facts are directly retrievable.

```mermaid
graph LR
    A[memory_search] --> B[note_search] --> C[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#8172B2,color:white
    style C fill:#55A868,color:white
```

**Typical questions**: straightforward fact lookups — "When did Caroline go to the adoption meeting?" (q-002), "When did Melanie buy the book?" (q-016), "Which song did they listen to?" (q-021).

### Deep verification path (11 questions, 22%)

Full two-speed reading: search finds candidate notes, then `get_page_indices` → `get_nodes` drills into specific sections for precise evidence. Primarily used for temporal (6) and adversarial (3) questions that need exact details from longer conversation sessions.

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

**Typical questions**: questions requiring verification from source text — "What did Melanie do after completing her final exams?" (q-014), "What does Melanie think about her new Fitbit?" (q-015).

### Deep + entity path (7 questions, 14%)

Adds entity exploration to deep verification. The agent queries `list_entities` and/or `get_entity_mentions` to discover relationships before or after searching. Used for open-domain (2), temporal (3), and questions involving person-to-person connections.

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

**Typical questions**: relationship and open-domain questions — "Would Caroline enjoy Melanie's favorite museum?" (q-020), "What were the highlights of Melanie's week?" (q-031).

### Exhaustive path (11 questions, 22%)

Multiple rounds of searching and reading across different notes and queries. The agent iterates when initial results are insufficient — refining queries, searching additional sessions, or reading more note sections. Most expensive ($0.36/q) but necessary for complex questions. Dominated by adversarial (6) questions where the agent tries harder to find contradicting information.

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

**Typical questions**: complex multi-evidence questions — "When did Melanie read 'Nothing is Impossible'?" (q-010, 22 tools, $0.61), "What counseling services is Melanie interested in?" (q-001, adversarial subject swap).

### Simple + entity path (3 questions, 6%)

A single search round with entity exploration. No deep reading needed — memory search alone returns enough. Lowest retrieval token usage (2,627/q). The 0.67 average score is misleading: both non-adversarial questions scored 1.0; the average is dragged down by q-035 (adversarial subject swap).

```mermaid
graph LR
    A[memory_search] --> B[list_entities] --> C[Answer]
    style A fill:#4C72B0,color:white
    style B fill:#DD8452,color:white
    style C fill:#55A868,color:white
```

### Two-stage + entity path (4 questions, 8%)

Memory and note search augmented with entity exploration. Used primarily for single-hop (3) questions where entity relationships help contextualize the answer.

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
| Total tokens | 6,507,762 |
| Input tokens | 6,432,306 |
| Output tokens | 75,456 |
| Retrieval tokens (Memex) | 346,954 (5.3%) |
| Total duration | 2,761s (46.0 min) |
| Avg duration/question | 55.2s |
| Median duration/question | 43.6s |
| Total cost | $8.98 |
| Avg cost/question | $0.180 |

## Per-Question Detail

| ID | Category | Score | Dur | Turns | Total Tok | Retr Tok | Retr % | Memex# | Cost |
|---|---|---|---|---|---|---|---|---|---|
| q-001 | adversarial | 1.0 | 118.2s | 10 | 171,095 | 10,342 | 6.0% | 8 | $0.27 |
| q-002 | multi-hop | 1.0 | 39.0s | 4 | 56,202 | 3,521 | 6.3% | 2 | $0.10 |
| q-003 | multi-hop | 1.0 | 52.3s | 6 | 105,531 | 5,448 | 5.2% | 4 | $0.15 |
| q-004 | adversarial | 1.0 | 76.8s | 6 | 105,899 | 5,381 | 5.1% | 4 | $0.16 |
| q-005 | single-hop | 1.0 | 39.2s | 5 | 57,214 | 3,511 | 6.1% | 3 | $0.12 |
| q-006 | multi-hop | 1.0 | 36.5s | 4 | 56,224 | 3,301 | 5.9% | 2 | $0.10 |
| q-007 | multi-hop | 1.0 | 45.9s | 6 | 104,727 | 6,072 | 5.8% | 4 | $0.15 |
| q-008 | multi-hop | 1.0 | 38.9s | 4 | 56,521 | 3,517 | 6.2% | 2 | $0.10 |
| q-009 | adversarial | 0.0 | 68.6s | 8 | 135,308 | 7,223 | 5.3% | 6 | $0.18 |
| q-010 | multi-hop | 1.0 | 128.3s | 23 | 622,109 | 23,795 | 3.8% | 20 | $0.61 |
| q-011 | adversarial | 1.0 | 55.6s | 7 | 133,194 | 6,584 | 4.9% | 5 | $0.18 |
| q-012 | temporal | 1.0 | 28.7s | 4 | 56,079 | 3,323 | 5.9% | 2 | $0.10 |
| q-013 | open domain | 1.0 | 55.4s | 7 | 107,930 | 5,998 | 5.6% | 5 | $0.16 |
| q-014 | temporal | 0.5 | 40.4s | 6 | 103,083 | 4,578 | 4.4% | 4 | $0.14 |
| q-015 | temporal | 1.0 | 43.8s | 6 | 103,882 | 4,919 | 4.7% | 4 | $0.14 |
| q-016 | multi-hop | 1.0 | 27.7s | 4 | 56,301 | 3,549 | 6.3% | 2 | $0.10 |
| q-017 | single-hop | 1.0 | 89.5s | 9 | 142,669 | 10,541 | 7.4% | 7 | $0.23 |
| q-018 | single-hop | — | 69.8s | 8 | 140,779 | 9,727 | 6.9% | 6 | $0.20 |
| q-019 | single-hop | 1.0 | 38.2s | 5 | 56,015 | 2,935 | 5.2% | 3 | $0.10 |
| q-020 | open domain | 1.0 | 57.6s | 8 | 114,157 | 9,594 | 8.4% | 6 | $0.20 |
| q-021 | temporal | 1.0 | 32.9s | 4 | 56,463 | 3,612 | 6.4% | 2 | $0.10 |
| q-022 | adversarial | 1.0 | 92.4s | 11 | 200,612 | 10,547 | 5.3% | 9 | $0.27 |
| q-023 | adversarial | 1.0 | 37.9s | 6 | 104,031 | 4,960 | 4.8% | 4 | $0.14 |
| q-024 | temporal | 1.0 | 57.8s | 7 | 104,444 | 4,734 | 4.5% | 5 | $0.15 |
| q-025 | open domain | 1.0 | 67.2s | 9 | 138,599 | 7,841 | 5.7% | 7 | $0.20 |
| q-026 | adversarial | 1.0 | 201.4s | 31 | 1,118,121 | 50,544 | 4.5% | 29 | $1.13 |
| q-027 | adversarial | — | 40.5s | 6 | 102,762 | 4,638 | 4.5% | 4 | $0.14 |
| q-028 | temporal | 1.0 | 43.5s | 6 | 103,911 | 4,970 | 4.8% | 4 | $0.14 |
| q-029 | single-hop | 1.0 | 65.0s | 5 | 57,307 | 3,232 | 5.6% | 3 | $0.13 |
| q-030 | temporal | 1.0 | 76.6s | 14 | 320,239 | 17,005 | 5.3% | 12 | $0.36 |
| q-031 | temporal | 1.0 | 47.1s | 7 | 108,192 | 6,827 | 6.3% | 5 | $0.17 |
| q-032 | single-hop | 1.0 | 28.8s | 4 | 56,113 | 3,247 | 5.8% | 2 | $0.10 |
| q-033 | multi-hop | 1.0 | 31.8s | 4 | 56,658 | 3,754 | 6.6% | 2 | $0.10 |
| q-034 | single-hop | 0.0 | 68.1s | 8 | 120,543 | 10,836 | 9.0% | 6 | $0.23 |
| q-035 | adversarial | 0.0 | 27.5s | 4 | 55,239 | 2,593 | 4.7% | 2 | $0.09 |
| q-036 | temporal | 1.0 | 58.3s | 7 | 107,514 | 6,626 | 6.2% | 5 | $0.17 |
| q-037 | adversarial | — | 34.4s | 5 | 56,708 | 3,288 | 5.8% | 3 | $0.11 |
| q-038 | single-hop | 1.0 | 74.2s | 5 | 58,508 | 3,883 | 6.6% | 3 | $0.14 |
| q-039 | adversarial | 1.0 | 91.5s | 16 | 290,901 | 16,974 | 5.8% | 14 | $0.36 |
| q-040 | temporal | 1.0 | 30.2s | 4 | 56,509 | 3,626 | 6.4% | 2 | $0.10 |
| q-041 | adversarial | 1.0 | 43.0s | 7 | 104,405 | 4,981 | 4.8% | 5 | $0.15 |
| q-042 | adversarial | 0.8 | 42.0s | 4 | 56,571 | 3,372 | 6.0% | 2 | $0.11 |
| q-043 | temporal | 1.0 | 28.9s | 4 | 56,078 | 3,228 | 5.8% | 2 | $0.10 |
| q-044 | single-hop | 1.0 | 62.2s | 4 | 57,881 | 3,815 | 6.6% | 2 | $0.13 |
| q-045 | temporal | 1.0 | 37.3s | 6 | 101,599 | 4,220 | 4.2% | 4 | $0.13 |
| q-046 | temporal | 1.0 | 55.4s | 7 | 106,305 | 6,467 | 6.1% | 5 | $0.16 |
| q-047 | multi-hop | 1.0 | 28.9s | 4 | 56,106 | 3,388 | 6.0% | 2 | $0.10 |
| q-048 | single-hop | 1.0 | 34.0s | 4 | 54,958 | 2,353 | 4.3% | 2 | $0.09 |
| q-049 | temporal | 1.0 | 40.2s | 4 | 58,012 | 4,459 | 7.7% | 2 | $0.12 |
| q-050 | temporal | 1.0 | 32.4s | 6 | 97,554 | 3,075 | 3.2% | 4 | $0.13 |

## Excluded Questions

| ID | Reason |
|---|---|
| q-018 | Book title only visible in shared image (book cover) |
| q-027 | Precautionary sign content only visible in shared photo |
| q-037 | References a photo not available to the memory system |

## Remaining Errors

### Non-adversarial (1)

| ID | Category | Question | Issue |
|---|---|---|---|
| q-034 | Single-Hop | How many times has Melanie gone to the beach in 2023? | The model identifies only one beach trip in 2023, whereas the expected answer is 2. The model's reasoning suggests it could not find the second trip in the extracted memories. |

### Adversarial (2, unweighted)

| ID | Question | Issue |
|---|---|---|
| q-009 | What setback did Caroline face recently? | The model identifies a different event (a negative encounter while hiking) than what the benchmark expects. The benchmark swaps subject — the expected answer is about Melanie's setback. |
| q-035 | What type of instrument does Caroline play? | The model finds acoustic guitar and piano (correct for Caroline), but the benchmark expects clarinet and violin (Melanie's instruments — subject swap). |

In all adversarial failures, the retrieval system found correct facts for the queried person. The failures are inherent to the adversarial format: a search tool cannot detect that a question deliberately swaps subjects.
