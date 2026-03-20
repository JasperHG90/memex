# About the Dashboard Architecture

The Memex dashboard is a single-page application built with React and Vite that provides a visual interface for exploring your knowledge base. It communicates with the Memex REST API to search memories, browse entities, view note lineage, and monitor system status.

## Context

Command-line tools and MCP integrations serve AI agents well, but humans benefit from visual exploration. The dashboard fills this gap — it lets you see the entity graph, browse timelines, search across both memories and documents, and inspect how reflection produces mental models. It is built with React+Vite for performance and rich interactivity.

## Technology Stack

| Layer | Technology | Purpose |
| :--- | :--- | :--- |
| **UI Framework** | React 19 | Component model, state management |
| **Build Tool** | Vite 7 | Fast dev server, production bundling |
| **Styling** | Tailwind CSS 4 | Utility-first CSS |
| **Components** | shadcn/ui (Radix primitives) | Accessible, unstyled component library |
| **Routing** | React Router 7 | Client-side page navigation |
| **Data Fetching** | TanStack React Query 5 | Server state caching, background refetching |
| **Client State** | Zustand 5 | Lightweight global stores |
| **Validation** | Zod 4 | Runtime API response validation |
| **Charts** | Recharts 3 | Statistical visualizations |
| **Graphs** | React Flow (@xyflow/react) + d3-force + dagre | Entity graph and lineage tree visualization |
| **Icons** | Lucide React | Consistent icon set |

## Application Structure

```
src/
  main.tsx          -- Entry point, router, providers
  app.tsx           -- Root layout (sidebar + content area)
  api/
    client.ts       -- HTTP client (fetch wrapper)
    generated.ts    -- Zod schemas from OpenAPI spec
    validate.ts     -- Response validation utilities
    ndjson.ts       -- NDJSON streaming parser
    hooks/          -- TanStack Query hooks per endpoint
  pages/            -- Route-level components (lazy loaded)
  components/
    layout/         -- Sidebar, navigation
    shared/         -- Reusable components (error boundary, banners)
    ui/             -- shadcn/ui primitives
    onboarding/     -- Welcome modal
  hooks/            -- Custom React hooks
  stores/           -- Zustand global state
  lib/              -- Utilities (debounce, media queries)
```

## API Data Flow

The dashboard follows a strict data flow pattern:

```
OpenAPI spec --> Zod schemas --> TanStack Query hooks --> Page components
```

### 1. Schema Generation

Zod schemas are auto-generated from the Memex server's OpenAPI specification:

```bash
# Fetch the latest spec from a running server
npm run fetch-openapi

# Generate Zod schemas
npm run generate-api
```

This produces `src/api/generated.ts` with typed Zod schemas for every API response (VaultDTO, EntityDTO, MemoryUnitDTO, etc.). The schemas serve as the single source of truth for API types.

### 2. Response Validation

The `validate.ts` module wraps API responses with Zod validation:

- In **development**: Responses are validated against Zod schemas. Mismatches are logged as warnings but do not break the UI — the raw data is still returned. This catches API drift early.
- In **production**: Validation is skipped entirely for performance. The Zod schemas still provide TypeScript types at compile time.

### 3. API Client

The `client.ts` module provides a thin `fetch` wrapper (`apiFetch`) that:

- Prepends the API base URL (configurable via `VITE_API_BASE`, defaults to `/api/v1`)
- Handles NDJSON streaming responses (used by memory search)
- Parses error responses into structured `ApiError` objects
- Handles 204 No Content responses

### 4. TanStack Query Hooks

Each API endpoint has a corresponding hook in `src/api/hooks/` that wraps TanStack React Query:

- **Queries** (`useQuery`): For read operations with automatic caching, background refetching, and stale-while-revalidate semantics (staleTime: 30s)
- **Mutations** (`useMutation`): For write operations with cache invalidation

Page components consume these hooks directly, never calling the API client manually.

## Page Components

All pages are lazy-loaded via `React.lazy()` for code-splitting. Each page wraps in a `Suspense` boundary that shows a skeleton loader during chunk download.

| Page | Route | Description |
| :--- | :--- | :--- |
| **Overview** | `/` | Dashboard home with stats, recent activity, charts |
| **Memory Search** | `/search` | TEMPR memory search with filters and result cards |
| **Note Search** | `/doc-search` | Document search with reason/summarize toggles |
| **Entity Graph** | `/entity` | Interactive knowledge graph (React Flow + d3-force) |
| **Lineage** | `/lineage` | Provenance tree for memory units (dagre layout) |
| **Timeline** | `/timeline` | Chronological view of memory units |
| **Reflection** | `/reflection` | Mental model viewer and manual reflection trigger |
| **Knowledge Flow** | `/knowledge-flow` | Visualization of information flow through the system |
| **System Status** | `/status` | Server health, worker status, queue metrics |
| **Settings** | `/settings` | Configuration viewer, vault management, theme |

## State Management

The dashboard uses two layers of state:

### Server State (TanStack React Query)

All data from the Memex API lives in TanStack Query's cache. This provides:

- Automatic background refetching when data becomes stale
- Cache deduplication (multiple components querying the same data share a single request)
- Optimistic updates for mutations
- Retry logic (1 retry by default)

### Client State (Zustand)

Local UI state that does not come from the server is managed by Zustand stores:

- **VaultStore** (`vault-store.ts`): Tracks the default write vault and reader vault. Initialized on app mount by fetching the server configuration.
- **UIStore** (`ui-store.ts`): Tracks modal visibility (command palette, quick note), sidebar state.
- **PreferencesStore** (`preferences-store.ts`): User preferences (theme, display density) persisted to localStorage.

## Key UI Patterns

### Command Palette

A `cmdk`-based command palette (Cmd+K) provides keyboard-driven navigation and quick actions across all pages.

### Quick Note Modal

A global modal for capturing notes without navigating away from the current page. Accessible via keyboard shortcut.

### Connection Banner

A persistent banner that appears when the Memex server is unreachable, using a polling health check.

### Error Boundary

Every page is wrapped in an error boundary that catches render errors and displays a recovery UI instead of crashing the entire application.

## Build and Development

The dashboard is built and served in two modes:

- **Development**: `just dashboard-dev` starts the Vite dev server with hot module replacement. The API is proxied to the Memex server.
- **Production**: `just dashboard-build` produces a static bundle that the Memex server serves directly. The build output goes to `dist/` and is embedded in the Python package.

## See Also

* [How to Use the Dashboard](../tutorials/using-the-dashboard.md) — guided walkthrough
* [REST API Reference](../reference/rest-api.md) — the API the dashboard consumes
* [How to Configure Memex](../how-to/configure-memex.md) — dashboard host/port settings
