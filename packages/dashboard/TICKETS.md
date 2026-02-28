# Dashboard Migration Tickets

## Status Legend
- [ ] Not started
- [~] In progress
- [x] Complete
- [!] Blocked

---

## Phase 0: Foundation

### T-001: Scaffold Vite + React + TypeScript project
**Assignee:** staff-eng-1
**Status:** [x]
**Priority:** P0 (blocking everything)

Initialize `packages/dashboard/` with:
```bash
npm create vite@latest . -- --template react-ts
```

Configure:
- `vite.config.ts` with proxy to `http://localhost:8000/api`
- `tsconfig.json` with strict mode, path aliases (`@/` -> `src/`)
- `.gitignore` for node_modules, dist, .vite

Install core deps:
```
react react-dom react-router-dom @tanstack/react-query zustand recharts @xyflow/react dagre
```

Dev deps:
```
typescript @types/react @types/react-dom vite @vitejs/plugin-react tailwindcss @tailwindcss/vite
```

**Acceptance:** `npm run dev` starts with no errors, shows "Hello World" at localhost:5173

---

### T-002: Configure Tailwind v4 + Shadcn UI + dark theme
**Assignee:** staff-eng-1
**Status:** [x]
**Depends on:** T-001
**Priority:** P0

1. Install Tailwind CSS v4 (use `@tailwindcss/vite` plugin — no `tailwind.config.ts` needed in v4)
2. Set up `src/index.css` with `@import "tailwindcss"` and CSS custom properties:
```css
@import "tailwindcss";

@theme {
  --color-background: #0D0D0D;
  --color-sidebar: #141414;
  --color-border: #262626;
  --color-primary: #3B82F6;
  --color-foreground: #EDEDED;
  --color-muted-foreground: #A1A1AA;
  --color-hover: rgba(255, 255, 255, 0.05);
  --color-card: #1A1A1A;
  --color-destructive: #EF4444;
  --color-success: #22C55E;
  --color-warning: #F59E0B;
}
```
3. Initialize Shadcn UI: `npx shadcn@latest init`
   - Style: New York
   - Base color: Zinc
   - CSS variables: yes
   - Configure `components.json` to use `@/components/ui` path
4. Add initial Shadcn components: `button`, `card`, `dialog`, `tabs`, `input`, `badge`, `command`, `slider`, `toast`, `dropdown-menu`, `separator`, `tooltip`, `select`, `textarea`, `checkbox`, `table`, `scroll-area`

**Acceptance:** Dark theme renders correctly, Shadcn components usable

---

### T-003: Generate Zod schemas from OpenAPI spec
**Assignee:** (unassigned)
**Status:** [ ]
**Depends on:** T-001
**Priority:** P0

Strategy: Use `openapi-zod-client` to auto-generate Zod schemas + a typed API client from the FastAPI `/openapi.json`.

1. Add dev dependency: `openapi-zod-client`
2. Create a script in `package.json`:
```json
{
  "scripts": {
    "generate-api": "openapi-zod-client http://localhost:8000/openapi.json -o src/api/generated.ts --with-description"
  }
}
```
3. If the server isn't running, save a snapshot of `openapi.json` to `src/api/openapi.json` and generate from that:
```json
{
  "scripts": {
    "generate-api": "openapi-zod-client ./src/api/openapi.json -o src/api/generated.ts --with-description",
    "fetch-openapi": "curl -s http://localhost:8000/openapi.json > src/api/openapi.json"
  }
}
```
4. Run generation and verify output contains Zod schemas for all DTOs (VaultDTO, EntityDTO, NoteDTO, MemoryUnitDTO, etc.)
5. Create `src/api/client.ts` that wraps the generated client with base URL config:
```typescript
const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1';
```
6. If `openapi-zod-client` produces suboptimal output, consider alternatives:
   - `orval` (generates Zod + TanStack Query hooks together)
   - Manual: `openapi-typescript` for types + hand-written Zod schemas for runtime validation at boundaries

**Acceptance:** Running `npm run generate-api` produces typed Zod schemas matching all FastAPI DTOs. TypeScript compiles clean.

---

### T-004: Create NDJSON streaming utility
**Assignee:** (unassigned)
**Status:** [ ]
**Depends on:** T-003
**Priority:** P0

Create `src/api/ndjson.ts`:
```typescript
export async function* streamNDJSON<T>(response: Response): AsyncGenerator<T> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (line.trim()) yield JSON.parse(line) as T;
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer) as T;
}

export async function collectNDJSON<T>(response: Response): Promise<T[]> {
  const items: T[] = [];
  for await (const item of streamNDJSON<T>(response)) {
    items.push(item);
  }
  return items;
}
```

