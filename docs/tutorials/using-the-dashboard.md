# Tutorial: Explore Your Knowledge with the Memex Dashboard

In this tutorial, we will start the Memex Dashboard and walk through its key views. By the end, you will know how to visually explore your knowledge base, search memories, inspect entity relationships, and trace information lineage.

## Prerequisites

* A running Memex server (see [Getting Started](getting-started.md))
* Some ingested content in at least one vault
* Node.js installed ([nodejs.org](https://nodejs.org/))

## Installing the dashboard

To install the dashboard, you can run `memex dashboard install`

> [!WARNING] As memex is currently private, you won't be able to downloade the releases. Instead, download the dashboard  distribution tarball from a release and install it using `memex dashboard install --path path/to/downloaded/tarball`

## Step 1: Start the Dashboard

First, let's make sure the Memex API server is running:

```bash
memex server status
```

We should see a message confirming the server is running. If not, start it with `memex server start -d`.

Now let's start the dashboard in development mode:

```bash
memex dashboard start --dev
```

We should see output indicating the Vite dev server is starting. Once ready, open a browser and navigate to:

```
http://localhost:5173
```

We should see the Memex dashboard with a sidebar on the left and the Overview page in the main area.

> [!TIP]
> For production mode, first build with `npm run build` in the `packages/dashboard` directory, then use `memex dashboard start -d`. The production server defaults to port 3001.

## Step 2: Review the Overview Page

The Overview page (`/`) is the landing page. It shows a high-level summary of our knowledge base:

- **Metric Cards** at the top display counts for Notes, Memories (extracted facts), Entities, and the Reflection Queue
- **Token Usage** chart shows daily token consumption for LLM operations, with filters for 7-day, 30-day, 90-day, and all-time views
- **Server Health** panel shows the current server status, memory usage, CPU time, and total requests
- **Recent Memories** feed on the right lists the most recently ingested notes — click any item to view its full content in a modal

Let's click on one of the recent memory items. A dialog appears showing the full Markdown content of the note.

## Step 3: Search Memories

Let's navigate to the **Memory Search** page by clicking "Memory Search" in the sidebar (or visiting `/search`).

This page lets us search across extracted facts and observations using Memex's TEMPR retrieval system. Let's try it:

1. Type a query in the search box, for example: `How does Memex extract facts?`
2. Press Enter or click the search button

We should see a list of memory units (extracted facts) ranked by relevance. Each result shows:

- The fact text
- Its type (world, event, or observation)
- A relevance score
- The vault it belongs to

We can refine our search using the strategy filters at the top. The five strategies are:

- **Semantic** — finds conceptually similar memories via embeddings
- **Keyword** — matches exact terms
- **Graph** — finds memories connected through the entity graph
- **Temporal** — finds memories from relevant time periods
- **Mental Model** — matches against synthesized high-level understanding

Toggle any combination of strategies on or off to see how results change.

## Step 4: Search Notes

Now let's try the **Note Search** page by clicking "Note Search" in the sidebar (or visiting `/doc-search`).

Unlike Memory Search (which searches extracted facts), Note Search finds passages within the original source documents. Let's enter the same query we used before.

The results show matched notes with relevant snippets highlighted. Click on any result to expand it and view the full note content with a page index (table of contents) for quick navigation.

> [!TIP]
> Toggle "AI Summary" on to get an AI-generated answer synthesized from the matched note sections.

## Step 5: Explore the Entity Graph

Let's navigate to the **Entity Graph** page by clicking "Entity Graph" in the sidebar (or visiting `/entity`).

This page shows a visual graph of entities (people, concepts, technologies, organizations) extracted from our notes:

- **Nodes** represent entities, colored and sized by type and mention count
- **Edges** represent co-occurrence relationships between entities
- We can **click a node** to select it and see its details in the side panel
- We can **drag nodes** to rearrange the graph layout
- The **filter panel** on the left lets us filter by entity type, connection strength, importance, and recency

Let's click on an entity node. The side panel on the right shows:

- The entity name and mention count
- **Mentions** — memory units that reference this entity, grouped by date in a timeline view
- **Co-occurs with** — other entities that frequently appear alongside this one

We can also use the search box at the top of the graph to find a specific entity by name.

## Step 6: Trace Information Lineage

Let's navigate to the **Lineage** page by clicking "Lineage" in the sidebar (or visiting `/lineage`).

Lineage lets us trace the provenance of information — where a fact came from and how it was derived. To explore lineage:

1. Use the search box to find an entity by name
2. Select an entity from the results

The graph that appears shows the full provenance chain:

- **Source Note** — the original document that was ingested
- **Memory Units** — the facts extracted from that note
- **Observations** — patterns identified across multiple facts
- **Mental Models** — high-level understanding synthesized from observations

This chain helps us understand how Memex arrived at any particular piece of knowledge, and we can trigger a reflection from this page to update mental models.

## Step 7: Check System Status and Settings

Two more pages are worth knowing about:

**System Status** (`/status`) shows detailed system health information including server metrics and the reflection queue status.

**Settings** (`/settings`) lets us manage our knowledge base:

- **Vaults tab** — create, delete, and switch between vaults. Each vault shows its role: "Writer" (active vault for new content), "Attached" (included in searches), or "Available" (exists but not active)
- **Preferences tab** — configure default search limits, preferred retrieval strategies, and theme (light/dark mode)

Let's try switching the theme: click the sun/moon icon in the bottom of the sidebar to toggle between light and dark mode.

## Conclusion

We have successfully started the Memex Dashboard and explored its key features: the Overview dashboard, Memory Search, Note Search, Entity Graph, Lineage tracing, and Settings. The dashboard provides a visual way to interact with the same knowledge base accessible through the CLI and API.

## Next Steps

* [Doc Search vs Memory Search](../how-to/doc-search-vs-memory-search.md) — understand when to use each search type
* [Organize with Vaults](../how-to/organize-with-vaults.md) — manage multiple knowledge domains
* [Hindsight Framework](../explanation/hindsight-framework.md) — learn how Memex extracts, retrieves, and reflects on knowledge
