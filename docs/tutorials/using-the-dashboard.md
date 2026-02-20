# Using the Memex Dashboard

The Memex Dashboard provides a visual interface for your knowledge base, allowing you to explore connections, view lineage, and manage your data.

## Starting the Dashboard

Ensure the Memex Server is running, then start the dashboard:

```bash
# Terminal 1: Start the API Server in daemon mode
memex server start -d

# Terminal 2: Start the Dashboard in daemon mode
memex dashboard start -d
```

Open your browser to `http://localhost:3000`.

## Features

### 1. Overview
The landing page shows recent activity, including newly ingested notes and memory units. It provides a quick way to resume your work.

### 2. Search
A powerful search interface that combines:
- **Semantic Search**: Find concepts related to your query.
- **Keyword Search**: Precise matching.
- **Time-based filtering**: Find memories from a specific period.

### 3. Entity Graph (`/entity`)
Visualize the connections between entities (people, concepts, technologies) in your vault.
- **Nodes**: Entities.
- **Edges**: Relationships extracted from your notes.
- **Interaction**: Click a node to see related memories and notes.

### 4. Lineage (`/lineage`)
Trace the provenance of information.
- See where a specific fact came from (Source Document -> Memory Unit -> Fact).
- Understand how observations and mental models were synthesized.

### 5. Settings
- Manage **Vaults** (Create, Switch, Delete).
- Configure active models.
- View system status.
