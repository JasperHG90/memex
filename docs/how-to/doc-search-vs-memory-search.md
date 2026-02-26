# Document Search vs. Memory Search

Memex provides two distinct search interfaces. Choosing the right one depends on whether you need a specific passage from a source document (or subset of documents) or a synthesized fact from your entire knowledge graph.

## 1. Note Search (`memex note search`)

**Target**: Raw source notes (PDFs, Markdown, Web Scrapes).

- **How it works**: Notes are split into semantic nodes (using the PageIndex algorithm). Memex searches these nodes using a hybrid multi-channel approach (Semantic Vector Search + Keyword BM25 + Entity Graph + Temporal). Results are fused using **Reciprocal Rank Fusion (RRF)** or a **Position-Aware Blending** algorithm.
- **Advanced Capabilities**:
    - **Skeleton-Tree Identification (`--reason`)**: Memex can scan the note's logical structure (TOC) and extract the exact sections relevant to your query, explaining *why* they are relevant.
    - **Synthesis (`--summarize`)**: After finding relevant sections, it can synthesize a direct answer based purely on those sections.
    - **Scoped Search (`--vault`)**: You can restrict the search to specific vaults or projects, effectively searching *within* specific sets of notes rather than your global memory.
    - **MMR Diversity** (REST API): The `POST /api/v1/notes/search` endpoint accepts an `mmr_lambda` field (float 0.0–1.0 or `null`). When set, results are re-ranked using Maximal Marginal Relevance to surface distinct notes rather than multiple high-scoring chunks from the same note. `0.0` = pure diversity, `1.0` = pure relevance; omit or set `null` to disable.
- **When to use**:
    - You need to find a specific quote or paragraph from a known note.
    - You want to see the original formatting and context of a note.
    - You are searching for "raw" information that hasn't been consolidated yet.
    - You want to interrogate a specific note or vault ("Search in these specific notes").

## 2. Memory Search (`memex memory search`)

**Target**: Atomic facts, observations, and opinions (Memory Units).

- **How it works**: Uses the **TEMPR** Recall architecture (Temporal, Entity, Mental Model, Probabilistic Ranking). This system doesn't just look for keywords; it considers when information was learned, how it relates to known entities, and whether it aligns with high-level **Mental Models** formed by the background reflection engine.
- **Advanced Capabilities**:
    - **Opinion Formation**: Memex uses Bayesian confidence scoring to dynamically evaluate competing facts or opinions across your entire memory graph.
    - **Strategy Tuning**: You can disable specific strategies (e.g., `--no-mental-model`, `--no-temporal`) to narrow down how the system recalls information.
- **When to use**:
    - You want a synthesized answer to a question spanning your entire knowledge base (e.g., "What is the project status?").
    - You are exploring relationships between entities over time.
    - You want to benefit from the **Reflection** process (where Memex consolidates multiple notes into a single mental model).
    - You are asking general questions ("Search over entire memory").

## Summary Comparison

| Feature | Note Search (`note`) | Memory Search (`memory`) |
| :--- | :--- | :--- |
| **Granularity** | Coarse (Chunks/Snippets/Nodes) | Atomic (Facts/Opinions) |
| **Retrieval Architecture** | Hybrid RRF / Position-Aware Fusion | TEMPR (Hindsight) |
| **Output** | Document Snippets & Reasoning | Memory Units + Synthesized Answer |
| **Context Scope** | Scoped to specific documents/vaults | Cross-vault knowledge graph |
| **Best for...** | "Find the PDF where X is mentioned" | "Tell me what we know about X globally" |

## Pro-Tip: The Lineage Bridge

If you find a fact in **Memory Search** and want to see the original source, use the **Lineage** command:

```bash
# Get the Document ID from memory search, then trace it
memex memory lineage document <document-uuid>
```