Create `src/api/fetch.ts` — a thin fetch wrapper that handles NDJSON content type:
```typescript
export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  });
  if (!response.ok) throw new ApiError(response);
  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('x-ndjson')) {
    return collectNDJSON<any>(response) as Promise<T>;
  }
  return response.json();
}
```

**Acceptance:** Unit tests pass for NDJSON parsing (test with mock ReadableStream)

---

### T-005: Create TanStack Query hooks per domain
**Assignee:** (unassigned)
**Status:** [ ]
**Depends on:** T-003, T-004
**Priority:** P0

Create hooks in `src/api/hooks/`:

**`use-vaults.ts`**
- `useVaults()` — GET /vaults (list all)
- `useDefaultVaults()` — GET /vaults/defaults
- `useCreateVault()` — POST /vaults (mutation)
- `useDeleteVault()` — DELETE /vaults/:id (mutation)
- `useSetWriterVault()` — POST /vaults/:id/set-writer (mutation)
- `useToggleAttached()` — POST /vaults/:id/toggle-attached (mutation)

**`use-stats.ts`**
- `useSystemStats()` — GET /stats/counts
- `useTokenUsage()` — GET /stats/token-usage
- `useMetrics()` — GET /metrics (Prometheus text, auto-refresh 5s)

**`use-entities.ts`**
- `useEntities(vaultIds, filters)` — GET /entities (NDJSON)
- `useEntityMentions(entityId)` — GET /entities/:id/mentions
- `useEntityCooccurrences(entityId)` — GET /entities/:id/cooccurrences

**`use-notes.ts`**
- `useNotes(vaultIds)` — GET /notes (NDJSON)
- `useNote(noteId)` — GET /notes/:id
- `useNotePageIndex(noteId)` — GET /notes/:id/page-index
- `useNoteSearch(query, opts)` — POST /retrieval/note-search
- `useIngestNote()` — POST /ingestion/notes (mutation)

**`use-memories.ts`**
- `useMemories(vaultIds)` — GET /memories (NDJSON)
- `useMemory(unitId)` — GET /memories/:id
- `useMemorySearch(query, opts)` — POST /retrieval/search
- `useLineage(unitId)` — GET /memories/:id/lineage

**`use-summary.ts`**
- `useSummary(query, unitIds)` — POST /summary (mutation)

All hooks must:
- Use generated Zod schemas for response validation (parse with `.parse()` or `.safeParse()`)
- Accept `vaultIds` from the vault store where applicable
- Use proper query keys for cache invalidation

**Acceptance:** All hooks compile, return typed data, and invalidate correctly on mutations.

---

### T-006: Create Zustand stores
**Assignee:** (unassigned)
**Status:** [ ]
**Depends on:** T-001
**Priority:** P0

**`src/stores/vault-store.ts`** (port from `vault_state.py`):
```typescript
interface VaultStore {
  writerVaultId: string;
  writerVaultName: string;
  attachedVaults: { id: string; name: string }[];
  isInitialized: boolean;

  allSelectedVaultIds: () => string[];
  setWriterVault: (id: string, name: string) => void;
  toggleAttachedVault: (id: string, name: string, checked: boolean) => void;
  initialize: (defaults: DefaultVaultsResponse) => void;
}
```

**`src/stores/ui-store.ts`**:
```typescript
interface UIStore {
  isFullscreen: boolean;
  isQuickNoteOpen: boolean;
  isSidebarCollapsed: boolean;
  isCommandPaletteOpen: boolean;

  toggleFullscreen: () => void;
  toggleQuickNote: () => void;
  toggleSidebar: () => void;
  toggleCommandPalette: () => void;
}
```

**Acceptance:** Stores work with React components, vault store matches `vault_state.py` behavior.

---

### T-007: Build root layout + routing skeleton
**Assignee:** staff-eng-1
**Status:** [x]
**Depends on:** T-002, T-006
**Priority:** P0

**`src/main.tsx`**: Providers wrapping — `QueryClientProvider` + `RouterProvider` + Shadcn `Toaster`

**`src/app.tsx`**: Root layout with:
- Sidebar (240px, fixed left)
- Main content area with `<Outlet />`
- Quick Note modal (global)
- Command Palette (global, Cmd+K)

**Routes** (React Router v7):
```
/              -> Overview
/entity        -> Entity Graph
/lineage       -> Lineage
/search        -> Memory Search
/doc-search    -> Note Search
/status        -> System Status
/settings      -> Settings
```

