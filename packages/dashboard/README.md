# Memex Dashboard (`dashboard`)

A web-based UI for exploring and managing your Memex knowledge base. Built with React, Vite, and Tailwind CSS.

## Features

- **Overview** — Vault statistics, recent activity, and system health at a glance.
- **Memory Search** — Semantic and keyword search across memory units with ranked results.
- **Note Search** — Search source documents with optional AI-powered summarization.
- **Entity Graph** — Interactive force-directed graph visualization of entities and their relationships (powered by React Flow and d3-force).
- **Knowledge Flow** — Visualize how information flows through extraction, retrieval, and reflection.
- **Lineage** — Trace provenance from memory units back to source notes.
- **Timeline** — Chronological view of ingested content and memory activity.
- **Reflection** — Monitor and manage the Hindsight reflection engine.
- **System Status** — Server health, connection pool stats, and background task monitoring.
- **Settings** — Configure vaults, models, and server parameters.
- **Dark/Light Theme** — Toggle via `next-themes`.

## Tech Stack

| Layer | Library |
|:------|:--------|
| Framework | React 19 |
| Build | Vite 7 |
| Styling | Tailwind CSS 4 + shadcn/ui (Radix primitives) |
| State | Zustand (client), TanStack Query (server) |
| Routing | React Router 7 |
| Charts | Recharts |
| Graph | React Flow + d3-force + dagre |
| Markdown | react-markdown |
| API | Generated Zod client from OpenAPI spec |
| Testing | Vitest + Testing Library |

## Development

```bash
# From the repository root
just dashboard-dev

# Or directly
cd packages/dashboard
npm install
npm run dev
```

The dev server runs on `http://localhost:3001` by default and proxies API requests to the Memex Core server at `http://localhost:8000`.

### Available Scripts

| Script | Description |
|:-------|:------------|
| `npm run dev` | Start Vite dev server with HMR. |
| `npm run build` | Type-check and build for production. |
| `npm run preview` | Preview the production build locally. |
| `npm run lint` | Run ESLint. |
| `npm run typecheck` | Run TypeScript type checking (no emit). |
| `npm test` | Run tests with Vitest (single run). |
| `npm run test:watch` | Run tests in watch mode. |
| `npm run fetch-openapi` | Fetch the OpenAPI spec from a running server. |
| `npm run generate-api` | Regenerate the Zod API client from the OpenAPI spec. |

### Regenerating the API Client

When the Core server's REST API changes:

```bash
# 1. Start the server
memex server start

# 2. Fetch the latest OpenAPI spec
npm run fetch-openapi

# 3. Regenerate the typed client
npm run generate-api
```

## Project Structure

```
packages/dashboard/
├── src/
│   ├── api/              # OpenAPI spec and generated Zod client
│   ├── components/       # Reusable UI components (shadcn/ui based)
│   ├── hooks/            # React hooks for data fetching and state
│   ├── lib/              # Utility functions
│   ├── pages/            # Route-level page components
│   ├── stores/           # Zustand state stores
│   ├── app.tsx           # Root app with routing
│   └── main.tsx          # Entry point
├── tests/                # Vitest + Testing Library tests
├── package.json
├── vite.config.ts
└── tsconfig.json
```

## Documentation

- [Using the Dashboard](../../docs/tutorials/using-the-dashboard.md)
- [REST API Reference](../../docs/reference/rest-api.md)