Each page initially renders a placeholder with its name.

**Acceptance:** All 7 routes navigate correctly, sidebar highlights active page, layout is dark themed.

---

### T-008: Build sidebar component
**Assignee:** staff-eng-1
**Status:** [x]
**Depends on:** T-002
**Priority:** P0

Port from `components/sidebar.py`. Build `src/components/layout/sidebar.tsx`:

- 240px wide, fixed height 100vh
- Background: `#141414`, right border: `1px solid #262626`
- Logo: blue square + "Memex" heading
- Nav items with Lucide icons:
  - LayoutDashboard -> Overview (/)
  - Share2 -> Entity Graph (/entity)
  - GitBranch -> Lineage (/lineage)
  - Search -> Memory Search (/search)
  - FileSearch -> Note Search (/doc-search)
  - Activity -> System Status (/status)
- Bottom section:
  - Settings -> Settings (/settings)
  - CircleHelp -> Help (#)
- Active item: blue-tinted background + blue text
- Hover: subtle white overlay
- Use `NavLink` from React Router for active state

**Acceptance:** Pixel-close match to existing sidebar, smooth transitions.

---

### T-009: Build shared components
**Assignee:** dev-2
**Status:** [x]
**Depends on:** T-002
**Priority:** P1

**`src/components/layout/page-header.tsx`**:
- Page title (left) + Quick Note button (right)
- Optional subtitle/description

**`src/components/shared/metric-card.tsx`**:
- Icon + label + value + optional trend
- Used on Overview (3x) and System Status (6x)
- Shadcn `Card` with dark theme colors

**`src/components/shared/type-badge.tsx`**:
- Color-coded badges for entity types (Person, Organization, Location, Concept, etc.)
- Map entity types to colors

**`src/components/shared/detail-modal.tsx`**:
- Generic key-value properties display in a `Dialog`
- Reused on 5+ pages for entity/memory/note details

**`src/components/shared/strategy-filter.tsx`**:
- TEMPR strategy toggle buttons (Temporal, Entity, Mental Model, Keyword, Semantic)
- Used on Memory Search and Note Search pages

**`src/components/shared/summary-card.tsx`**:
- AI summary display with clickable citation markers `[1]`, `[2]` etc.
- Replaces the hidden `__cite-bridge` input hack from the Reflex version
- Citation clicks scroll to or highlight the referenced result

**`src/components/quick-note-modal.tsx`**:
- Textarea + Save button
- Uses `useIngestNote()` mutation
- Targets writer vault from vault store

**`src/components/command-palette.tsx`**:
- Shadcn `Command` component
- Cmd+K to open
- Search across pages, actions (Quick Note, toggle fullscreen)

**Acceptance:** All components render correctly in dark theme, are reusable.

---

### T-010: Add justfile entries + package.json scripts
**Assignee:** staff-eng-1
**Status:** [x]
**Depends on:** T-001
**Priority:** P1

Add to root `justfile`:
```
# Start new dashboard in dev mode
dashboard-dev:
  cd packages/dashboard && npm run dev

# Build new dashboard for production
dashboard-build:
  cd packages/dashboard && npm run build

# Generate API types from OpenAPI spec
dashboard-generate-api:
  cd packages/dashboard && npm run generate-api
```

**package.json** scripts:
```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "generate-api": "openapi-zod-client ./src/api/openapi.json -o src/api/generated.ts",
    "fetch-openapi": "curl -s http://localhost:8000/openapi.json > src/api/openapi.json",
    "lint": "eslint .",
    "typecheck": "tsc --noEmit"
  }
}
```

**Acceptance:** `just dashboard-dev` works, `just dashboard-build` produces dist/.

---

## Phase 1: Simple Pages

### T-101: Settings page
**Assignee:** dev-1
**Status:** [x]
**Depends on:** T-005, T-007, T-009
**Priority:** P1

Port from `pages/settings.py` (278 lines).

- Shadcn `Tabs`: Vaults / Preferences
- **Vaults tab**:
  - Table of all vaults with columns: Name, Role (Writer/Attached/Available badge), Actions
  - "Create Vault" button -> `Dialog` with name input
  - Set as Writer / Toggle Attached / Delete actions
  - Uses `useVaults()`, `useCreateVault()`, `useDeleteVault()`, `useSetWriterVault()`
- **Preferences tab**:
  - Placeholder for future settings

**Acceptance:** Can create vault, set writer, toggle attached, delete vault. Matches current settings page behavior.

---

### T-102: System Status page
**Assignee:** dev-1
**Status:** [x]
**Depends on:** T-005, T-007, T-009
**Priority:** P1

Port from `pages/status.py` (180 lines).

- 6 `MetricCard` components in 2 rows of 3:
  - Row 1 (KPIs): Memory Units, Notes, Entities
  - Row 2 (Resources): CPU Usage, Memory Usage, Uptime
- Auto-refresh with `refetchInterval: 5000`
- Port Prometheus text parsing from lines 52-59 of `status.py`:
  - Parse `process_cpu_seconds_total`, `process_resident_memory_bytes`, `process_start_time_seconds`
- Computed values: CPU %, memory MB, uptime string

**Acceptance:** All 6 metrics display and auto-refresh every 5s.

---

### T-103: Overview page
**Assignee:** dev-2
**Status:** [x]
**Depends on:** T-005, T-007, T-009
**Priority:** P1

Port from `pages/overview.py` (419 lines).

- 3 MetricCards: Total Memories, Total Notes, Total Entities
- Recharts `BarChart` for token usage (by model)
- Recent Memories feed (last 10 memory units)
  - Each item clickable -> detail modal
- Server Health panel (reuse metrics parsing from T-102)
- Uses `useSystemStats()`, `useTokenUsage()`, `useMemories()`, `useMetrics()`

**Acceptance:** Overview renders with real data, chart works, recent memories clickable.

---

## Phase 2: Search Pages

### T-201: Memory Search page
**Assignee:** dev-3
**Status:** [x]
**Depends on:** T-005, T-007, T-009
**Priority:** P2

Port from `pages/search.py` (649 lines).

- Search input with debounce
- TEMPR strategy filter toggles (StrategyFilter component)
- Results list with:
  - Memory unit cards showing content preview, entity badges, confidence score
  - Actions: Details, Lineage, Entity Graph
- AI Summary card at top (SummaryCard component):
  - Citation markers `[1]` `[2]` that scroll to referenced results
  - Replaces the `__cite-bridge` hidden input pattern from Reflex
- Detail modal for selected memory unit
- Pagination (load more / virtual scroll)
- Vault-scoped: uses `allSelectedVaultIds` from vault store

**Acceptance:** Search works, strategies toggle, citations click to results, detail modal opens.

---

### T-202: Note Search page
**Assignee:** (unassigned)
**Status:** [ ]
**Depends on:** T-005, T-007, T-009
**Priority:** P2

Port from `pages/doc_search.py` (849 lines).

- Search input + strategy filter (same as Memory Search)
- Results with snippet previews + relevance scores
- AI Summary with citations
- Selected note detail in split layout:
  - Left: Full note content (markdown rendered)
  - Right: Page index tree + metadata
- Page index tree:
  - Recursive React component with box-drawing characters (├── └──)
  - Click node -> highlight in content
- Uses `useNoteSearch()`, `useNote()`, `useNotePageIndex()`

**Acceptance:** Note search works, page index tree renders, split layout correct.

---

## Phase 3: Graph Visualizations

### T-301: Entity Graph page
**Assignee:** dev-4
**Status:** [x]
**Depends on:** T-005, T-007, T-009
**Priority:** P2

Port from `pages/entity.py` (975 lines — most complex page).

Use **React Flow v12** with `@xyflow/react`:
- d3-force layout for entity positioning (replaces NetworkX `spring_layout`)
- Custom node components: colored circles with entity name labels
- Edge styling: opacity based on co-occurrence strength
- **Filter panel** (Shadcn Sliders):
  - Connection strength threshold
  - Importance threshold
  - Recency filter
- **Side panel**: Selected entity info
  - Entity details (type, description)
  - Mentions list with expandable items
  - Co-occurrences list
- **Interactions**:
  - Click node -> select entity, show side panel
  - Double-click node -> open detail modal
  - Pan/zoom/drag built into React Flow (eliminates 60+ lines of manual mouse tracking)
- Fullscreen toggle

**Acceptance:** Graph renders with real entities, filters work, node selection shows details.

---

### T-302: Lineage page
**Assignee:** (unassigned)
**Status:** [ ]
**Depends on:** T-005, T-007, T-009
**Priority:** P2

Port from `pages/lineage.py` (775 lines).

Use **React Flow v12** with **dagre** layout:
- `rankdir: 'LR'` for left-to-right DAG
- 5 custom node types (one per entity type, colored):
  - Note, MemoryUnit, Observation, MentalModel, Entity
- Edges: animated, styled by relationship type
- **Path highlighting**:
  - Click a node -> highlight all ancestors (BFS upstream) + descendants (BFS downstream)
  - Replaces `nx.ancestors`/`nx.descendants` from NetworkX
- **Entity search autocomplete**:
  - Shadcn `Command` component
  - Search entities, select one -> load its lineage tree
  - Uses `useEntities()` for autocomplete
- **Layout**: Auto-fit on load, recenter button

**Acceptance:** Lineage DAG renders correctly, path highlighting works, autocomplete works.

---

## Phase 4: CLI + Polish

### T-401: Rewrite CLI `dashboard.py`
**Assignee:** (unassigned)
**Status:** [ ]
**Depends on:** T-001
**Priority:** P2

Rewrite `packages/cli/src/memex_cli/dashboard.py`:

**`check_dashboard_installed()`**:
- Check for `node` and `npm` on PATH using `shutil.which()`
- Locate dashboard directory: check sibling `packages/dashboard/` or installed location
- Verify `package.json` exists

**Remove** `_get_dashboard_pkg_root()` and `_setup_runtime_cwd()` entirely.

**`start` command**:
- `--dev` mode: `npm run dev -- --host {host} --port {port}` in dashboard dir
- Production mode: `npx serve dist -l tcp://{host}:{port}` (requires `npm run build` first)
- `--daemon` mode: same `subprocess.Popen` pattern with npm commands
- Keep port check, PID management, log file logic

**`status` command**:
- Change health check from `/ping/` to `GET /` (returns 200 for both dev and production)

**`stop` command**: No changes needed (SIGTERM/SIGKILL works for any process).

---

### T-402: Update CLI pyproject.toml and utils.py
**Assignee:** (unassigned)
**Status:** [ ]
**Depends on:** T-401
**Priority:** P2

1. `packages/cli/pyproject.toml`: Remove `dashboard = ["memex_dashboard"]` from optional deps
2. `packages/cli/src/memex_cli/utils.py` lines 92-95: Change error message:
```python
elif cmd_name == 'dashboard':
    console.print('[bold red]Error:[/bold red] Dashboard requires Node.js.')
    console.print('Install Node.js and run: [cyan]cd packages/dashboard && npm install[/cyan]')
    raise typer.Exit(code=1)
```

---

### T-403: Vite proxy configuration
**Assignee:** staff-eng-1
**Status:** [x]
**Depends on:** T-001
**Priority:** P1

In `vite.config.ts`:
```typescript
export default defineConfig({
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/metrics': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
```

This allows the frontend to call `/api/v1/...` without CORS issues during development.

---

### T-404: Update justfile for cutover
**Assignee:** staff-eng-1
**Status:** [x]
**Depends on:** T-401
**Priority:** P3

- Remove old `packages/dashboard/` references if any
- Ensure `just dashboard-dev` and `just dashboard-build` are the canonical commands

---

## UX Polish Tickets

### T-501: Loading states and skeleton screens
**Assignee:** UX Developer
**Status:** [ ]
**Depends on:** T-007
**Priority:** P2

- Add Shadcn `Skeleton` components for all data-loading states
- Ensure no layout shift when data arrives
- Add loading spinners for mutations (save, delete)
- Suspense boundaries for code-split routes

---

### T-502: Error states and empty states
**Assignee:** UX Developer
**Status:** [ ]
**Depends on:** T-007
**Priority:** P2

- API error display with retry button
- Empty states with helpful messages ("No memories found. Try adjusting your search.")
- Connection error banner (when backend unreachable)
- Toast notifications for mutation success/failure

---

### T-503: Keyboard navigation and accessibility
**Assignee:** UX Developer
**Status:** [ ]
**Depends on:** T-007, T-009
**Priority:** P2

- Cmd+K for command palette
- Escape to close modals/command palette
- Tab navigation through sidebar items
- Focus management in modals
- ARIA labels on interactive elements
- Keyboard shortcuts for common actions (Cmd+N for Quick Note)

---

### T-504: Transitions and micro-interactions
**Assignee:** UX Developer
**Status:** [ ]
**Depends on:** All pages
**Priority:** P3

- Smooth page transitions (no flash of white between routes)
- Hover states on all interactive elements
- Sidebar item transitions (as defined in style.py)
- Modal open/close animations
- Search result fade-in
- Graph node hover effects
- Ensure 60fps on all animations

---

### T-505: Responsive layout
**Assignee:** UX Developer
**Status:** [ ]
**Depends on:** All pages
**Priority:** P3

- Sidebar collapse to icons on narrow screens (<1024px)
- Stack metric cards vertically on mobile
- Graph pages: hide filter panel behind toggle on small screens
- Search pages: full-width on mobile
- Settings: stack tabs vertically on narrow screens
